import hashlib
import json
from typing import ClassVar

from apps.accounts.permissions import accessible_workspace_ids, membership
from apps.core.models import Entity, WorldWorkspace
from apps.core.models import WorldBranch
from apps.core.serializers import (
    CommitJobSerializer,
    EntitySerializer,
    RelationSerializer,
)
from apps.core.services.branching import (
    capture_main_snapshot,
    materialize_branch_baseline,
)
from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import (
    CanvasOperation,
    CanvasRevision,
    CanvasSyncEvent,
    DialogueMessage,
    DialogueSession,
    EntityProposal,
    RelationProposal,
    StagingCanvas,
)
from .serializers import (
    DialogueMessageSerializer,
    DialogueSessionSerializer,
    EntityProposalSerializer,
    RelationProposalSerializer,
    StagingCanvasSerializer,
)
from .services.commit import CanvasCommitError, CanvasCommitService
from .services.sync_ticket import issue_sync_ticket


def _snapshot_hash(snapshot):
    return hashlib.sha256(json.dumps(snapshot or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class CanvasViewSet(viewsets.ModelViewSet):
    serializer_class = StagingCanvasSerializer
    http_method_names: ClassVar[list[str]] = ["get", "post", "patch", "head", "options"]

    @staticmethod
    def _etag(canvas):
        return f'"canvas-{canvas.snapshot_version}"'

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        canvas = self.get_object()
        response["ETag"] = self._etag(canvas)
        return response

    def get_queryset(self):
        qs = StagingCanvas.objects.select_related("workspace").filter(workspace_id__in=accessible_workspace_ids(self.request.user))
        if self.request.query_params.get("purpose") != StagingCanvas.Purpose.GRAPH:
            qs = qs.filter(purpose=StagingCanvas.Purpose.STAGING)
        if self.request.query_params.get("workspace"):
            qs = qs.filter(workspace_id=self.request.query_params["workspace"])
        return qs

    @action(detail=False, methods=["post"], url_path="graph")
    def graph_canvas(self, request):
        workspace_id = request.data.get("workspace")
        branch_key = str(request.data.get("branch") or "main")
        workspace = WorldWorkspace.objects.filter(id=workspace_id, id__in=accessible_workspace_ids(request.user)).first()
        if not workspace:
            return Response({"detail": "workspace not found"}, status=404)
        branch = WorldBranch.objects.filter(workspace=workspace, status=WorldBranch.Status.ACTIVE).filter(
            models.Q(name=branch_key) | models.Q(id=branch_key) if branch_key != "main" else models.Q(name="main")
        ).first()
        if not branch:
            return Response({"detail": "branch not found"}, status=404)
        member = membership(request.user, workspace)
        if not member:
            return Response({"detail": "无权访问该世界观"}, status=403)
        canvas, created = StagingCanvas.objects.get_or_create(
            workspace=workspace, branch=branch, purpose=StagingCanvas.Purpose.GRAPH,
            defaults={"name": f"{workspace.name} · {branch.name} · 世界观图谱", "snapshot": {}},
        )
        if created:
            CanvasRevision.objects.create(canvas=canvas, version=canvas.snapshot_version, snapshot=canvas.snapshot, sync_metadata={})
        return Response(self.get_serializer(canvas).data, status=201 if created else 200)

    def _graph_canvas(self, request, pk):
        canvas = StagingCanvas.objects.select_related("workspace", "branch").filter(pk=pk, purpose=StagingCanvas.Purpose.GRAPH).first()
        if not canvas or canvas.workspace_id not in accessible_workspace_ids(request.user):
            raise ValidationError("图谱画布不存在")
        return canvas

    @action(detail=True, methods=["get"], url_path="graph-projection")
    def graph_projection(self, request, pk=None):
        canvas = self._graph_canvas(request, pk)
        from apps.core.services.branching import effective_entities, effective_relations
        entities = effective_entities(canvas.workspace, canvas.branch)
        relations = effective_relations(canvas.workspace, canvas.branch)
        return Response({
            "canvas": self.get_serializer(canvas).data,
            "nodes": [{"id": str(e.id), "title": e.title, "type": e.type, "status": e.status, "content": e.content} for e in entities],
            "edges": [{"id": str(r.id), "source": str(r.source_id), "target": str(r.target_id), "label": r.relation_type, "relation_type": r.relation_type, "status": "archived" if r.archived else "active"} for r in relations],
            "layout": (canvas.snapshot or {}).get("graphLayout", {}),
        })

    @action(detail=True, methods=["get", "post"], url_path="graph-relation-proposals")
    def graph_relation_proposals(self, request, pk=None):
        canvas = self._graph_canvas(request, pk)
        if request.method == "GET":
            rows = RelationProposal.objects.filter(canvas=canvas).select_related("source_entity", "source_proposal", "target_entity", "target_proposal")
            return Response(RelationProposalSerializer(rows, many=True).data)
        member = membership(request.user, canvas.workspace)
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        payload = dict(request.data)
        payload["canvas"] = str(canvas.id)
        payload["workspace"] = str(canvas.workspace_id)
        serializer = RelationProposalSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        for endpoint in (serializer.validated_data.get("source_entity"), serializer.validated_data.get("target_entity")):
            if endpoint and (endpoint.workspace_id != canvas.workspace_id or endpoint.status != Entity.Status.ACTIVE or endpoint.branch_id not in (None, canvas.branch_id)):
                raise ValidationError("正式实体必须属于当前分支且处于有效状态")
        serializer.save(workspace=canvas.workspace, canvas=canvas, status=RelationProposal.Status.PENDING)
        return Response(serializer.data, status=201)

    @action(detail=True, methods=["post"], url_path="graph-preview")
    def graph_preview(self, request, pk=None):
        canvas = self._graph_canvas(request, pk)
        return Response(CanvasCommitService.preview(canvas, [], request.data.get("relation_proposal_ids", [])))

    @action(detail=True, methods=["post"], url_path="graph-commit")
    def graph_commit(self, request, pk=None):
        canvas = self._graph_canvas(request, pk)
        member = membership(request.user, canvas.workspace)
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        try:
            job, entities, relations = CanvasCommitService.commit(canvas, [], request.data.get("relation_proposal_ids", []), request.data.get("idempotency_key"), request.data.get("preview_token"))
        except CanvasCommitError as exc:
            raise ValidationError(str(exc))
        return Response({"job": CommitJobSerializer(job).data, "entities": EntitySerializer(entities, many=True).data, "relations": RelationSerializer(relations, many=True).data})

    def perform_create(self, serializer):
        workspace = serializer.validated_data["workspace"]
        if not membership(self.request.user, workspace) or membership(self.request.user, workspace).role == "reader":
            raise ValidationError("需要 editor 或 owner 权限")
        try:
            with transaction.atomic():
                # Lock the workspace while capturing the branch baseline so a
                # concurrent merge cannot be split across the new canvas view.
                canvas = serializer.save()
                if canvas.branch_id is None:
                    from apps.core.models import WorldBranch
                    workspace = WorldWorkspace.objects.select_for_update().get(pk=canvas.workspace_id)
                    branch = WorldBranch.objects.create(
                        workspace=workspace,
                        name=f"canvas-{str(canvas.id).replace('-', '')[:12]}",
                        base_snapshot=capture_main_snapshot(workspace),
                    )
                    materialize_branch_baseline(branch)
                    canvas.branch = branch
                    canvas.save(update_fields=["branch"])
                CanvasRevision.objects.create(
                    canvas=canvas,
                    version=1,
                    snapshot=canvas.snapshot,
                    sync_metadata=canvas.sync_metadata,
                    operation_cursor=canvas.snapshot_operation_cursor,
                )
        except IntegrityError as exc:
            raise ValidationError({"detail": "无法创建唯一的画布工作分支"}) from exc

    def partial_update(self, request, *args, **kwargs):
        with transaction.atomic():
            canvas = StagingCanvas.objects.select_for_update().get(pk=self.get_object().pk)
            member = membership(request.user, canvas.workspace)
            if not member or member.role == "reader": return Response({"detail":"需要 editor 权限"}, status=403)
            expected = request.data.get("expected_version")
            if expected is None:
                expected = request.headers.get("If-Match")
                if isinstance(expected, str):
                    expected = expected.strip().strip('"')
                    if expected.startswith("canvas-"):
                        expected = expected.removeprefix("canvas-")
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                return Response({"detail": "需要 expected_version 或 If-Match 画布版本", "current_version": canvas.snapshot_version}, status=409)
            if expected != canvas.snapshot_version:
                return Response({
                    "detail": "画布版本冲突，请先保留本地副本并重新加载",
                    "current_version": canvas.snapshot_version,
                    "etag": self._etag(canvas),
                }, status=409)
            data = {k: v for k, v in request.data.items() if k in {"snapshot", "name", "status"}}
            serializer = self.get_serializer(canvas, data=data, partial=True)
            serializer.is_valid(raise_exception=True)
            CanvasRevision.objects.update_or_create(
                canvas=canvas,
                version=canvas.snapshot_version,
                defaults={
                    "snapshot": canvas.snapshot,
                    "sync_metadata": canvas.sync_metadata,
                    "operation_cursor": canvas.snapshot_operation_cursor,
                },
            )
            serializer.save(snapshot_version=canvas.snapshot_version + 1, last_saved_at=timezone.now())
        response = Response(serializer.data)
        response["ETag"] = self._etag(canvas)
        return response

    def get_authenticators(self):
        # The sync daemon is an internal peer, not a browser client. It uses a
        # separate shared secret for the snapshot endpoint because it cannot
        # forward a Django session cookie.
        if getattr(self, "action", None) in {"sync_state", "sync_ops", "sync_events", "sync_reconciliation", "compact_ops"}:
            return []
        return super().get_authenticators()

    def get_permissions(self):
        if getattr(self, "action", None) in {"sync_state", "sync_ops", "sync_events", "sync_reconciliation", "compact_ops"}:
            return [AllowAny()]
        return super().get_permissions()

    def _sync_internal_allowed(self, request):
        import hmac
        supplied = request.headers.get("X-Sync-Internal-Secret", "")
        expected = getattr(settings, "SYNC_INTERNAL_SECRET", "")
        return bool(expected) and hmac.compare_digest(supplied, expected)

    @action(detail=True, methods=["get", "post"], url_path="sync-state")
    def sync_state(self, request, pk=None):
        if not self._sync_internal_allowed(request):
            return Response({"detail": "sync service authentication required"}, status=403)
        if request.method == "GET":
            canvas = StagingCanvas.objects.filter(pk=pk).first()
            if not canvas:
                return Response({"detail": "canvas not found"}, status=404)
            latest_event = canvas.sync_events.order_by("-document_clock", "-created_at").values(
                "id", "document_clock", "snapshot_hash", "protocol_version", "schema_version", "room_key", "created_at"
            ).first()
            return Response({
                "canvas": str(canvas.id), "snapshot": canvas.snapshot,
                "snapshot_hash": _snapshot_hash(canvas.snapshot),
                "sync_metadata": canvas.sync_metadata,
                "snapshot_version": canvas.snapshot_version,
                "operation_cursor": canvas.operation_sequence,
                "snapshot_operation_cursor": canvas.snapshot_operation_cursor,
                "operation_compacted_through": canvas.operation_compacted_through,
                "latest_sync_event": latest_event,
                "status": canvas.status,
            })

        snapshot = request.data.get("snapshot")
        sync_metadata = request.data.get("sync_metadata", {})
        expected = request.data.get("expected_version")
        if isinstance(sync_metadata, dict) and sync_metadata.get("protocol") == "records-v1":
            return Response({"detail": "records-v1 retired; use tldraw-sync-v2"}, status=410)
        if not isinstance(snapshot, dict):
            return Response({"detail": "snapshot must be an object"}, status=400)
        if not isinstance(sync_metadata, dict):
            return Response({"detail": "sync_metadata must be an object"}, status=400)
        try:
            expected = int(expected)
        except (TypeError, ValueError):
            return Response({"detail": "expected_version must be an integer"}, status=400)
        requested_cursor = request.data.get("operation_cursor")
        if requested_cursor is not None:
            try:
                requested_cursor = max(0, int(requested_cursor))
            except (TypeError, ValueError):
                return Response({"detail": "operation_cursor must be an integer"}, status=400)
        with transaction.atomic():
            canvas = StagingCanvas.objects.select_for_update().filter(pk=pk).first()
            if not canvas:
                return Response({"detail": "canvas not found"}, status=404)
            if canvas.status == StagingCanvas.Status.ARCHIVED:
                return Response({"detail": "archived canvas"}, status=409)
            if expected != canvas.snapshot_version:
                return Response({
                    "detail": "snapshot version conflict", "snapshot": canvas.snapshot,
                    "snapshot_hash": _snapshot_hash(canvas.snapshot),
                    "sync_metadata": canvas.sync_metadata,
                    "snapshot_version": canvas.snapshot_version,
                    "operation_cursor": canvas.operation_sequence,
                    "snapshot_operation_cursor": canvas.snapshot_operation_cursor,
                }, status=409)
            # Keep the same revision history guarantees as the browser REST
            # save path.  The sync daemon is an internal transport, not a
            # version-history bypass.
            CanvasRevision.objects.update_or_create(
                canvas=canvas,
                version=canvas.snapshot_version,
                defaults={
                    "snapshot": canvas.snapshot,
                    "sync_metadata": canvas.sync_metadata,
                    "operation_cursor": canvas.snapshot_operation_cursor,
                },
            )
            canvas.snapshot = snapshot
            canvas.sync_metadata = sync_metadata
            if requested_cursor is not None:
                # The sync service appends operations before persisting the
                # materialized snapshot. Clamp a future cursor, while keeping
                # the prior checkpoint for ordinary REST saves.
                canvas.snapshot_operation_cursor = min(requested_cursor, canvas.operation_sequence)
            canvas.snapshot_version += 1
            canvas.last_saved_at = timezone.now()
            canvas.save(update_fields=["snapshot", "sync_metadata", "snapshot_version", "snapshot_operation_cursor", "last_saved_at", "updated_at"])
            if str(sync_metadata.get("protocol") or "") == "tldraw-sync-v2":
                CanvasSyncEvent.objects.get_or_create(
                    canvas=canvas,
                    document_clock=max(0, int(sync_metadata.get("document_clock") or 0)),
                    snapshot_hash=_snapshot_hash(snapshot),
                    defaults={
                        "branch": canvas.branch,
                        "room_key": str(sync_metadata.get("room_key") or f"{canvas.workspace_id}:{canvas.branch_id or 'main'}:{canvas.id}"),
                        "diff": sync_metadata.get("diff") if isinstance(sync_metadata.get("diff"), dict) else {},
                        "protocol_version": str(sync_metadata.get("protocol") or "tldraw-sync-v2"),
                        "schema_version": str(sync_metadata.get("schema_version") or "oc-tldraw-2"),
                        "source": str(sync_metadata.get("source") or "sync-service"),
                        "client_session": str(sync_metadata.get("client_session") or ""),
                        "metadata": sync_metadata,
                    },
                )
        return Response({
            "canvas": str(canvas.id),
            "snapshot": canvas.snapshot,
            "snapshot_version": canvas.snapshot_version,
            "operation_cursor": canvas.operation_sequence,
            "snapshot_operation_cursor": canvas.snapshot_operation_cursor,
            "operation_compacted_through": canvas.operation_compacted_through,
            "sync_events": list(canvas.sync_events.order_by("-document_clock", "-created_at").values("document_clock", "snapshot_hash", "protocol_version", "schema_version", "created_at")[:10]),
        })

    @action(detail=True, methods=["get"], url_path="sync-events")
    def sync_events(self, request, pk=None):
        if not self._sync_internal_allowed(request):
            return Response({"detail": "sync service authentication required"}, status=403)
        canvas = StagingCanvas.objects.filter(pk=pk).first()
        if not canvas:
            return Response({"detail": "canvas not found"}, status=404)
        try:
            after = max(0, int(request.query_params.get("after", 0)))
            limit = min(1000, max(1, int(request.query_params.get("limit", 200))))
        except (TypeError, ValueError):
            return Response({"detail": "after 和 limit 必须是整数"}, status=400)
        rows = list(canvas.sync_events.filter(document_clock__gt=after).order_by("document_clock", "created_at")[: limit + 1])
        page = rows[:limit]
        last = page[-1] if page else None
        return Response({
            "events": [{"id": str(row.id), "document_clock": row.document_clock, "snapshot_hash": row.snapshot_hash, "diff": row.diff, "protocol_version": row.protocol_version, "schema_version": row.schema_version, "room_key": row.room_key, "created_at": row.created_at} for row in page],
            "cursor": last.document_clock if last else after,
            "has_more": len(rows) > limit,
        })

    @action(detail=True, methods=["post"], url_path="sync-reconciliation")
    def sync_reconciliation(self, request, pk=None):
        """Persist sync-room reconciliation health without creating a revision."""
        if not self._sync_internal_allowed(request):
            return Response({"detail": "sync service authentication required"}, status=403)
        reconciliation = request.data.get("reconciliation") if isinstance(request.data, dict) else None
        if not isinstance(reconciliation, dict):
            return Response({"detail": "reconciliation must be an object"}, status=400)
        with transaction.atomic():
            canvas = StagingCanvas.objects.select_for_update().filter(pk=pk).first()
            if not canvas:
                return Response({"detail": "canvas not found"}, status=404)
            metadata = dict(canvas.sync_metadata or {})
            metadata["reconciliation"] = reconciliation
            canvas.sync_metadata = metadata
            canvas.save(update_fields=["sync_metadata", "updated_at"])
        return Response({"canvas": str(canvas.id), "reconciliation": reconciliation})

    @action(detail=True, methods=["get", "post"], url_path="sync-ops")
    def sync_ops(self, request, pk=None):
        """Read-only historical records-v1 log for one-time migration."""
        if not self._sync_internal_allowed(request):
            return Response({"detail": "sync service authentication required"}, status=403)
        canvas = StagingCanvas.objects.filter(pk=pk).first()
        if not canvas:
            return Response({"detail": "canvas not found"}, status=404)
        if request.method == "GET":
            try:
                after = max(0, int(request.query_params.get("after", 0)))
                limit = min(1000, max(1, int(request.query_params.get("limit", 200))))
            except (TypeError, ValueError):
                return Response({"detail": "after 和 limit 必须是整数"}, status=400)
            operations = list(canvas.operations.filter(sequence__gt=after).order_by("sequence")[:limit])
            return Response({
                "operations": [{"sequence": row.sequence, "operation": row.operation} for row in operations],
                "cursor": canvas.operation_sequence,
                "snapshot_operation_cursor": canvas.snapshot_operation_cursor,
                "compacted_through": canvas.operation_compacted_through,
                "has_more": bool(operations and operations[-1].sequence < canvas.operation_sequence),
            })

        return Response({"detail": "records-v1 retired; use tldraw-sync-v2"}, status=410)

    @action(detail=True, methods=["post"], url_path="compact-ops")
    def compact_ops(self, request, pk=None):
        """Compact the durable operation log after a persisted snapshot.

        A snapshot is the recovery checkpoint. Operations are deleted only at
        or below the cursor known to be included in that checkpoint, and a
        small tail may be retained for diagnostics. This endpoint is reserved
        for the internal sync worker; browser users cannot delete the audit
        log.
        """
        if not self._sync_internal_allowed(request):
            return Response({"detail": "sync service authentication required"}, status=403)
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            keep_last = max(0, min(10_000, int(payload.get("keep_last", 100))))
        except (TypeError, ValueError):
            return Response({"detail": "keep_last must be an integer"}, status=400)
        requested_through = payload.get("through")
        if requested_through is not None:
            try:
                requested_through = max(0, int(requested_through))
            except (TypeError, ValueError):
                return Response({"detail": "through must be an integer"}, status=400)
        with transaction.atomic():
            canvas = StagingCanvas.objects.select_for_update().filter(pk=pk).first()
            if not canvas:
                return Response({"detail": "canvas not found"}, status=404)
            checkpoint = min(canvas.snapshot_operation_cursor, canvas.operation_sequence)
            if requested_through is not None:
                checkpoint = min(checkpoint, requested_through)
            target = max(canvas.operation_compacted_through, checkpoint - keep_last)
            if target <= canvas.operation_compacted_through:
                return Response({
                    "canvas": str(canvas.id),
                    "compacted_through": canvas.operation_compacted_through,
                    "deleted": 0,
                    "remaining": canvas.operations.count(),
                    "snapshot_operation_cursor": canvas.snapshot_operation_cursor,
                    "operation_cursor": canvas.operation_sequence,
                })
            deleted, _ = canvas.operations.filter(sequence__lte=target).delete()
            canvas.operation_compacted_through = target
            canvas.save(update_fields=["operation_compacted_through", "updated_at"])
            remaining = canvas.operations.count()
        return Response({
            "canvas": str(canvas.id),
            "compacted_through": canvas.operation_compacted_through,
            "deleted": deleted,
            "remaining": remaining,
            "snapshot_operation_cursor": canvas.snapshot_operation_cursor,
            "operation_cursor": canvas.operation_sequence,
        })

    @action(detail=True, methods=["post"], url_path="sync-ticket")
    def sync_ticket(self, request, pk=None):
        """Issue a scoped ticket for the self-hosted websocket sync service."""
        canvas = StagingCanvas.objects.select_related("workspace", "branch").filter(
            pk=pk, workspace_id__in=accessible_workspace_ids(request.user)
        ).first()
        if not canvas:
            return Response({"detail": "canvas not found"}, status=404)
        member = membership(request.user, canvas.workspace)
        if not member:
            return Response({"detail": "无权访问该画布"}, status=403)
        if canvas.status == StagingCanvas.Status.ARCHIVED:
            return Response({"detail": "归档画布不能建立实时协作连接"}, status=409)
        ttl = request.data.get("ttl", 300) if isinstance(request.data, dict) else 300
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            return Response({"detail": "ttl 必须是整数"}, status=400)
        client_id = request.data.get("client_id") if isinstance(request.data, dict) else None
        if client_id is not None:
            client_id = str(client_id)
            if not 1 <= len(client_id) <= 128 or any(ord(char) < 0x21 or ord(char) > 0x7e for char in client_id):
                return Response({"detail": "client_id 必须是 1-128 个可打印 ASCII 字符"}, status=400)
        ticket, expires_at = issue_sync_ticket(
            user=request.user, workspace_id=canvas.workspace_id, canvas_id=canvas.id,
            branch_id=canvas.branch_id or "main", role=member.role, ttl=ttl, client_id=client_id,
        )
        from django.conf import settings
        service_url = getattr(settings, "SYNC_SERVICE_URL", "ws://127.0.0.1:8787")
        return Response({
            "ticket": ticket, "expires_at": expires_at,
            "url": f"{service_url.rstrip('/')}/rooms/{canvas.id}",
            "workspace": str(canvas.workspace_id), "canvas": str(canvas.id),
            "branch": str(canvas.branch_id or "main"),
            "role": member.role,
        })

    @action(detail=True, methods=["get"])
    def revisions(self, request, pk=None):
        return Response(list(self.get_object().revisions.values("version", "created_at")))

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        canvas = self.get_object()
        member = membership(request.user, canvas.workspace)
        if not member or member.role == "reader": return Response({"detail":"需要 editor 权限"}, status=403)
        version = request.data.get("version")
        if not isinstance(version, int):
            raise ValidationError("version 必須为整数")
        revision = canvas.revisions.filter(version=version).first()
        if not revision:
            return Response({"detail": "找不到快照"}, status=404)
        request.data["snapshot"] = revision.snapshot
        request.data["sync_metadata"] = revision.sync_metadata
        request.data["operation_cursor"] = revision.operation_cursor
        return self.partial_update(request, pk=pk)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        original = self.get_object()
        member = membership(request.user, original.workspace)
        if not member or member.role == "reader": return Response({"detail":"需要 editor 权限"}, status=403)
        canvas = StagingCanvas.objects.create(workspace=original.workspace, name=original.name + " · 副本", snapshot=original.snapshot, sync_metadata=original.sync_metadata)
        from apps.core.models import WorldBranch
        branch = WorldBranch.objects.create(
            workspace=original.workspace,
            name=f"canvas-{str(canvas.id).replace('-', '')[:12]}",
            base_snapshot=capture_main_snapshot(original.workspace),
        )
        materialize_branch_baseline(branch)
        canvas.branch=branch; canvas.save(update_fields=["branch"])
        # Referenced proposals receive new IDs; no shared mutable business state.
        mapping = {}
        for proposal in original.entity_proposals.all():
            old = str(proposal.id)
            proposal.id = None
            proposal.canvas, proposal.status, proposal.created_entity = canvas, "pending", None
            proposal.save()
            mapping[old] = str(proposal.id)
        import json
        snapshot = json.dumps(canvas.snapshot)
        for old, new in mapping.items():
            snapshot = snapshot.replace(old, new)
        canvas.snapshot = json.loads(snapshot)
        canvas.save()
        for relation in original.relation_proposals.all():
            relation.id = None
            relation.canvas = canvas
            relation.source_proposal_id = mapping[str(relation.source_proposal_id)]
            if relation.target_proposal_id:
                relation.target_proposal_id = mapping[str(relation.target_proposal_id)]
            relation.status, relation.created_relation = "pending", None
            relation.save()
        CanvasRevision.objects.create(
            canvas=canvas,
            version=1,
            snapshot=canvas.snapshot,
            sync_metadata=canvas.sync_metadata,
            operation_cursor=canvas.snapshot_operation_cursor,
        )
        return Response(self.get_serializer(canvas).data, status=201)

    @action(detail=True, methods=["get"])
    def proposals(self, request, pk=None):
        return Response(EntityProposalSerializer(self.get_object().entity_proposals.all(), many=True).data)

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        try:
            return Response(CanvasCommitService.preview(self.get_object(), request.data.get("proposal_ids", []), request.data.get("relation_proposal_ids", [])))
        except CanvasCommitError as exc:
            raise ValidationError(str(exc))

    @action(detail=True, methods=["post"])
    def commit(self, request, pk=None):
        canvas = self.get_object()
        member = membership(request.user, canvas.workspace)
        if not member or member.role == "reader": return Response({"detail":"需要 editor 权限"}, status=403)
        try:
            job, entities, relations = CanvasCommitService.commit(self.get_object(), request.data.get("proposal_ids", []), request.data.get("relation_proposal_ids", []), request.data.get("idempotency_key"), request.data.get("preview_token"))
        except (CanvasCommitError, TypeError, ValueError) as exc:
            raise ValidationError(str(exc))
        return Response({"job": CommitJobSerializer(job).data, "entities": EntitySerializer(entities, many=True).data, "relations": RelationSerializer(relations, many=True).data})


class EntityProposalViewSet(viewsets.ModelViewSet):
    serializer_class = EntityProposalSerializer
    http_method_names: ClassVar[list[str]] = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = EntityProposal.objects.prefetch_related("suggested_relations").filter(workspace_id__in=accessible_workspace_ids(self.request.user))
        for field in ["canvas", "workspace", "status"]:
            if self.request.query_params.get(field):
                qs = qs.filter(**{field: self.request.query_params[field]})
        return qs

    def perform_create(self, serializer):
        canvas = serializer.validated_data["canvas"]
        if not membership(self.request.user, canvas.workspace) or membership(self.request.user, canvas.workspace).role == "reader":
            raise ValidationError("需要 editor 权限")
        if serializer.validated_data["workspace"] != canvas.workspace:
            raise ValidationError("画布与世界观不匹配")
        serializer.save(source="user", status="pending", dialogue_session=None, source_message=None)

    def partial_update(self, request, *args, **kwargs):
        with transaction.atomic():
            proposal = self.get_object()
            member = membership(request.user, proposal.workspace)
            if not member or member.role == "reader": return Response({"detail":"需要 editor 权限"}, status=403)
            WorldWorkspace.objects.select_for_update().get(id=proposal.workspace_id)
            proposal.refresh_from_db()
            if proposal.status == "accepted":
                raise ValidationError("已提交提案不可编辑")
            if request.data.get("status", "pending") not in ["pending", "rejected", "superseded"]:
                raise ValidationError("接受提案必须通过预览与提交")
            allowed = {"title", "content", "metadata", "entity_type", "status"}
            serializer = self.get_serializer(proposal, data={k:v for k,v in request.data.items() if k in allowed}, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return Response(serializer.data)


class RelationProposalViewSet(viewsets.ModelViewSet):
    serializer_class = RelationProposalSerializer
    http_method_names: ClassVar[list[str]] = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = RelationProposal.objects.select_related("source_entity", "source_proposal", "target_proposal", "target_entity").filter(workspace_id__in=accessible_workspace_ids(self.request.user))
        if self.request.query_params.get("canvas"):
            qs = qs.filter(canvas_id=self.request.query_params["canvas"])
        return qs

    def perform_create(self, serializer):
        source = serializer.validated_data.get("source_proposal") or serializer.validated_data.get("source_entity")
        if not membership(self.request.user, source.workspace) or membership(self.request.user, source.workspace).role == "reader": raise ValidationError("需要 editor 权限")
        canvas = serializer.validated_data.get("canvas")
        if canvas is None:
            canvas = source.canvas if isinstance(source, EntityProposal) else StagingCanvas.objects.filter(workspace=source.workspace, purpose=StagingCanvas.Purpose.GRAPH, branch=source.branch).first()
        if canvas is None or canvas.workspace_id != source.workspace_id:
            raise ValidationError("必须绑定同一世界观的画布")
        if isinstance(source, EntityProposal) and canvas.purpose != StagingCanvas.Purpose.STAGING:
            raise ValidationError("实体提案只能来自灵感暂存画布")
        serializer.save(workspace=source.workspace, canvas=canvas, status="pending")

    def partial_update(self, request, *args, **kwargs):
        with transaction.atomic():
            obj = self.get_object()
            member = membership(request.user, obj.workspace)
            if not member or member.role == "reader": return Response({"detail":"需要 editor 权限"}, status=403)
            WorldWorkspace.objects.select_for_update().get(id=obj.workspace_id)
            obj.refresh_from_db()
            if obj.status == "accepted" or request.data.get("status", "pending") not in ["pending", "rejected"]:
                raise ValidationError("已提交关系不可编辑，接受关系请通过提交")
            allowed = {"relation_type", "properties", "reason", "status", "target_entity", "target_proposal", "time_system", "valid_from", "valid_to"}
            payload = {k: v for k, v in request.data.items() if k in allowed}
            # Changing one endpoint must clear the previous endpoint. This
            # makes a PATCH deterministic instead of accidentally retaining
            # both foreign keys on the model.
            if "target_proposal" in payload and "target_entity" not in payload:
                payload["target_entity"] = None
            elif "target_entity" in payload and "target_proposal" not in payload:
                payload["target_proposal"] = None
            serializer = self.get_serializer(obj, data=payload, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
        return Response(serializer.data)


class DialogueSessionViewSet(viewsets.ModelViewSet):
    serializer_class = DialogueSessionSerializer
    http_method_names: ClassVar[list[str]] = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):
        workspace = serializer.validated_data["workspace"]
        if not membership(self.request.user, workspace) or membership(self.request.user, workspace).role == "reader": raise ValidationError("需要 editor 权限")
        serializer.save()

    def get_queryset(self):
        qs = DialogueSession.objects.prefetch_related("messages").filter(workspace_id__in=accessible_workspace_ids(self.request.user))
        for field in ["workspace", "canvas"]:
            if self.request.query_params.get(field):
                qs = qs.filter(**{field: self.request.query_params[field]})
        return qs


class DialogueMessageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DialogueMessageSerializer
    def get_queryset(self):
        return DialogueMessage.objects.filter(session_id=self.request.query_params.get("session"), session__workspace_id__in=accessible_workspace_ids(self.request.user))
