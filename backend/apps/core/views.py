from django.conf import settings
from uuid import UUID
from collections import deque
from fractions import Fraction
from django.db.models import Exists, OuterRef, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.views import APIView
from .models import CharacterLifespan, CommitJob, Entity, GraphProjectionJob, Relation, TimeSystem, TimeSystemConversion, TimelineEntry, WorldBranch, WorldWorkspace
from apps.accounts.permissions import accessible_workspace_ids, membership
from .serializers import CommitJobSerializer, EntitySerializer, RelationSerializer, WorldWorkspaceSerializer
from .temporal_serializers import CharacterLifespanSerializer, TimeSystemConversionSerializer, TimeSystemSerializer, TimelineEntrySerializer
from .services.graph import GraphProvider
from .services.consistency import build_consistency_report
from .services.branching import (
    branch_entity_queryset, branch_relation_queryset, capture_main_snapshot, materialize_branch_baseline,
    effective_entities, effective_entity_map, effective_relations, map_entity_id_to_effective,
    resolve_entity_for_branch,
    resolve_entity_for_branch_write, resolve_relation_for_branch,
    resolve_relation_for_branch_write,
)
from apps.version_control.services.git_sync import GitRepositoryService
from apps.ai_agent.providers import ProviderError, configured_provider
from apps.canvas.models import CanvasContainer, EntityCanvasReference, StagingCanvas


def _find_branch(workspace, key, *, active_only=False):
    queryset = WorldBranch.objects.filter(workspace=workspace)
    if active_only:
        queryset = queryset.filter(status=WorldBranch.Status.ACTIVE)
    branch = queryset.filter(name=key).first()
    if branch:
        return branch
    try:
        branch_id = UUID(str(key))
    except (TypeError, ValueError, AttributeError):
        return None
    return queryset.filter(id=branch_id).first()


class WorkspaceViewSet(viewsets.ModelViewSet):
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = WorldWorkspace.objects.all()
    serializer_class = WorldWorkspaceSerializer

    def get_queryset(self):
        return WorldWorkspace.objects.filter(id__in=accessible_workspace_ids(self.request.user))

    def perform_create(self, serializer):
        workspace = serializer.save()
        workspace.repo_path = str((settings.WORLD_REPOS_ROOT / workspace.slug).resolve())
        workspace.save(update_fields=["repo_path", "updated_at"])
        from apps.accounts.models import WorkspaceMembership
        WorkspaceMembership.objects.create(workspace=workspace, user=self.request.user, role="owner")
        WorldBranch.objects.create(workspace=workspace, name="main")

    def partial_update(self, request, *args, **kwargs):
        workspace = self.get_object()
        from apps.accounts.models import WorkspaceMembership
        if not WorkspaceMembership.objects.filter(
            workspace=workspace, user=request.user, role=WorkspaceMembership.Role.OWNER
        ).exists():
            return Response({"detail": "只有 owner 可以修改工作台配置"}, status=403)
        serializer = self.get_serializer(workspace, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class GraphPathView(APIView):
    def get(self, request):
        workspace_id = request.query_params.get("workspace")
        start = request.query_params.get("from")
        end = request.query_params.get("to")
        branch_key = request.query_params.get("branch", "main")
        workspace = WorldWorkspace.objects.filter(
            id=workspace_id, id__in=accessible_workspace_ids(request.user)
        ).first()
        if not workspace:
            return Response({"detail": "workspace not found"}, status=404)
        if not start or not end:
            return Response({"detail": "from and to are required"}, status=400)
        branch = _find_branch(workspace, branch_key, active_only=True) if branch_key != "main" else None
        if branch_key != "main" and not branch:
            return Response({"detail": "branch not found"}, status=404)

        from apps.core.services.neo4j_projection import Neo4jProjection
        try:
            result = Neo4jProjection().shortest_path(workspace, start, end, branch)
            if result:
                return Response({"path": result, "source": "neo4j"})
        except Exception:
            # PostgreSQL is authoritative and remains a safe fallback while
            # Neo4j is unavailable or its projection is rebuilding.
            pass

        entities = effective_entities(workspace, branch)
        _, logical_map, _ = effective_entity_map(workspace, branch)
        start_entity = resolve_entity_for_branch(start, workspace, branch)
        end_entity = resolve_entity_for_branch(end, workspace, branch)
        if not start_entity or not end_entity:
            return Response({"path": None, "source": "postgresql"})
        start_id, end_id = str(start_entity.id), str(end_entity.id)
        allowed = {str(entity.id) for entity in entities}
        if start_id not in allowed or end_id not in allowed:
            return Response({"path": None, "source": "postgresql"})

        adjacency = {}
        for relation in effective_relations(workspace, branch):
            source = str(map_entity_id_to_effective(relation.source_id, logical_map))
            target = str(map_entity_id_to_effective(relation.target_id, logical_map))
            if source not in allowed or target not in allowed:
                continue
            item = (target, relation.relation_type, str(relation.id))
            reverse = (source, relation.relation_type, str(relation.id))
            adjacency.setdefault(source, []).append(item)
            adjacency.setdefault(target, []).append(reverse)

        queue = [start_id]
        previous = {start_id: None}
        for node in queue:
            if node == end_id:
                break
            for neighbor, kind, relation_id in adjacency.get(node, []):
                if neighbor not in previous:
                    previous[neighbor] = (node, kind, relation_id)
                    queue.append(neighbor)
            if len(previous) > 10000:
                break
        if end_id not in previous:
            return Response({"path": None, "source": "postgresql"})

        ids, current = [], end_id
        while current is not None:
            ids.append(current)
            current = previous[current][0] if previous[current] else None
        ids.reverse()
        entity_map = {str(entity.id): entity for entity in entities}
        return Response({
            "path": {
                "nodes": [{"id": entity_id, "title": entity_map[entity_id].title, "type": entity_map[entity_id].type} for entity_id in ids],
                "edges": [{"type": previous[entity_id][1], "id": previous[entity_id][2]} for entity_id in ids[1:]],
            },
            "source": "postgresql",
        })


class GraphImpactView(APIView):
    def get(self, request):
        workspace_id = request.query_params.get("workspace")
        entity_id = request.query_params.get("entity")
        branch_key = request.query_params.get("branch", "main")
        workspace = WorldWorkspace.objects.filter(
            id=workspace_id, id__in=accessible_workspace_ids(request.user)
        ).first()
        if not workspace:
            return Response({"detail": "workspace not found"}, status=404)
        if not entity_id:
            return Response({"detail": "entity is required"}, status=400)
        branch = _find_branch(workspace, branch_key, active_only=True) if branch_key != "main" else None
        if branch_key != "main" and not branch:
            return Response({"detail": "branch not found"}, status=404)

        from apps.core.services.neo4j_projection import Neo4jProjection
        try:
            return Response({"items": Neo4jProjection().impact(workspace, entity_id, branch), "source": "neo4j"})
        except Exception:
            pass

        entity = resolve_entity_for_branch(entity_id, workspace, branch)
        if not entity:
            return Response({"detail": "entity not found"}, status=404)
        entities = effective_entities(workspace, branch)
        _, logical_map, _ = effective_entity_map(workspace, branch)
        visible = {row.id for row in entities}
        items = {}
        effective_id = entity.id
        for relation in effective_relations(workspace, branch):
            source_id = map_entity_id_to_effective(relation.source_id, logical_map)
            target_id = map_entity_id_to_effective(relation.target_id, logical_map)
            if source_id == effective_id:
                other_id = target_id
            elif target_id == effective_id:
                other_id = source_id
            else:
                continue
            if other_id not in visible:
                continue
            other = next((row for row in entities if row.id == other_id), None)
            if other:
                item = items.setdefault(str(other.id), {"id": str(other.id), "title": other.title, "degree": 0})
                item["degree"] += 1
        return Response({"items": sorted(items.values(), key=lambda item: (-item["degree"], item["title"])), "source": "postgresql"})

class ConsistencyReportView(APIView):
    """Return a deterministic, read-only health report for a world view."""

    def get(self, request, workspace_id):
        workspace = WorldWorkspace.objects.filter(
            id=workspace_id, id__in=accessible_workspace_ids(request.user)
        ).first()
        if not workspace:
            return Response({"detail": "workspace not found"}, status=404)
        branch_key = request.query_params.get("branch", "main")
        branch = None
        if branch_key and branch_key != "main":
            branch = _find_branch(workspace, branch_key, active_only=True)
            if not branch:
                return Response({"detail": "branch not found"}, status=404)
        return Response(build_consistency_report(workspace, branch))


class ProjectionStatusView(APIView):
    def get(self,request,workspace_id):
        ws=WorldWorkspace.objects.filter(id=workspace_id,id__in=accessible_workspace_ids(request.user)).first()
        if not ws:return Response({"detail":"workspace not found"},status=404)
        from apps.core.services.neo4j_projection import Neo4jProjection
        job=GraphProjectionJob.objects.filter(workspace=ws).first()
        return Response({**Neo4jProjection().health(),"last_job":{"id":str(job.id),"status":job.status,"attempts":job.attempts,"error":job.error_message} if job else None})
    def post(self,request,workspace_id):
        ws=WorldWorkspace.objects.filter(id=workspace_id,id__in=accessible_workspace_ids(request.user)).first()
        if not ws:return Response({"detail":"workspace not found"},status=404)
        if not membership(request.user,ws) or membership(request.user,ws).role!="owner":return Response({"detail":"owner required"},status=403)
        job=GraphProjectionJob.objects.create(workspace=ws)
        from apps.core.services.neo4j_projection import Neo4jProjection
        ok,error=Neo4jProjection().process_job(job);job.refresh_from_db()
        return Response({"status":job.status,"error":error},status=200 if ok else 503)


class WorldBranchViewSet(viewsets.ModelViewSet):
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = WorldBranch.objects.all()
    serializer_class = None

    def get_queryset(self):
        qs = WorldBranch.objects.filter(workspace_id__in=accessible_workspace_ids(self.request.user))
        wid = self.request.query_params.get("workspace")
        return qs.filter(workspace_id=wid) if wid else qs

    @staticmethod
    def _payload(branch):
        return {
            "id": str(branch.id),
            "workspace": str(branch.workspace_id),
            "name": branch.name,
            "status": branch.status,
            "base_commit": branch.base_commit,
            "git_ref": branch.git_ref,
            "created_at": branch.created_at,
            "merged_at": branch.merged_at,
        }

    def list(self, request, *args, **kwargs):
        return Response([self._payload(branch) for branch in self.get_queryset()])

    def retrieve(self, request, *args, **kwargs):
        return Response(self._payload(self.get_object()))

    def create(self, request, *args, **kwargs):
        wid = request.data.get("workspace")
        name = str(request.data.get("name", "")).strip()
        ws = WorldWorkspace.objects.filter(
            id=wid,
            id__in=accessible_workspace_ids(request.user),
        ).first()
        member = membership(request.user, ws) if ws else None
        if not ws:
            return Response({"detail": "workspace not found"}, status=404)
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        if not name or name == "main" or len(name) > 120:
            return Response({"detail": "分支名无效"}, status=400)
        from uuid import uuid4
        branch = None
        try:
            # Serialize the baseline capture with commits/merges. The unique
            # constraint remains the final arbiter for concurrent names.
            with transaction.atomic():
                ws = WorldWorkspace.objects.select_for_update().get(pk=ws.pk)
                if WorldBranch.objects.filter(workspace=ws, name=name).exists():
                    return Response({"detail": "branch already exists"}, status=409)
                branch_id = uuid4()
                branch = WorldBranch(
                    id=branch_id,
                    workspace=ws,
                    name=name,
                    base_snapshot=capture_main_snapshot(ws),
                    git_ref=f"oc/branch/{branch_id.hex}",
                )
                branch.save(force_insert=True)
                materialize_branch_baseline(branch)
                branch.git_ref, branch.base_commit = GitRepositoryService(ws).prepare_branch(branch)
                branch.save(update_fields=["git_ref", "base_commit", "updated_at"])
        except IntegrityError:
            return Response({"detail": "branch already exists"}, status=409)
        except Exception as exc:
            return Response({"detail": f"无法创建 Git 分支：{exc}"}, status=503)
        return Response(self._payload(branch), status=201)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        branch = self.get_object()
        member = membership(request.user, branch.workspace)
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        if branch.name == "main":
            return Response({"detail": "main 分支不能归档"}, status=409)
        if branch.status == "archived":
            return Response({"id": str(branch.id), "status": branch.status, "idempotent": True})
        if branch.status != "active":
            return Response({"detail": "只有活动分支可归档"}, status=409)
        branch.status = "archived"
        branch.save(update_fields=["status", "updated_at"])
        return Response({"id": str(branch.id), "status": branch.status})

    @action(detail=True, methods=["post"], url_path="merge-preview")
    def merge_preview(self, request, pk=None):
        branch = self.get_object()
        if branch.name == "main" or branch.status != WorldBranch.Status.ACTIVE:
            return Response({"detail": "分支不能合并"}, status=409)
        return Response(_branch_merge_preview(branch))

    @action(detail=True, methods=["post"], url_path="merge-suggestion")
    def merge_suggestion(self, request, pk=None):
        """Return non-binding AI suggestions for the current merge preview.

        The preview is calculated before calling the provider and is returned
        on stale-token or provider errors.  The provider can therefore never
        partially mutate a merge, and the user can keep reviewing manually if
        the upstream model is unavailable.
        """
        branch = self.get_object()
        member = membership(request.user, branch.workspace)
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        if branch.name == "main" or branch.status != WorldBranch.Status.ACTIVE:
            return Response({"detail": "分支不能生成合并建议"}, status=409)

        preview = _branch_merge_preview(branch)
        requested_token = request.data.get("preview_token")
        if requested_token != preview["preview_token"]:
            return Response(
                {"detail": "预览已过期，请重新预览", "preview": preview},
                status=409,
            )

        from apps.ai_agent.services.merge_suggestions import MergeSuggestionService

        model = request.data.get("model", "")
        if not isinstance(model, str) or len(model) > 200:
            return Response({"detail": "model 必须是至多 200 字符的字符串"}, status=400)
        try:
            suggestions = MergeSuggestionService(configured_provider(branch.workspace)).suggest(
                branch_name=branch.name,
                preview=preview,
                model=model,
            )
        except ProviderError:
            return Response(
                {
                    "detail": "AI 建议暂不可用或格式无效；你的审核选择未改变，可继续人工处理。",
                    "status": "unavailable",
                    "suggestions": [],
                    "preview_token": preview["preview_token"],
                },
                status=503,
            )

        # Do not hold database locks across a slow upstream request. Recheck
        # membership and preview afterwards so stale suggestions cannot be used.
        branch = self.get_object()
        if membership(request.user, branch.workspace).role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        if branch.status != WorldBranch.Status.ACTIVE:
            return Response({"detail": "分支状态已变化，请重新预览"}, status=409)
        if _branch_merge_preview(branch)["preview_token"] != preview["preview_token"]:
            return Response({"detail": "生成建议期间内容已变化，请重新预览"}, status=409)

        return Response(
            {
                "status": "suggested",
                "suggestions": suggestions,
                "preview_token": preview["preview_token"],
                "model": model or settings.OPENAI_MODEL,
            }
        )

    @action(detail=True, methods=["post"])
    def merge(self, request, pk=None):
        branch = self.get_object()
        member = membership(request.user, branch.workspace)
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=403)
        if branch.status == WorldBranch.Status.MERGED:
            return Response({"status": "merged", "branch": str(branch.id), "idempotent": True})

        preview = _branch_merge_preview(branch)
        if request.data.get("preview_token") != preview["preview_token"]:
            return Response({"detail": "预览已过期，请重新预览", "preview": preview}, status=409)
        resolutions = request.data.get("resolutions", {}) or {}
        for conflict in preview["conflicts"]:
            key = conflict.get("branch_entity") or conflict.get("branch_relation")
            decision = resolutions.get(key)
            if conflict["kind"] == "duplicate_title":
                allowed = {"keep_both", "skip_branch_entity"}
                if decision not in allowed:
                    return Response({"detail": "每个冲突都必须由用户明确解决", "preview": preview}, status=400)
            elif conflict["kind"] in {"entity_update", "relation_update", "entity_deleted_in_main", "relation_deleted_in_main"}:
                # A legacy whole-row choice remains supported, but normal
                # conflicts can now be resolved field by field.  Every field
                # that both sides changed differently must be accounted for.
                allowed = {"keep_branch", "keep_main"}
                if isinstance(decision, str):
                    valid = decision in allowed
                elif isinstance(decision, dict):
                    valid = decision.get("mode") in allowed
                    field_selections = decision.get("fields") or {}
                    fields = ENTITY_MERGE_FIELDS if "entity" in conflict["kind"] else RELATION_MERGE_FIELDS
                    conflicting_fields = set(conflict.get("conflicting_fields") or [])
                    valid = valid or (
                        not (set(field_selections) - set(fields))
                        and conflicting_fields.issubset(field_selections)
                        and all(
                            (isinstance(value, str) and value in allowed)
                            or (isinstance(value, dict) and "value" in value)
                            for value in field_selections.values()
                        )
                    )
                else:
                    valid = False
                if not valid:
                    return Response({"detail": "每个冲突字段都必须由用户明确解决", "preview": preview}, status=400)
            else:
                return Response({"detail": "未知冲突类型", "preview": preview}, status=400)

        skip_entities = {
            key for key, value in resolutions.items()
            if value == "skip_branch_entity" or _resolution_mode(value) == "keep_main"
            and any(c.get("branch_entity") == key for c in preview["conflicts"])
        }
        skip_relations = {
            key for key, value in resolutions.items()
            if value == "skip_branch_entity" or _resolution_mode(value) == "keep_main"
            and any(c.get("branch_relation") == key for c in preview["conflicts"])
        }

        with transaction.atomic():
            WorldWorkspace.objects.select_for_update().get(pk=branch.workspace_id)
            branch = WorldBranch.objects.select_for_update().get(pk=branch.pk)
            if branch.status == WorldBranch.Status.MERGED:
                return Response({"status": "merged", "branch": str(branch.id), "idempotent": True})
            locked_preview = _branch_merge_preview(branch)
            if request.data.get("preview_token") != locked_preview["preview_token"]:
                return Response({"detail": "预览在确认期间已变化，请重新预览", "preview": locked_preview}, status=409)
            entity_conflicts = {
                item["branch_entity"]: item
                for item in locked_preview["conflicts"]
                if item.get("branch_entity")
            }
            relation_conflicts = {
                item["branch_relation"]: item
                for item in locked_preview["conflicts"]
                if item.get("branch_relation")
            }
            entity_changes_by_id = {
                item["id"]: item for item in locked_preview["entity_changes"]
            }
            relation_changes_by_id = {
                item["id"]: item for item in locked_preview["relation_changes"]
            }

            # First apply copy-on-write entity changes.  Branch override rows
            # stay on the branch as audit history; only branch-local additions
            # are promoted to main.
            branch_entities = list(
                Entity.objects.select_for_update().filter(workspace=branch.workspace, branch=branch).order_by("id")
            )
            changed_entity_ids = {
                item["id"] for item in locked_preview["entity_changes"]
                if item["branch_changed"]
            }
            for row in branch_entities:
                row_key = str(row.id)
                if row_key in skip_entities:
                    continue
                # Materialized baseline rows are deliberately present on every
                # branch.  Only rows that differ from the captured baseline are
                # part of the merge change-set; otherwise a stale branch would
                # overwrite unrelated main edits.
                if row.base_entity_id and row_key not in changed_entity_ids:
                    continue
                if row.base_entity_id:
                    main = Entity.objects.select_for_update().filter(
                        pk=row.base_entity_id,
                        workspace=branch.workspace,
                        branch__isnull=True,
                    ).first()
                    if main is None:
                        if row.status == Entity.Status.ACTIVE:
                            row.base_entity = None
                            row.branch = None
                            row.save(update_fields=["base_entity", "branch", "updated_at"])
                        continue
                    conflict = entity_conflicts.get(row_key)
                    resolution = resolutions.get(row_key)
                    if conflict:
                        resolved = _resolve_three_way_fields(
                            conflict, resolution, ENTITY_MERGE_FIELDS,
                        )
                        if resolved is None:
                            raise ValueError("invalid entity merge resolution")
                    else:
                        resolved = entity_changes_by_id.get(row_key, {}).get("merge_candidate")
                    resolved = resolved or {
                        "type": row.type,
                        "title": row.title,
                        "content": row.content,
                        "metadata": row.metadata,
                        "status": row.status,
                    }
                    main.type = resolved["type"]
                    main.title = resolved["title"]
                    main.content = resolved["content"]
                    main.metadata = resolved["metadata"]
                    main.status = resolved["status"]
                    main.sync_status = Entity.SyncStatus.PENDING
                    main.save(update_fields=["type", "title", "content", "metadata", "status", "sync_status", "updated_at"])
                elif row.status == Entity.Status.ACTIVE:
                    row.branch = None
                    row.base_entity = None
                    row.sync_status = Entity.SyncStatus.PENDING
                    row.save(update_fields=["branch", "base_entity", "sync_status", "updated_at"])

            # Map branch entity ids (including overrides) to the main ids used
            # by promoted relations and temporal records.
            main_entities = list(Entity.objects.filter(workspace=branch.workspace, branch__isnull=True))
            main_ids = {row.id for row in main_entities}
            entity_to_main = {row.id: row.id for row in main_entities}
            for row in branch_entities:
                if row.base_entity_id:
                    entity_to_main[row.id] = row.base_entity_id
                elif row.branch_id is None:
                    entity_to_main[row.id] = row.id
            entity_to_main_by_str = {str(key): value for key, value in entity_to_main.items()}

            # Apply relation overrides, then promote branch-local relations.
            branch_relations = list(
                Relation.objects.select_for_update().filter(workspace=branch.workspace, branch=branch).order_by("id")
            )
            changed_relation_ids = {
                item["id"] for item in locked_preview["relation_changes"]
                if item["branch_changed"]
            }
            for row in branch_relations:
                row_key = str(row.id)
                if row_key in skip_relations:
                    continue
                if row.base_relation_id and row_key not in changed_relation_ids:
                    continue
                source_id = entity_to_main.get(row.source_id, row.source_id)
                target_id = entity_to_main.get(row.target_id, row.target_id)
                if row.base_relation_id:
                    main = Relation.objects.select_for_update().filter(
                        pk=row.base_relation_id,
                        workspace=branch.workspace,
                        branch__isnull=True,
                    ).first()
                    if main is None:
                        if not row.archived and source_id in main_ids and target_id in main_ids:
                            row.source_id = source_id
                            row.target_id = target_id
                            row.base_relation = None
                            row.branch = None
                            row.save(update_fields=["source", "target", "base_relation", "branch", "updated_at"])
                        continue
                    conflict = relation_conflicts.get(row_key)
                    resolution = resolutions.get(row_key)
                    if conflict:
                        resolved = _resolve_three_way_fields(
                            conflict, resolution, RELATION_MERGE_FIELDS,
                        )
                        if resolved is None:
                            raise ValueError("invalid relation merge resolution")
                        resolved_source = resolved["source_id"]
                        resolved_target = resolved["target_id"]
                        source_id = entity_to_main_by_str.get(str(resolved_source), resolved_source)
                        target_id = entity_to_main_by_str.get(str(resolved_target), resolved_target)
                    else:
                        resolved = relation_changes_by_id.get(row_key, {}).get("merge_candidate")
                    resolved = resolved or {
                        "source_id": source_id,
                        "target_id": target_id,
                        "relation_type": row.relation_type,
                        "properties": row.properties,
                        "weight": row.weight,
                        "time_system_id": row.time_system_id,
                        "valid_from": row.valid_from,
                        "valid_to": row.valid_to,
                        "archived": row.archived,
                    }
                    main.source_id = entity_to_main_by_str.get(str(resolved["source_id"]), resolved["source_id"])
                    main.target_id = entity_to_main_by_str.get(str(resolved["target_id"]), resolved["target_id"])
                    main.relation_type = resolved["relation_type"]
                    main.properties = resolved["properties"]
                    main.weight = resolved["weight"]
                    main.time_system_id = resolved["time_system_id"]
                    main.valid_from = resolved["valid_from"]
                    main.valid_to = resolved["valid_to"]
                    main.archived = resolved["archived"]
                    main.save(update_fields=[
                        "source", "target", "relation_type", "properties", "weight",
                        "time_system", "valid_from", "valid_to", "archived", "updated_at",
                    ])
                elif not row.archived and source_id in main_ids and target_id in main_ids:
                    row.source_id = source_id
                    row.target_id = target_id
                    row.branch = None
                    row.base_relation = None
                    row.save(update_fields=["source", "target", "branch", "base_relation", "updated_at"])

            promoted = set(
                Entity.objects.filter(
                    workspace=branch.workspace,
                    branch__isnull=True,
                    status=Entity.Status.ACTIVE,
                ).values_list("id", flat=True)
            )
            # A branch-local lifespan is an explicit override of the inherited
            # main lifespan for the same character and chronology.
            for branch_span in CharacterLifespan.objects.select_for_update().filter(branch=branch):
                character_id = entity_to_main.get(branch_span.character_id, branch_span.character_id)
                if character_id not in promoted:
                    continue
                main_span = CharacterLifespan.objects.select_for_update().filter(
                    branch__isnull=True,
                    character_id=character_id,
                    time_system_id=branch_span.time_system_id,
                ).first()
                if branch_span.archived:
                    if main_span:
                        main_span.delete()
                    branch_span.delete()
                elif main_span:
                    main_span.start_value = branch_span.start_value
                    main_span.end_value = branch_span.end_value
                    main_span.properties = branch_span.properties
                    main_span.save(update_fields=["start_value", "end_value", "properties"])
                    branch_span.delete()
                else:
                    branch_span.character_id = character_id
                    branch_span.branch = None
                    branch_span.save(update_fields=["character", "branch", "updated_at"])

            # Same event on the same timeline is an overlay: branch values and
            # participant set replace the inherited main occurrence.
            for branch_entry in TimelineEntry.objects.select_for_update().filter(branch=branch).prefetch_related("participations"):
                timeline_id = entity_to_main.get(branch_entry.timeline_id, branch_entry.timeline_id)
                event_id = entity_to_main.get(branch_entry.event_id, branch_entry.event_id)
                if timeline_id not in promoted or event_id not in promoted:
                    continue
                main_entry = TimelineEntry.objects.select_for_update().filter(
                    branch__isnull=True,
                    timeline_id=timeline_id,
                    event_id=event_id,
                    time_system_id=branch_entry.time_system_id,
                ).first()
                if branch_entry.archived:
                    if main_entry:
                        main_entry.delete()
                    branch_entry.delete()
                elif main_entry:
                    main_entry.start_value = branch_entry.start_value
                    main_entry.end_value = branch_entry.end_value
                    main_entry.sequence = branch_entry.sequence
                    main_entry.summary = branch_entry.summary
                    main_entry.properties = branch_entry.properties
                    main_entry.timeline_id = timeline_id
                    main_entry.event_id = event_id
                    main_entry.save(update_fields=[
                        "timeline", "event", "start_value", "end_value", "sequence",
                        "summary", "properties", "updated_at",
                    ])
                    main_entry.participations.all().delete()
                    for participation in branch_entry.participations.all():
                        participant_id = entity_to_main.get(participation.entity_id, participation.entity_id)
                        if participant_id in promoted:
                            from .models import TimelineParticipation
                            TimelineParticipation.objects.get_or_create(
                                entry=main_entry,
                                entity_id=participant_id,
                                defaults={"role": participation.role},
                            )
                    branch_entry.delete()
                else:
                    branch_entry.timeline_id = timeline_id
                    branch_entry.event_id = event_id
                    branch_entry.branch = None
                    branch_entry.participations.exclude(entity_id__in=promoted).delete()
                    for participation in branch_entry.participations.all():
                        participant_id = entity_to_main.get(participation.entity_id, participation.entity_id)
                        if participant_id != participation.entity_id:
                            participation.entity_id = participant_id
                            participation.save(update_fields=["entity",])
                    branch_entry.save(update_fields=["timeline", "event", "branch", "updated_at"])

            # Containers and references are branch-scoped layout metadata. They
            # have stable identities, so merge promotes them in place after
            # semantic entity ids have been mapped to main. This keeps nested
            # parent links and canvas layouts intact without copying snapshots.
            branch_containers = list(CanvasContainer.objects.select_for_update().filter(branch=branch).order_by("created_at"))
            for container in branch_containers:
                container.branch = None
                container.canvas.branch = None
                container.canvas.save(update_fields=["branch", "updated_at"])
                container.save(update_fields=["branch", "updated_at"])
            for reference in EntityCanvasReference.objects.select_for_update().filter(branch=branch):
                mapped_entity = entity_to_main.get(reference.entity_id, reference.entity_id)
                if mapped_entity not in promoted:
                    reference.delete()
                    continue
                reference.entity_id = mapped_entity
                reference.branch = None
                reference.save(update_fields=["entity", "branch"])

            branch.status = WorldBranch.Status.MERGED
            branch.merged_at = timezone.now()
            branch.save(update_fields=["status", "merged_at", "updated_at"])
            export = GitRepositoryService(branch.workspace).snapshot()
            job = CommitJob.objects.create(
                workspace=branch.workspace,
                branch=None,
                idempotency_key=f"merge:{branch.id}",
                request_data={"merge_branch": str(branch.id)},
                export_data=export,
                status=CommitJob.Status.DATABASE_COMMITTED,
                entity_ids=[str(i) for i in Entity.objects.filter(workspace=branch.workspace, branch__isnull=True).values_list("id", flat=True)],
                relation_ids=[str(i) for i in Relation.objects.filter(workspace=branch.workspace, branch__isnull=True).values_list("id", flat=True)],
                actor=request.user.username,
            )
            projection_job = GraphProjectionJob.objects.create(
                workspace=branch.workspace,
                commit_job=job,
            )

        from apps.canvas.services.commit import CanvasCommitService
        CanvasCommitService.sync_pending(branch.workspace)
        from apps.core.services.neo4j_projection import Neo4jProjection
        projection = Neo4jProjection()
        projection_ok, projection_error = projection.process_job(projection_job)
        if not projection_ok and projection.uri and projection.password:
            CommitJob.objects.filter(pk=job.pk).update(
                status=CommitJob.Status.PROJECTION_SYNC_FAILED,
                error_message=projection_error,
            )
        job.refresh_from_db()
        return Response({"status": "merged", "branch": str(branch.id), "job": CommitJobSerializer(job).data})


def _entity_merge_fingerprint(row):
    return {
        "type": row.get("type"),
        "title": row.get("title"),
        "content": row.get("content"),
        "metadata": row.get("metadata") or {},
        "status": row.get("status"),
    }


ENTITY_MERGE_FIELDS = ("type", "title", "content", "metadata", "status")
RELATION_MERGE_FIELDS = (
    "source_id", "target_id", "relation_type", "properties", "weight",
    "time_system_id", "valid_from", "valid_to", "archived",
)


def _merge_field_value(row, field):
    """Return a JSON-friendly logical value for a merge field."""
    value = row.get(field)
    if field.endswith("_id") and value is not None:
        return str(value)
    return value


def _three_way_fields(base, main, branch, fields):
    """Calculate a field-level three-way merge.

    A field changed on only one side is safe to merge automatically.  A field
    changed to the same value on both sides is also safe.  Only fields changed
    differently by both sides require an explicit user choice.
    """
    field_changes = {}
    merged = {}
    conflicts = []
    for field in fields:
        base_value = _merge_field_value(base, field)
        main_value = _merge_field_value(main, field)
        branch_value = _merge_field_value(branch, field)
        if main_value == branch_value:
            state = "same"
            merged_value = main_value
        elif main_value == base_value:
            state = "branch_only"
            merged_value = branch_value
        elif branch_value == base_value:
            state = "main_only"
            merged_value = main_value
        else:
            state = "conflict"
            merged_value = main_value
            conflicts.append(field)
        field_changes[field] = {
            "base": base_value,
            "main": main_value,
            "branch": branch_value,
            "state": state,
        }
        merged[field] = merged_value
    # Archiving is a semantic delete. If one side archives an object while
    # the other side keeps it active and edits another field, selecting the
    # automatically merged row would silently resurrect or discard content.
    if "status" in fields and field_changes["status"]["main"] != field_changes["status"]["branch"]:
        if "status" not in conflicts and any(
            data["state"] in {"main_only", "branch_only", "conflict"}
            for name, data in field_changes.items()
            if name != "status"
        ):
            conflicts.append("status")
    if "archived" in fields and field_changes["archived"]["main"] != field_changes["archived"]["branch"]:
        if "archived" not in conflicts and any(
            data["state"] in {"main_only", "branch_only", "conflict"}
            for name, data in field_changes.items()
            if name != "archived"
        ):
            conflicts.append("archived")
    return field_changes, merged, conflicts


def _resolution_mode(resolution):
    if isinstance(resolution, str):
        return resolution
    if isinstance(resolution, dict):
        return resolution.get("mode")
    return None


def _resolve_three_way_fields(conflict, resolution, fields):
    """Apply a field-level resolution to a preview conflict.

    ``fields`` accepts ``keep_main``, ``keep_branch`` or
    ``{"value": <final value>}``.  The latter is the escape hatch used by a
    future UI to let a user edit the final field rather than merely selecting
    one side.
    """
    mode = _resolution_mode(resolution)
    if mode in {"keep_main", "keep_branch"}:
        row = conflict["main"] if mode == "keep_main" else conflict["branch"]
        return {
            field: _merge_field_value(row, field)
            for field in fields
        }
    if not isinstance(resolution, dict):
        return None
    selections = resolution.get("fields") or {}
    field_changes = conflict.get("field_changes") or {}
    if any(field not in selections for field, data in field_changes.items() if data["state"] == "conflict"):
        return None
    merged = dict(conflict.get("merge_candidate") or {})
    for field, selection in selections.items():
        if field not in fields:
            return None
        if isinstance(selection, str) and selection in {"keep_main", "keep_branch"}:
            row = conflict["main"] if selection == "keep_main" else conflict["branch"]
            merged[field] = _merge_field_value(row, field)
        elif isinstance(selection, dict) and "value" in selection:
            merged[field] = selection["value"]
        else:
            return None
    return merged


def _relation_merge_fingerprint(row, entity_id_map=None):
    entity_id_map = entity_id_map or {}
    source_id = row.get("source_id")
    target_id = row.get("target_id")
    return {
        # Branch relation rows point to branch materializations of inherited
        # entities. Compare logical ids so an untouched copied relation does
        # not look like a user edit merely because its physical FK differs.
        "source_id": str(entity_id_map.get(str(source_id), source_id)) if source_id else None,
        "target_id": str(entity_id_map.get(str(target_id), target_id)) if target_id else None,
        "relation_type": row.get("relation_type"),
        "properties": row.get("properties") or {},
        "weight": row.get("weight"),
        "time_system_id": str(row.get("time_system_id")) if row.get("time_system_id") else None,
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("valid_to"),
        "archived": row.get("archived", False),
    }


def _branch_merge_preview(branch):
    main_entities = list(Entity.objects.filter(workspace=branch.workspace, branch__isnull=True).order_by("id").values(
        "id", "type", "title", "content", "metadata", "status",
    ))
    main_by_id = {str(row["id"]): row for row in main_entities}
    baseline_entities = {
        str(row.get("id")): row
        for row in (branch.base_snapshot or {}).get("entities", [])
        if row.get("id")
    }
    main_titles = {str(row["title"]).casefold() for row in main_entities if row["status"] == Entity.Status.ACTIVE}
    branch_entities = list(Entity.objects.filter(branch=branch).order_by("id").values(
        "id", "base_entity_id", "type", "title", "content", "metadata", "status",
    ))
    additions = [
        {key: value for key, value in row.items() if key != "base_entity_id"}
        for row in branch_entities
        if row["base_entity_id"] is None and row["status"] == Entity.Status.ACTIVE
    ]
    entity_changes = []
    conflicts = []
    for row in branch_entities:
        if row["base_entity_id"] is None:
            if row["status"] == Entity.Status.ACTIVE and str(row["title"]).casefold() in main_titles:
                conflicts.append({
                    "branch_entity": str(row["id"]),
                    "title": row["title"],
                    "kind": "duplicate_title",
                })
            continue
        base_key = str(row["base_entity_id"])
        main = main_by_id.get(base_key)
        baseline = baseline_entities.get(base_key)
        # A missing main row is a change relative to the captured baseline.
        # This is important for a three-way merge when main was archived or
        # removed after the branch was created.
        main_changed = bool(
            baseline
            and (
                main is None
                or _entity_merge_fingerprint(main) != _entity_merge_fingerprint(baseline)
            )
        )
        branch_changed = bool(
            baseline and _entity_merge_fingerprint(row) != _entity_merge_fingerprint(baseline)
        )
        field_changes = {}
        merge_candidate = None
        conflicting_fields = []
        if baseline and main is not None and branch_changed:
            field_changes, merge_candidate, conflicting_fields = _three_way_fields(
                baseline, main, row, ENTITY_MERGE_FIELDS,
            )
        entity_changes.append({
            "id": str(row["id"]),
            "base_entity": base_key,
            "title": row["title"],
            "status": row["status"],
            "main_changed_since_branch": main_changed,
            "branch_changed": branch_changed,
            "field_changes": field_changes,
            "merge_candidate": merge_candidate,
            "auto_mergeable": bool(main_changed and branch_changed and not conflicting_fields and main is not None),
        })
        if main_changed and branch_changed and (main is None or conflicting_fields):
            conflicts.append({
                "branch_entity": str(row["id"]),
                "main_entity": base_key,
                "title": row["title"],
                "kind": "entity_deleted_in_main" if main is None else "entity_update",
                "base": baseline,
                "main": main,
                "branch": row,
                "field_changes": field_changes,
                "merge_candidate": merge_candidate,
                "conflicting_fields": conflicting_fields,
            })

    main_relations = list(Relation.objects.filter(workspace=branch.workspace, branch__isnull=True).order_by("id").values(
        "id", "source_id", "target_id", "relation_type", "properties", "weight",
        "time_system_id", "valid_from", "valid_to", "archived",
    ))
    main_relation_by_id = {str(row["id"]): row for row in main_relations}
    baseline_relations = {
        str(row.get("id")): row
        for row in (branch.base_snapshot or {}).get("relations", [])
        if row.get("id")
    }
    branch_relations = list(Relation.objects.filter(branch=branch).order_by("id").values(
        "id", "base_relation_id", "source_id", "target_id", "relation_type", "properties", "weight",
        "time_system_id", "valid_from", "valid_to", "archived",
    ))
    relation_changes = []
    branch_entity_logical_ids = {
        str(row["id"]): str(row["base_entity_id"] or row["id"])
        for row in branch_entities
    }
    for row in branch_relations:
        if row["base_relation_id"] is None:
            continue
        base_key = str(row["base_relation_id"])
        main = main_relation_by_id.get(base_key)
        baseline = baseline_relations.get(base_key)
        main_changed = bool(
            baseline
            and (
                main is None
                or _relation_merge_fingerprint(main) != _relation_merge_fingerprint(baseline)
            )
        )
        branch_changed = bool(
            baseline
            and _relation_merge_fingerprint(row, branch_entity_logical_ids)
            != _relation_merge_fingerprint(baseline)
        )
        field_changes = {}
        merge_candidate = None
        conflicting_fields = []
        if baseline and main is not None and branch_changed:
            main_for_merge = dict(main)
            branch_for_merge = dict(row)
            # Relations in a branch refer to materialized entity ids.  The
            # logical ids are the values users should see and resolve.
            for relation_row in (main_for_merge, branch_for_merge):
                relation_row["source_id"] = str(
                    branch_entity_logical_ids.get(str(relation_row.get("source_id")), relation_row.get("source_id"))
                )
                relation_row["target_id"] = str(
                    branch_entity_logical_ids.get(str(relation_row.get("target_id")), relation_row.get("target_id"))
                )
            field_changes, merge_candidate, conflicting_fields = _three_way_fields(
                baseline, main_for_merge, branch_for_merge, RELATION_MERGE_FIELDS,
            )
        relation_changes.append({
            "id": str(row["id"]),
            "base_relation": base_key,
            "main_changed_since_branch": main_changed,
            "branch_changed": branch_changed,
            "archived": row["archived"],
            "field_changes": field_changes,
            "merge_candidate": merge_candidate,
            "auto_mergeable": bool(main_changed and branch_changed and not conflicting_fields and main is not None),
        })
        if main_changed and branch_changed and (main is None or conflicting_fields):
            main_for_conflict = main
            branch_for_conflict = row
            if main is not None:
                main_for_conflict = main_for_merge
            branch_for_conflict = branch_for_merge
            conflicts.append({
                "branch_relation": str(row["id"]),
                "main_relation": base_key,
                "kind": "relation_deleted_in_main" if main is None else "relation_update",
                "base": baseline,
                "main": main_for_conflict,
                "branch": branch_for_conflict,
                "field_changes": field_changes,
                "merge_candidate": merge_candidate,
                "conflicting_fields": conflicting_fields,
            })

    conflicts.sort(key=lambda row: row.get("branch_entity") or row.get("branch_relation") or "")
    lifespan_overrides = list(CharacterLifespan.objects.filter(branch=branch).order_by("character_id", "time_system_id").values(
        "character_id", "time_system_id", "start_value", "end_value", "properties", "archived",
    ))
    entry_overrides = list(TimelineEntry.objects.filter(branch=branch).order_by("timeline_id", "event_id", "time_system_id").values(
        "id", "timeline_id", "event_id", "time_system_id", "start_value", "end_value", "summary", "sequence", "properties", "archived",
    ))
    from .models import TimelineParticipation
    for entry in entry_overrides:
        entry["participants"] = list(
            TimelineParticipation.objects.filter(entry_id=entry["id"]).order_by("entity_id").values("entity_id", "role")
        )
    container_changes = list(CanvasContainer.objects.filter(branch=branch).order_by("created_at").values(
        "id", "parent_id", "canvas_id", "name", "sort_order", "status"
    ))
    reference_changes = list(EntityCanvasReference.objects.filter(branch=branch).order_by("created_at").values(
        "id", "container_id", "entity_id"
    ))
    import hashlib
    import json
    body = {
        "branch": str(branch.id),
        "additions": additions,
        "entity_changes": entity_changes,
        "relation_changes": relation_changes,
        "conflicts": conflicts,
        "lifespan_overrides": lifespan_overrides,
        "timeline_overrides": entry_overrides,
        "container_changes": container_changes,
        "reference_changes": reference_changes,
    }
    token = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()
    return {**body, "preview_token": token}


class EntityViewSet(viewsets.ModelViewSet):
    serializer_class = EntitySerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def _workspace_for_request(self, instance=None):
        if instance is not None:
            return instance.workspace
        workspace_id = self.request.query_params.get("workspace") or self.request.data.get("workspace")
        if not workspace_id:
            return None
        return WorldWorkspace.objects.filter(
            id=workspace_id,
            id__in=accessible_workspace_ids(self.request.user),
        ).first()

    def _branch_for_request(self, instance=None, *, required=False):
        key = self.request.query_params.get("branch")
        if not key or key == "main":
            if required:
                raise ValidationError({"branch": "分支写入必须提供 branch 查询参数"})
            return None
        workspace = self._workspace_for_request(instance)
        if workspace is None:
            raise ValidationError({"workspace": "查询分支时必须提供 workspace"})
        branch = _find_branch(workspace, key, active_only=True)
        if branch is None:
            from rest_framework.exceptions import NotFound
            raise NotFound("branch not found")
        return branch

    def _require_editor(self, workspace):
        member = membership(self.request.user, workspace) if workspace else None
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=status.HTTP_403_FORBIDDEN)
        return None

    def get_queryset(self):
        queryset = Entity.objects.select_related("workspace", "base_entity").filter(
            workspace_id__in=accessible_workspace_ids(self.request.user),
            branch__isnull=True,
        )
        branch_key = self.request.query_params.get("branch")
        workspace_id = self.request.query_params.get("workspace")
        if branch_key and branch_key != "main":
            if not workspace_id:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"workspace": "workspace is required when querying a branch"})
            workspace = WorldWorkspace.objects.filter(
                id=workspace_id,
                id__in=accessible_workspace_ids(self.request.user),
            ).first()
            branch = _find_branch(workspace, branch_key, active_only=True) if workspace else None
            if not branch:
                from rest_framework.exceptions import NotFound
                raise NotFound("branch not found")
            queryset = branch_entity_queryset(workspace, branch).select_related("workspace", "base_entity")
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        entity_type = self.request.query_params.get("type")
        search = self.request.query_params.get("search")
        if entity_type:
            queryset = queryset.filter(type=entity_type)
        if search:
            queryset = queryset.filter(Q(title__icontains=search) | Q(content__icontains=search))
        return queryset

    def get_object(self):
        branch_key = self.request.query_params.get("branch")
        if branch_key and branch_key != "main":
            workspace = self._workspace_for_request()
            if workspace is None:
                workspace = Entity.objects.filter(
                    id=self.kwargs["pk"],
                    workspace_id__in=accessible_workspace_ids(self.request.user),
                ).select_related("workspace").first()
                workspace = workspace.workspace if workspace else None
            if workspace is not None:
                branch = _find_branch(workspace, branch_key, active_only=True)
                if branch is not None:
                    entity = resolve_entity_for_branch(self.kwargs["pk"], workspace, branch)
                    if entity is not None:
                        self.check_object_permissions(self.request, entity)
                        return entity
            from rest_framework.exceptions import NotFound
            raise NotFound("entity not found")
        return super().get_object()

    def create(self, request, *args, **kwargs):
        # Formal main entities still enter through reviewed proposals.  Direct
        # branch writes are intentionally allowed for editor maintenance work.
        if not request.query_params.get("branch") or request.query_params.get("branch") == "main":
            return Response({"detail": "正式实体必须通过提案审核提交；请在 branch 上创建工作版本"}, status=405)
        workspace = self._workspace_for_request()
        branch = self._branch_for_request(required=True)
        if workspace is None or branch is None or branch.workspace_id != workspace.id:
            return Response({"detail": "workspace 与 branch 不匹配"}, status=400)
        denied = self._require_editor(workspace)
        if denied:
            return denied
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        supplied_workspace = serializer.validated_data.get("workspace")
        if supplied_workspace and supplied_workspace.id != workspace.id:
            raise ValidationError({"workspace": "实体必须属于当前世界观"})
        entity = serializer.save(
            workspace=workspace,
            branch=branch,
            base_entity=None,
            sync_status=Entity.SyncStatus.PENDING,
        )
        return Response(self.get_serializer(entity).data, status=status.HTTP_201_CREATED, headers=self.get_success_headers(serializer.data))

    def _branch_write_target(self, branch, pk):
        return resolve_entity_for_branch_write(pk, branch.workspace, branch)

    @staticmethod
    def _copy_entity(base, branch):
        return Entity.objects.create(
            workspace=base.workspace,
            branch=branch,
            base_entity=base,
            type=base.type,
            title=base.title,
            content=base.content,
            metadata=base.metadata,
            status=base.status,
            git_path=base.git_path,
            commit_hash=base.commit_hash,
            sync_status=Entity.SyncStatus.PENDING,
        )

    def _mutate(self, request, pk, *, partial):
        branch = self._branch_for_request(required=True)
        denied = self._require_editor(branch.workspace)
        if denied:
            return denied
        with transaction.atomic():
            target = self._branch_write_target(branch, pk)
            if target is None:
                from rest_framework.exceptions import NotFound
                raise NotFound("entity not found")
            if target.branch_id != branch.id:
                base = Entity.objects.select_for_update().get(pk=target.pk)
                target = Entity.objects.filter(branch=branch, base_entity=base).first() or self._copy_entity(base, branch)
            serializer = self.get_serializer(target, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)
            supplied_workspace = serializer.validated_data.get("workspace")
            if supplied_workspace and supplied_workspace.id != branch.workspace_id:
                raise ValidationError({"workspace": "实体必须属于当前世界观"})
            serializer.save(workspace=branch.workspace, branch=branch, sync_status=Entity.SyncStatus.PENDING)
        return Response(self.get_serializer(target).data)

    def update(self, request, *args, **kwargs):
        return self._mutate(request, kwargs["pk"], partial=False)

    def partial_update(self, request, *args, **kwargs):
        return self._mutate(request, kwargs["pk"], partial=True)

    def destroy(self, request, *args, **kwargs):
        # Main records are archived rather than physically deleted so branch
        # base pointers remain stable and three-way merge can detect deletion.
        if not request.query_params.get("branch") or request.query_params.get("branch") == "main":
            target = self.get_object()
            denied = self._require_editor(target.workspace)
            if denied:
                return denied
            target.status = Entity.Status.ARCHIVED
            target.sync_status = Entity.SyncStatus.PENDING
            target.save(update_fields=["status", "sync_status", "updated_at"])
            return Response(status=status.HTTP_204_NO_CONTENT)
        branch = self._branch_for_request(required=True)
        denied = self._require_editor(branch.workspace)
        if denied:
            return denied
        with transaction.atomic():
            target = self._branch_write_target(branch, kwargs["pk"])
            if target is None:
                from rest_framework.exceptions import NotFound
                raise NotFound("entity not found")
            if target.branch_id != branch.id:
                base = Entity.objects.select_for_update().get(pk=target.pk)
                target = Entity.objects.filter(branch=branch, base_entity=base).first() or self._copy_entity(base, branch)
            target.status = Entity.Status.ARCHIVED
            target.sync_status = Entity.SyncStatus.PENDING
            target.save(update_fields=["status", "sync_status", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        branch = self._branch_for_request(required=True)
        denied = self._require_editor(branch.workspace)
        if denied:
            return denied
        with transaction.atomic():
            target = self._branch_write_target(branch, pk)
            if target is None:
                from rest_framework.exceptions import NotFound
                raise NotFound("entity not found")
            if target.branch_id != branch.id:
                base = Entity.objects.select_for_update().get(pk=target.pk)
                target = Entity.objects.filter(branch=branch, base_entity=base).first() or self._copy_entity(base, branch)
            target.status = Entity.Status.ACTIVE
            target.sync_status = Entity.SyncStatus.PENDING
            target.save(update_fields=["status", "sync_status", "updated_at"])
        return Response(self.get_serializer(target).data)


class RelationViewSet(viewsets.ModelViewSet):
    serializer_class = RelationSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def _workspace_for_request(self, instance=None):
        if instance is not None:
            return instance.workspace
        workspace_id = self.request.query_params.get("workspace") or self.request.data.get("workspace")
        if workspace_id:
            return WorldWorkspace.objects.filter(
                id=workspace_id,
                id__in=accessible_workspace_ids(self.request.user),
            ).first()
        source_id = self.request.data.get("source")
        return Entity.objects.filter(
            id=source_id,
            workspace_id__in=accessible_workspace_ids(self.request.user),
        ).select_related("workspace").first().workspace if source_id and Entity.objects.filter(id=source_id, workspace_id__in=accessible_workspace_ids(self.request.user)).exists() else None

    def _branch_for_request(self, instance=None, *, required=False):
        key = self.request.query_params.get("branch")
        if not key or key == "main":
            if required:
                raise ValidationError({"branch": "分支写入必须提供 branch 查询参数"})
            return None
        workspace = self._workspace_for_request(instance)
        if workspace is None:
            raise ValidationError({"workspace": "查询分支时必须提供 workspace"})
        branch = _find_branch(workspace, key, active_only=True)
        if branch is None:
            from rest_framework.exceptions import NotFound
            raise NotFound("branch not found")
        return branch

    def _require_editor(self, workspace):
        member = membership(self.request.user, workspace) if workspace else None
        if not member or member.role == "reader":
            return Response({"detail": "需要 editor 权限"}, status=status.HTTP_403_FORBIDDEN)
        return None

    def get_queryset(self):
        queryset = Relation.objects.select_related(
            "source", "target", "workspace", "base_relation",
        ).filter(
            workspace_id__in=accessible_workspace_ids(self.request.user),
            branch__isnull=True,
            archived=False,
        )
        branch_key = self.request.query_params.get("branch")
        workspace_id = self.request.query_params.get("workspace")
        if branch_key and branch_key != "main":
            if not workspace_id:
                raise ValidationError({"workspace": "workspace is required when querying a branch"})
            workspace = WorldWorkspace.objects.filter(
                id=workspace_id,
                id__in=accessible_workspace_ids(self.request.user),
            ).first()
            branch = _find_branch(workspace, branch_key, active_only=True) if workspace else None
            if not branch:
                from rest_framework.exceptions import NotFound
                raise NotFound("branch not found")
            queryset = branch_relation_queryset(workspace, branch).select_related(
                "source", "target", "workspace", "base_relation",
            )
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        return queryset

    def get_object(self):
        branch_key = self.request.query_params.get("branch")
        if branch_key and branch_key != "main":
            workspace = self._workspace_for_request()
            if workspace is None:
                raw = Relation.objects.filter(
                    id=self.kwargs["pk"],
                    workspace_id__in=accessible_workspace_ids(self.request.user),
                ).select_related("workspace").first()
                workspace = raw.workspace if raw else None
            if workspace is not None:
                branch = _find_branch(workspace, branch_key, active_only=True)
                if branch is not None:
                    relation = resolve_relation_for_branch(self.kwargs["pk"], workspace, branch)
                    if relation is not None:
                        self.check_object_permissions(self.request, relation)
                        return relation
            from rest_framework.exceptions import NotFound
            raise NotFound("relation not found")
        return super().get_object()

    def _validate_endpoints(self, branch, source, target):
        if source.workspace_id != branch.workspace_id or target.workspace_id != branch.workspace_id:
            raise ValidationError("关系两端必须属于当前世界观")
        for entity in (source, target):
            if entity.status != Entity.Status.ACTIVE or entity.branch_id not in (None, branch.id):
                raise ValidationError("关系端点必须是当前分支中的有效实体")

    def create(self, request, *args, **kwargs):
        if not request.query_params.get("branch") or request.query_params.get("branch") == "main":
            return Response({"detail": "正式关系必须通过提案审核提交；请在 branch 上创建工作版本"}, status=405)
        branch = self._branch_for_request(required=True)
        denied = self._require_editor(branch.workspace)
        if denied:
            return denied
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = serializer.validated_data["source"]
        target = serializer.validated_data["target"]
        self._validate_endpoints(branch, source, target)
        if source.branch_id != branch.id:
            source = ensure_branch_entity_dependency(branch, source)
        if target.branch_id != branch.id:
            target = ensure_branch_entity_dependency(branch, target)
        axis = serializer.validated_data.get("time_system")
        if axis and axis.workspace_id != branch.workspace_id:
            raise ValidationError({"time_system": "必须属于当前世界观"})
        relation = serializer.save(
            workspace=branch.workspace,
            branch=branch,
            source=source,
            target=target,
            base_relation=None,
            archived=False,
        )
        return Response(self.get_serializer(relation).data, status=status.HTTP_201_CREATED, headers=self.get_success_headers(serializer.data))

    @staticmethod
    def _copy_relation(base, branch):
        # Keep the branch relation physically isolated from moving main entity
        # rows. This matters when a main entity is later archived or removed;
        # the branch override must remain auditable until merge.
        source = ensure_branch_entity_dependency(branch, base.source)
        target = ensure_branch_entity_dependency(branch, base.target)
        return Relation.objects.create(
            workspace=base.workspace,
            branch=branch,
            base_relation=base,
            time_system=base.time_system,
            source=source,
            target=target,
            relation_type=base.relation_type,
            properties=base.properties,
            weight=base.weight,
            valid_from=base.valid_from,
            valid_to=base.valid_to,
            archived=base.archived,
        )

    def _mutate(self, request, pk, *, partial):
        branch = self._branch_for_request(required=True)
        denied = self._require_editor(branch.workspace)
        if denied:
            return denied
        with transaction.atomic():
            target = resolve_relation_for_branch_write(pk, branch.workspace, branch)
            if target is None:
                from rest_framework.exceptions import NotFound
                raise NotFound("relation not found")
            if target.branch_id != branch.id:
                base = Relation.objects.select_for_update().get(pk=target.pk)
                target = Relation.objects.filter(branch=branch, base_relation=base).first() or self._copy_relation(base, branch)
            serializer = self.get_serializer(target, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)
            source = serializer.validated_data.get("source", target.source)
            target_entity = serializer.validated_data.get("target", target.target)
            self._validate_endpoints(branch, source, target_entity)
            axis = serializer.validated_data.get("time_system", target.time_system)
            if axis and axis.workspace_id != branch.workspace_id:
                raise ValidationError({"time_system": "必须属于当前世界观"})
            serializer.save(workspace=branch.workspace, branch=branch, archived=False)
        return Response(self.get_serializer(target).data)

    def update(self, request, *args, **kwargs):
        return self._mutate(request, kwargs["pk"], partial=False)

    def partial_update(self, request, *args, **kwargs):
        return self._mutate(request, kwargs["pk"], partial=True)

    def destroy(self, request, *args, **kwargs):
        if not request.query_params.get("branch") or request.query_params.get("branch") == "main":
            target = self.get_object()
            denied = self._require_editor(target.workspace)
            if denied:
                return denied
            target.archived = True
            target.save(update_fields=["archived", "updated_at"])
            return Response(status=status.HTTP_204_NO_CONTENT)
        branch = self._branch_for_request(required=True)
        denied = self._require_editor(branch.workspace)
        if denied:
            return denied
        with transaction.atomic():
            target = resolve_relation_for_branch_write(kwargs["pk"], branch.workspace, branch)
            if target is None:
                from rest_framework.exceptions import NotFound
                raise NotFound("relation not found")
            if target.branch_id != branch.id:
                base = Relation.objects.select_for_update().get(pk=target.pk)
                target = Relation.objects.filter(branch=branch, base_relation=base).first() or self._copy_relation(base, branch)
            target.archived = True
            target.save(update_fields=["archived", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        branch = self._branch_for_request(required=True)
        denied = self._require_editor(branch.workspace)
        if denied:
            return denied
        with transaction.atomic():
            target = resolve_relation_for_branch_write(pk, branch.workspace, branch)
            if target is None:
                from rest_framework.exceptions import NotFound
                raise NotFound("relation not found")
            if target.branch_id != branch.id:
                base = Relation.objects.select_for_update().get(pk=target.pk)
                target = Relation.objects.filter(branch=branch, base_relation=base).first() or self._copy_relation(base, branch)
            target.archived = False
            target.save(update_fields=["archived", "updated_at"])
        return Response(self.get_serializer(target).data)


class GraphView(APIView):

    def get(self, request):
        workspace_id = request.query_params.get("workspace")
        if not workspace_id:
            return Response({"detail": "workspace query parameter is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            workspace = WorldWorkspace.objects.get(id=workspace_id, id__in=accessible_workspace_ids(request.user))
        except WorldWorkspace.DoesNotExist:
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)
        branch_key = request.query_params.get("branch")
        if branch_key and branch_key != "main" and not _find_branch(workspace, branch_key, active_only=True):
            return Response({"detail":"branch not found"}, status=404)
        return Response(GraphProvider().graph(workspace, request.query_params.get("type"), branch_key))


class EntityLinksView(APIView):

    def get(self, request, entity_id):
        try:
            entity = Entity.objects.select_related("workspace").get(id=entity_id, workspace_id__in=accessible_workspace_ids(request.user))
        except Entity.DoesNotExist:
            return Response({"detail": "entity not found"}, status=status.HTTP_404_NOT_FOUND)
        branch_key=request.query_params.get("branch")
        if entity.branch_id and (not branch_key or branch_key=="main"):
            return Response({"detail":"entity not found"},status=404)
        if branch_key and branch_key != "main" and not _find_branch(entity.workspace, branch_key, active_only=True):
            return Response({"detail":"branch not found"},status=404)
        return Response(GraphProvider().links(entity, branch_key))


class GitStatusView(APIView):
    def get(self, request, workspace_id):
        workspace = WorldWorkspace.objects.filter(
            id=workspace_id, id__in=accessible_workspace_ids(request.user)
        ).first()
        if not workspace:
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)
        from apps.version_control.services.git_sync import GitRepositoryService
        payload = GitRepositoryService(workspace).status()
        pending = CommitJob.objects.filter(
            workspace=workspace,
            status__in=[
                CommitJob.Status.DATABASE_COMMITTED,
                CommitJob.Status.GIT_SYNCING,
                CommitJob.Status.GIT_SYNC_FAILED,
                CommitJob.Status.PROJECTION_SYNC_FAILED,
            ],
        ).values("status").order_by("status")
        counts = {}
        for row in pending:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        payload["pending_jobs"] = counts
        return Response(payload)


class CommitHistoryView(APIView):

    def get(self, request, workspace_id):
        try:
            workspace = WorldWorkspace.objects.get(id=workspace_id, id__in=accessible_workspace_ids(request.user))
        except WorldWorkspace.DoesNotExist:
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)
        service = GitRepositoryService(workspace)
        branch_key = self.request.query_params.get("branch")
        ref = None
        if branch_key and branch_key != "main":
            branch = _find_branch(workspace, branch_key)
            if not branch:
                return Response({"detail": "branch not found"}, status=404)
            ref = branch.git_ref or f"oc/branch/{branch.pk.hex}"
        return Response({"history": service.history(ref=ref)})


class TemporalDiffView(APIView):
    def _workspace(self, request, workspace_id):
        try:
            return WorldWorkspace.objects.get(id=workspace_id, id__in=accessible_workspace_ids(request.user))
        except WorldWorkspace.DoesNotExist:
            return None

    def _ref(self, workspace, branch_key):
        if not branch_key or branch_key == "main":
            return None
        branch = _find_branch(workspace, branch_key)
        if not branch:
            return False
        return branch.git_ref or f"oc/branch/{branch.pk.hex}"

    def get(self, request, workspace_id, timeline_id=None):
        workspace = self._workspace(request, workspace_id)
        if not workspace:
            return Response({"detail": "workspace not found"}, status=404)
        ref = self._ref(workspace, request.query_params.get("branch"))
        if ref is False:
            return Response({"detail": "branch not found"}, status=404)
        service = GitRepositoryService(workspace)
        if request.query_params.get("snapshot") == "1":
            commit = request.query_params.get("commit") or request.query_params.get("snapshot_id")
            if request.query_params.get("commit") and request.query_params.get("snapshot_id"):
                return Response({"detail": "commit 与 snapshot_id 不能同时提供"}, status=400)
            return Response({"snapshot": service.temporal_snapshot(commit, ref=ref)})

        from_commit = request.query_params.get("from_commit") or request.query_params.get("from")
        to_commit = request.query_params.get("to_commit") or request.query_params.get("to")
        from_snapshot = request.query_params.get("from_snapshot")
        to_snapshot = request.query_params.get("to_snapshot")
        if from_commit and from_snapshot or to_commit and to_snapshot:
            return Response({"detail": "同一端不能同时使用 commit 和 snapshot"}, status=400)
        # Snapshot refs are immutable Git temporal snapshots in MVP. Keeping
        # them as a separate public parameter makes the contract explicit and
        # leaves room for database snapshot UUIDs without changing the view.
        from_commit = from_commit or from_snapshot
        to_commit = to_commit or to_snapshot
        if timeline_id and not (from_commit or to_commit):
            return Response({"detail": "timeline diff 需要 from/to commit 或 snapshot"}, status=400)
        try:
            diff = service.temporal_diff(from_commit, to_commit, ref=ref, timeline_id=timeline_id)
        except Exception as exc:
            from rest_framework.exceptions import ValidationError
            if isinstance(exc, ValidationError):
                return Response({"detail": str(exc.detail)}, status=400)
            raise
        diff["source"] = {"branch": request.query_params.get("branch") or "main", "from": "snapshot" if from_snapshot else "commit", "to": "snapshot" if to_snapshot else "commit"}
        return Response({"diff": diff})


class CommitDiffView(APIView):

    def get(self, request, workspace_id):
        try:
            workspace = WorldWorkspace.objects.get(id=workspace_id, id__in=accessible_workspace_ids(request.user))
        except WorldWorkspace.DoesNotExist:
            return Response({"detail": "workspace not found"}, status=status.HTTP_404_NOT_FOUND)
        branch_key = self.request.query_params.get("branch")
        ref = None
        if branch_key and branch_key != "main":
            branch = _find_branch(workspace, branch_key)
            if not branch:
                return Response({"detail": "branch not found"}, status=404)
            ref = branch.git_ref or f"oc/branch/{branch.pk.hex}"
        return Response({"diff": GitRepositoryService(workspace).diff(request.query_params.get("from"), request.query_params.get("to"), ref=ref)})


class CommitJobViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CommitJobSerializer

    def get_queryset(self):
        queryset = CommitJob.objects.filter(workspace_id__in=accessible_workspace_ids(self.request.user))
        workspace_id = self.request.query_params.get("workspace")
        return queryset.filter(workspace_id=workspace_id) if workspace_id else queryset

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        from apps.canvas.services.commit import CanvasCommitService
        job = self.get_object()
        member = membership(request.user, job.workspace)
        if not member or member.role not in ("owner", "editor"): return Response({"detail":"需要 editor 权限"}, status=403)
        if job.status == CommitJob.Status.PROJECTION_SYNC_FAILED:
            projection_job = GraphProjectionJob.objects.filter(commit_job=job).first()
            if projection_job is None:
                return Response({"detail": "projection job not found"}, status=409)
            from apps.core.services.neo4j_projection import Neo4jProjection
            Neo4jProjection().process_job(projection_job)
        else:
            CanvasCommitService.sync_pending(job.workspace)
        job.refresh_from_db()
        return Response(self.get_serializer(job).data)


class _TemporalWorkspaceViewSet:
    """Shared role checks and server-controlled branch routing for chronology."""
    def get_queryset(self):
        return self.queryset.filter(workspace_id__in=accessible_workspace_ids(self.request.user))

    def _require_editor(self, workspace):
        member = membership(self.request.user, workspace) if workspace else None
        if not member or member.role == "reader":
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("需要 editor 权限")

    def _resolve_branch(self, workspace, key):
        if not key or key == "main":
            return None
        branch = _find_branch(workspace, key, active_only=True)
        if not branch:
            from rest_framework.exceptions import NotFound
            raise NotFound("branch not found")
        return branch

    def _restore_temporal(self, model, pk):
        workspace_id = self.request.query_params.get("workspace")
        workspace = WorldWorkspace.objects.filter(
            id=workspace_id, id__in=accessible_workspace_ids(self.request.user)
        ).first()
        if not workspace:
            from rest_framework.exceptions import NotFound
            raise NotFound("workspace not found")
        self._require_editor(workspace)
        branch_key = self.request.query_params.get("branch")
        branch = self._resolve_branch(workspace, branch_key) if branch_key and branch_key != "main" else None
        base = model.objects.filter(pk=pk, workspace=workspace).first()
        target = None
        if branch:
            target = model.objects.filter(pk=pk, workspace=workspace, branch=branch).first()
            if target is None and base is not None and base.branch_id is None:
                key = {
                    "timeline_id": base.timeline_id, "event_id": base.event_id, "time_system_id": base.time_system_id,
                } if isinstance(base, TimelineEntry) else {
                    "character_id": base.character_id, "time_system_id": base.time_system_id,
                }
                target = model.objects.filter(workspace=workspace, branch=branch, **key).first()
        else:
            target = model.objects.filter(pk=pk, workspace=workspace, branch__isnull=True).first()
        if target is None:
            from rest_framework.exceptions import NotFound
            raise NotFound("chronology record not found")
        target.archived = False
        target.save(update_fields=["archived", "updated_at"] if hasattr(target, "updated_at") else ["archived"])
        return target

    def perform_create(self, serializer):
        workspace = serializer.validated_data.get("workspace")
        self._require_editor(workspace)
        # Branch is intentionally read-only in serializers: choose it from the
        # request context and validate membership/status before writing.
        if "branch" in serializer.fields:
            branch_key = self.request.query_params.get("branch") or self.request.data.get("branch")
            branch = self._resolve_branch(workspace, branch_key)
            serializer.save(branch=branch)
        else:
            serializer.save()

    def perform_update(self, serializer):
        instance = serializer.instance
        self._require_editor(instance.workspace)
        workspace = serializer.validated_data.get("workspace", instance.workspace)
        if workspace.id != instance.workspace_id:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"workspace": "不能将数据移动到另一个工作区"})

        if not hasattr(instance, "branch_id"):
            serializer.save()
            return

        from django.db import transaction
        from .models import TimelineEntry, TimelineParticipation, CharacterLifespan

        branch_key = self.request.query_params.get("branch")
        # Without explicit branch context, updates stay on the record's own
        # branch. The request body can never move a record between branches.
        branch = self._resolve_branch(workspace, branch_key) if branch_key is not None else instance.branch
        with transaction.atomic():
            if branch:
                # Serialize copy-on-write for a given branch, avoiding duplicate
                # overrides when two clients edit the inherited row together.
                WorldBranch.objects.select_for_update().get(pk=branch.pk)
            if branch and instance.branch_id is None:
                if isinstance(instance, TimelineEntry):
                    key = {"timeline_id": instance.timeline_id, "event_id": instance.event_id, "time_system_id": instance.time_system_id}
                    clone = TimelineEntry.objects.filter(workspace=workspace, branch=branch, **key).first()
                    if clone is None:
                        clone = TimelineEntry.objects.create(
                            workspace=instance.workspace, branch=branch, timeline=instance.timeline,
                            event=instance.event, time_system=instance.time_system,
                            start_value=instance.start_value, end_value=instance.end_value,
                            sequence=instance.sequence, summary=instance.summary, properties=instance.properties,
                        )
                        TimelineParticipation.objects.bulk_create([
                            TimelineParticipation(entry=clone, entity_id=row.entity_id, role=row.role)
                            for row in instance.participations.all()
                        ])
                elif isinstance(instance, CharacterLifespan):
                    key = {"character_id": instance.character_id, "time_system_id": instance.time_system_id}
                    clone = CharacterLifespan.objects.filter(workspace=workspace, branch=branch, **key).first()
                    if clone is None:
                        clone = CharacterLifespan.objects.create(
                            workspace=instance.workspace, branch=branch, character=instance.character,
                            time_system=instance.time_system, start_value=instance.start_value,
                            end_value=instance.end_value, properties=instance.properties,
                        )
                else:
                    clone = instance
                serializer.instance = clone
            # Branch is assigned exclusively by the server-side routing above.
            serializer.save(branch=branch)

    def perform_destroy(self, instance):
        self._require_editor(instance.workspace)
        if not hasattr(instance, "branch_id"):
            instance.delete()
            return

        branch_key = self.request.query_params.get("branch")
        branch = self._resolve_branch(instance.workspace, branch_key) if branch_key else instance.branch
        if branch is None:
            # Main edits are explicit and destructive; branch context is needed
            # to create a tombstone instead of mutating main.
            instance.delete()
            return

        from django.db import transaction
        from .models import TimelineEntry, TimelineParticipation, CharacterLifespan
        with transaction.atomic():
            WorldBranch.objects.select_for_update().get(pk=branch.pk)
            if isinstance(instance, TimelineEntry):
                key = {
                    "timeline_id": instance.timeline_id,
                    "event_id": instance.event_id,
                    "time_system_id": instance.time_system_id,
                }
                main_exists = TimelineEntry.objects.filter(
                    workspace=instance.workspace, branch__isnull=True, **key,
                ).exists()
            elif isinstance(instance, CharacterLifespan):
                key = {"character_id": instance.character_id, "time_system_id": instance.time_system_id}
                main_exists = CharacterLifespan.objects.filter(
                    workspace=instance.workspace, branch__isnull=True, **key,
                ).exists()
            else:
                main_exists = False
            if main_exists:
                # Keep the override as an explicit branch tombstone. When the
                # branch view resolved an inherited main row, create a clone;
                # never mark the main record archived.
                if instance.branch_id is None:
                    if isinstance(instance, TimelineEntry):
                        tombstone = TimelineEntry.objects.create(
                            workspace=instance.workspace, branch=branch,
                            timeline=instance.timeline, event=instance.event,
                            time_system=instance.time_system,
                            start_value=instance.start_value, end_value=instance.end_value,
                            sequence=instance.sequence, summary=instance.summary,
                            properties=instance.properties, archived=True,
                        )
                        TimelineParticipation.objects.bulk_create([
                            TimelineParticipation(entry=tombstone, entity_id=row.entity_id, role=row.role)
                            for row in instance.participations.all()
                        ])
                    else:
                        CharacterLifespan.objects.create(
                            workspace=instance.workspace, branch=branch,
                            character=instance.character, time_system=instance.time_system,
                            start_value=instance.start_value, end_value=instance.end_value,
                            properties=instance.properties, archived=True,
                        )
                else:
                    instance.archived = True
                    instance.save(update_fields=["archived", "updated_at"] if isinstance(instance, TimelineEntry) else ["archived"])
            else:
                # A branch-local addition has no main counterpart to hide.
                instance.delete()


class TimeSystemViewSet(_TemporalWorkspaceViewSet, viewsets.ModelViewSet):
    queryset = TimeSystem.objects.all()
    serializer_class = TimeSystemSerializer

    @action(detail=True, methods=["get"], url_path="convert")
    def convert(self, request, pk=None):
        """Convert an integer value through explicitly declared routes.

        The response retains an exact numerator/denominator so a fractional
        display value is never silently rounded into a fictional fact.
        """
        source = self.get_object()
        target_id = request.query_params.get("target_system")
        raw_value = request.query_params.get("value")
        if not target_id or raw_value is None:
            return Response({"detail": "target_system and value are required"}, status=400)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return Response({"detail": "value must be an integer"}, status=400)
        target = TimeSystem.objects.filter(workspace=source.workspace, pk=target_id).first()
        if not target:
            return Response({"detail": "target time system not found"}, status=404)
        if target.pk == source.pk:
            return Response({
                "source_system": str(source.pk), "target_system": str(target.pk),
                "value": value, "converted_numerator": value, "converted_denominator": 1,
                "exact": True, "path": [str(source.pk)],
            })

        conversions = list(TimeSystemConversion.objects.filter(workspace=source.workspace).select_related("source_system", "target_system"))
        adjacency = {}
        for conversion in conversions:
            adjacency.setdefault(str(conversion.source_system_id), []).append(conversion)
        queue = deque([(str(source.pk), Fraction(value), [str(source.pk)])])
        visited = {str(source.pk)}
        found = None
        while queue:
            current, current_value, path = queue.popleft()
            for conversion in adjacency.get(current, []):
                next_id = str(conversion.target_system_id)
                if next_id in visited:
                    continue
                next_value = current_value * Fraction(conversion.numerator, conversion.denominator) + conversion.offset
                next_path = path + [next_id]
                if next_id == str(target.pk):
                    found = (next_value, next_path)
                    queue.clear()
                    break
                visited.add(next_id)
                queue.append((next_id, next_value, next_path))
        if found is None:
            return Response({"detail": "no conversion route exists"}, status=404)
        converted, path = found
        return Response({
            "source_system": str(source.pk), "target_system": str(target.pk),
            "value": value, "converted_numerator": converted.numerator,
            "converted_denominator": converted.denominator,
            "exact": converted.denominator == 1, "path": path,
        })


class TimeSystemConversionViewSet(_TemporalWorkspaceViewSet, viewsets.ModelViewSet):
    queryset = TimeSystemConversion.objects.select_related("workspace", "source_system", "target_system").all()
    serializer_class = TimeSystemConversionSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        workspace_id = self.request.query_params.get("workspace")
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        source = self.request.query_params.get("source_system")
        target = self.request.query_params.get("target_system")
        if source:
            queryset = queryset.filter(source_system_id=source)
        if target:
            queryset = queryset.filter(target_system_id=target)
        return queryset


class TimelineEntryViewSet(_TemporalWorkspaceViewSet, viewsets.ModelViewSet):
    queryset = TimelineEntry.objects.select_related("workspace", "timeline", "event", "time_system", "branch").all()
    serializer_class = TimelineEntrySerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        workspace_id = self.request.query_params.get("workspace")
        branch_key = self.request.query_params.get("branch")
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        if branch_key and branch_key != "main":
            workspace = WorldWorkspace.objects.filter(id=workspace_id, id__in=accessible_workspace_ids(self.request.user)).first()
            branch = _find_branch(workspace, branch_key, active_only=True) if workspace else None
            if not branch:
                from rest_framework.exceptions import NotFound
                raise NotFound("branch not found")
            branch_overrides = TimelineEntry.objects.filter(
                branch=branch, timeline_id=OuterRef("timeline_id"),
                event_id=OuterRef("event_id"), time_system_id=OuterRef("time_system_id"),
            )
            queryset = queryset.filter(
                Q(branch=branch, archived=False)
                | (Q(branch__isnull=True, archived=False) & ~Exists(branch_overrides))
            )
        else:
            queryset = queryset.filter(branch__isnull=True, archived=False)
        timeline_id = self.request.query_params.get("timeline")
        if timeline_id:
            queryset = queryset.filter(timeline_id=timeline_id)
        return queryset



    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        target = self._restore_temporal(TimelineEntry, pk)
        return Response(self.get_serializer(target).data)

class CharacterLifespanViewSet(_TemporalWorkspaceViewSet, viewsets.ModelViewSet):
    queryset = CharacterLifespan.objects.select_related("workspace", "character", "time_system", "branch").all()
    serializer_class = CharacterLifespanSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        workspace_id = self.request.query_params.get("workspace")
        branch_key = self.request.query_params.get("branch")
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        if branch_key and branch_key != "main":
            workspace = WorldWorkspace.objects.filter(id=workspace_id, id__in=accessible_workspace_ids(self.request.user)).first()
            branch = _find_branch(workspace, branch_key, active_only=True) if workspace else None
            if not branch:
                from rest_framework.exceptions import NotFound
                raise NotFound("branch not found")
            branch_overrides = CharacterLifespan.objects.filter(
                branch=branch, character_id=OuterRef("character_id"),
                time_system_id=OuterRef("time_system_id"),
            )
            queryset = queryset.filter(
                Q(branch=branch, archived=False)
                | (Q(branch__isnull=True, archived=False) & ~Exists(branch_overrides))
            )
        else:
            queryset = queryset.filter(branch__isnull=True, archived=False)
        axis = self.request.query_params.get("time_system")
        return queryset.filter(time_system_id=axis) if axis else queryset



    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        target = self._restore_temporal(CharacterLifespan, pk)
        return Response(self.get_serializer(target).data)

class TemporalGraphSliceView(APIView):
    """Return an as-of view on one explicitly selected fictional time axis."""
    def get(self, request):
        workspace_id = request.query_params.get("workspace")
        axis_id = request.query_params.get("time_system")
        at_value = request.query_params.get("at")
        if not workspace_id or not axis_id or at_value is None:
            return Response({"detail": "workspace, time_system and at are required"}, status=400)
        try:
            at_value = int(at_value)
        except (TypeError, ValueError):
            return Response({"detail": "at must be an integer normalized time value"}, status=400)
        timeline_id = request.query_params.get("timeline")
        workspace = WorldWorkspace.objects.filter(id=workspace_id, id__in=accessible_workspace_ids(request.user)).first()
        if not workspace:
            return Response({"detail": "workspace not found"}, status=404)
        axis = TimeSystem.objects.filter(id=axis_id, workspace=workspace).first()
        if not axis:
            return Response({"detail": "time system not found"}, status=404)
        branch_key = request.query_params.get("branch", "main")
        branch = _find_branch(workspace, branch_key, active_only=True) if branch_key != "main" else None
        if branch_key != "main" and not branch:
            return Response({"detail": "branch not found"}, status=404)

        # A commit query switches the slice to the immutable Git chronology
        # snapshot.  PostgreSQL remains the source of truth for live views,
        # while this path makes historical timeline playback deterministic and
        # independent from later edits to the workspace.
        commit = request.query_params.get("commit")
        if commit:
            from apps.version_control.services.git_sync import GitRepositoryService

            ref = (branch.git_ref or f"oc/branch/{branch.pk.hex}") if branch else None
            snapshot = GitRepositoryService(workspace).temporal_snapshot(commit, ref=ref)
            systems = {str(row.get("id")): row for row in snapshot.get("time_systems", []) if row.get("id")}
            axis = systems.get(str(axis_id))
            if not axis:
                return Response({"detail": "time system not found in historical commit"}, status=404)
            entity_map = {str(row.get("id")): row for row in snapshot.get("entities", []) if row.get("id")}
            active_entities = {
                key: value for key, value in entity_map.items()
                if value.get("status", "active") == "active"
            }
            characters = [
                entity for entity in active_entities.values()
                if entity.get("type") == Entity.EntityType.CHARACTER
            ]
            spans = {
                str(row.get("character")): row
                for row in snapshot.get("character_lifespans", [])
                if str(row.get("time_system")) == str(axis_id) and not row.get("archived", False)
            }
            alive, life_states = [], {}
            for person in characters:
                person_id = str(person["id"])
                span = spans.get(person_id)
                living = span is None or ((span.get("start_value") is None or span["start_value"] <= at_value) and (span.get("end_value") is None or span["end_value"] >= at_value))
                if living:
                    alive.append(person)
                    life_states[person_id] = "unspecified" if span is None else "alive"
            alive_ids = {str(person["id"]) for person in alive}
            timeline_filter = str(timeline_id) if timeline_id else None
            snapshot_entries = [
                row for row in snapshot.get("timeline_entries", [])
                if str(row.get("time_system")) == str(axis_id)
                and not row.get("archived", False)
                and row.get("start_value") is not None
                and row["start_value"] <= at_value
                and (row.get("end_value") is not None and row["end_value"] >= at_value or row.get("end_value") is None and row["start_value"] == at_value)
                and (timeline_filter is None or str(row.get("timeline")) == timeline_filter)
            ]
            nodes = [{"data": {"id": str(person["id"]), "title": person.get("title", "未命名人物"), "type": person.get("type", "character"), "lifeState": life_states[str(person["id"])]}} for person in alive]
            edges = []
            shown_ids = set(alive_ids)
            for entry in sorted(snapshot_entries, key=lambda row: (row.get("start_value", 0), row.get("sequence", 0))):
                event = active_entities.get(str(entry.get("event")))
                timeline = active_entities.get(str(entry.get("timeline")))
                if not event:
                    continue
                event_id = str(event["id"])
                nodes.append({"data": {"id": event_id, "title": event.get("title", "未命名事件"), "type": event.get("type", "event"), "time": entry.get("start_value"), "timeline": timeline.get("title", "") if timeline else ""}})
                shown_ids.add(event_id)
                for participant in entry.get("participants", []) or []:
                    participant_id = str(participant.get("entity"))
                    if participant_id in alive_ids:
                        edges.append({"data": {"id": f"participation:{entry.get('id')}:{participant_id}", "source": event_id, "target": participant_id, "label": participant.get("role") or "参与"}})
            for relation in snapshot.get("relations", []):
                relation_time_system = relation.get("time_system")
                valid_from = relation.get("valid_from")
                valid_to = relation.get("valid_to")
                valid = (relation_time_system is None and valid_from is None and valid_to is None) or (str(relation_time_system) == str(axis_id) and (valid_from is None or valid_from <= at_value) and (valid_to is None or valid_to >= at_value))
                source_id, target_id = str(relation.get("source")), str(relation.get("target"))
                if valid and source_id in shown_ids and target_id in shown_ids:
                    edges.append({"data": {"id": str(relation.get("id")), "source": source_id, "target": target_id, "label": relation.get("type", "关系"), "relationType": relation.get("type", "")}})
            return Response({"time_system": axis, "at": at_value, "branch": branch_key, "commit": snapshot.get("commit", commit), "nodes": nodes, "edges": edges, "characters": [{"id": str(person["id"]), "title": person.get("title", "未命名人物"), "life_state": life_states[str(person["id"])]} for person in alive]})

        branch_entities = effective_entities(workspace, branch)
        _, logical_map, _ = effective_entity_map(workspace, branch)
        characters = [
            entity for entity in branch_entities
            if entity.type == Entity.EntityType.CHARACTER and entity.status == Entity.Status.ACTIVE
        ]
        lifespans = CharacterLifespan.objects.filter(workspace=workspace, time_system=axis, branch__isnull=True, archived=False)
        entries = TimelineEntry.objects.filter(workspace=workspace, time_system=axis, branch__isnull=True, archived=False)
        if branch:
            lifespan_overrides = CharacterLifespan.objects.filter(branch=branch, character_id=OuterRef("character_id"), time_system_id=OuterRef("time_system_id"))
            entry_overrides = TimelineEntry.objects.filter(branch=branch, timeline_id=OuterRef("timeline_id"), event_id=OuterRef("event_id"), time_system_id=OuterRef("time_system_id"))
            lifespans = CharacterLifespan.objects.filter(workspace=workspace, time_system=axis).filter(
                Q(branch=branch, archived=False)
                | (Q(branch__isnull=True, archived=False) & ~Exists(lifespan_overrides))
            )
            entries = TimelineEntry.objects.filter(workspace=workspace, time_system=axis).filter(
                Q(branch=branch, archived=False)
                | (Q(branch__isnull=True, archived=False) & ~Exists(entry_overrides))
            )
        # Branch-local lifespan records override the corresponding main lifespan.
        spans = {}
        for span in sorted(lifespans.select_related("character"), key=lambda row: row.branch_id is not None):
            spans[map_entity_id_to_effective(span.character_id, logical_map)] = span
        alive, life_states = [], {}
        for person in characters:
            span = spans.get(person.id)
            living = span is None or ((span.start_value is None or span.start_value <= at_value) and (span.end_value is None or span.end_value >= at_value))
            if living:
                alive.append(person)
                life_states[str(person.id)] = "unspecified" if span is None else "alive"
        alive_ids = {str(person.id) for person in alive}
        timeline_id = request.query_params.get("timeline")
        entries = entries.filter(start_value__lte=at_value).filter(Q(end_value__gte=at_value) | Q(end_value__isnull=True, start_value=at_value)).select_related("event", "timeline")
        if timeline_id:
            entries = entries.filter(timeline_id=timeline_id)
        nodes = [{"data": {"id": str(p.id), "title": p.title, "type": p.type, "lifeState": life_states[str(p.id)]}} for p in alive]
        edges = []
        shown_ids = set(alive_ids)
        for entry in entries.order_by("start_value", "sequence"):
            event = logical_map.get(entry.event_id) or entry.event
            timeline = logical_map.get(entry.timeline_id) or entry.timeline
            event_id = str(event.id)
            nodes.append({"data": {"id": event_id, "title": event.title, "type": event.type, "time": entry.start_value, "timeline": timeline.title}})
            shown_ids.add(event_id)
            for participation in entry.participations.select_related("entity"):
                participant = logical_map.get(participation.entity_id) or participation.entity
                participant_id = str(participant.id)
                if participant_id in alive_ids:
                    edges.append({"data": {"id": f"participation:{entry.id}:{participant_id}", "source": event_id, "target": participant_id, "label": participation.role or "参与"}})
        relations = Relation.objects.filter(workspace=workspace).filter(Q(time_system__isnull=True, valid_from__isnull=True, valid_to__isnull=True) | Q(time_system=axis, valid_from__isnull=True) | Q(time_system=axis, valid_from__lte=at_value)).filter(Q(time_system__isnull=True, valid_to__isnull=True) | Q(time_system=axis, valid_to__isnull=True) | Q(time_system=axis, valid_to__gte=at_value))
        relations = [
            relation for relation in effective_relations(workspace, branch)
            if (relation.time_system_id is None and relation.valid_from is None and relation.valid_to is None)
            or (relation.time_system_id == axis.id and (relation.valid_from is None or relation.valid_from <= at_value) and (relation.valid_to is None or relation.valid_to >= at_value))
        ]
        for relation in relations:
            source_id = str(map_entity_id_to_effective(relation.source_id, logical_map))
            target_id = str(map_entity_id_to_effective(relation.target_id, logical_map))
            if source_id in shown_ids and target_id in shown_ids:
                edges.append({"data": {"id": str(relation.id), "source": source_id, "target": target_id, "label": relation.get_relation_type_display(), "relationType": relation.relation_type}})
        return Response({"time_system": TimeSystemSerializer(axis).data, "at": at_value, "branch": branch_key, "nodes": nodes, "edges": edges, "characters": [{"id": str(p.id), "title": p.title, "life_state": life_states[str(p.id)]} for p in alive]})
