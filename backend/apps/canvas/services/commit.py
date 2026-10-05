"""Atomic promotion of reviewed proposals and replayable Git outbox jobs."""
from __future__ import annotations

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from apps.core.models import CommitJob, Entity, GraphProjectionJob, Relation, WorldWorkspace
from apps.core.services.branching import ensure_branch_entity_dependency
from apps.version_control.services.git_sync import GitRepositoryService
from ..models import EntityProposal, RelationProposal


class CanvasCommitError(ValueError):
    pass


class CanvasCommitService:
    @staticmethod
    def _ids(values, label):
        if not isinstance(values, list):
            raise CanvasCommitError(f"{label} ID 必须是数组")
        normalized = []
        for value in values:
            if not isinstance(value, str):
                raise CanvasCommitError(f"{label} ID 无效")
            normalized.append(value)
        if len(normalized) != len(set(normalized)):
            raise CanvasCommitError(f"{label} ID 不能重复")
        return normalized

    @classmethod
    def selection(cls, canvas, proposal_ids, relation_ids):
        proposal_ids = cls._ids(proposal_ids, "实体提案")
        relation_ids = cls._ids(relation_ids, "关系提案")
        if not proposal_ids and not relation_ids:
            raise CanvasCommitError("至少选择一个实体或关系提案")

        proposals = list(
            EntityProposal.objects.filter(canvas=canvas, id__in=proposal_ids).order_by("id")
        )
        relations = list(
            RelationProposal.objects.filter(canvas=canvas, id__in=relation_ids)
            .select_related(
                "source_proposal",
                "source_proposal__created_entity",
                "target_proposal",
                "target_proposal__created_entity",
                "target_entity",
            )
            .order_by("id")
        )
        if len(proposals) != len(proposal_ids) or len(relations) != len(relation_ids):
            raise CanvasCommitError("提案不属于当前画布")

        selected = {proposal.id for proposal in proposals}
        for proposal in proposals:
            if proposal.status != EntityProposal.Status.PENDING or proposal.created_entity_id:
                raise CanvasCommitError("只能提交待审核实体提案")
            if not proposal.title.strip() or proposal.entity_type not in Entity.EntityType.values:
                raise CanvasCommitError("实体标题或类型无效")

        for relation in relations:
            if relation.status != RelationProposal.Status.PENDING or relation.created_relation_id:
                raise CanvasCommitError("只能提交待审核关系提案")
            if relation.relation_type not in Relation.RelationType.values:
                raise CanvasCommitError("未知关系类型")
            if bool(relation.target_entity_id) == bool(relation.target_proposal_id):
                raise CanvasCommitError("关系需要且只能有一个目标")
            if relation.workspace_id != canvas.workspace_id or relation.canvas_id != canvas.id:
                raise CanvasCommitError("关系提案不属于当前画布")

            source = relation.source_proposal
            if source.workspace_id != canvas.workspace_id or source.canvas_id != canvas.id:
                raise CanvasCommitError("关系源端点不属于当前画布")
            if source.id not in selected and not source.created_entity_id:
                raise CanvasCommitError("请同时选择关系源实体提案，或先提交源实体")
            if source.created_entity_id and source.created_entity.status != Entity.Status.ACTIVE:
                raise CanvasCommitError("关系源端点已归档")

            if relation.target_proposal_id:
                target = relation.target_proposal
                if target.workspace_id != canvas.workspace_id or target.canvas_id != canvas.id:
                    raise CanvasCommitError("关系目标提案不属于当前画布")
                if target.id not in selected and not target.created_entity_id:
                    raise CanvasCommitError("请同时选择关系目标实体提案，或先提交目标实体")
                if target.created_entity_id and target.created_entity.status != Entity.Status.ACTIVE:
                    raise CanvasCommitError("关系目标端点已归档")
            else:
                target_entity = relation.target_entity
                if (
                    target_entity.workspace_id != canvas.workspace_id
                    or target_entity.status != Entity.Status.ACTIVE
                    or target_entity.branch_id not in (None, canvas.branch_id)
                ):
                    raise CanvasCommitError("关系目标不属于当前世界观或已归档")

        return proposals, relations

    @classmethod
    def preview(cls, canvas, proposal_ids, relation_ids):
        proposals, relations = cls.selection(canvas, proposal_ids, relation_ids)
        changes = {
            "entities": [
                {
                    "proposal_id": str(proposal.id),
                    "type": proposal.entity_type,
                    "title": proposal.title,
                    "content": proposal.content,
                    "metadata": proposal.metadata,
                    "updated_at": proposal.updated_at.isoformat(),
                }
                for proposal in proposals
            ],
            "relations": [
                {
                    "proposal_id": str(relation.id),
                    "source": str(relation.source_proposal_id),
                    "target": str(relation.target_proposal_id or relation.target_entity_id),
                    "type": relation.relation_type,
                    "properties": relation.properties,
                    "time_system": str(relation.time_system_id) if relation.time_system_id else None,
                    "valid_from": relation.valid_from,
                    "valid_to": relation.valid_to,
                    "updated_at": relation.updated_at.isoformat(),
                }
                for relation in relations
            ],
        }
        digest = hashlib.sha256(
            json.dumps(changes, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        diff = "\n".join(
            "+ " + line
            for line in json.dumps(changes, indent=2, ensure_ascii=False).splitlines()
        )
        return {"preview_token": digest, "changes": changes, "diff": diff}

    @classmethod
    def commit(
        cls,
        canvas,
        proposal_ids,
        relation_ids,
        idempotency_key,
        preview_token=None,
    ):
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise CanvasCommitError("idempotency_key 必填且不能超过 200 字符")
        proposal_ids = cls._ids(proposal_ids, "实体提案")
        relation_ids = cls._ids(relation_ids, "关系提案")
        request_data = {
            "canvas": str(canvas.id),
            "proposal_ids": sorted(proposal_ids),
            "relation_proposal_ids": sorted(relation_ids),
        }

        with transaction.atomic():
            # Serialize confirmations within a world, including concurrent
            # requests that reuse the same idempotency key.
            WorldWorkspace.objects.select_for_update().get(id=canvas.workspace_id)
            existing = CommitJob.objects.filter(
                workspace=canvas.workspace,
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                if existing.request_data != request_data:
                    raise CanvasCommitError("该幂等键已用于其他提交")
                return (
                    existing,
                    list(Entity.objects.filter(id__in=existing.entity_ids)),
                    list(Relation.objects.filter(id__in=existing.relation_ids)),
                )

            preview = cls.preview(canvas, proposal_ids, relation_ids)
            if not isinstance(preview_token, str) or not preview_token:
                raise CanvasCommitError("提交前必须先生成预览")
            if preview_token != preview["preview_token"]:
                raise CanvasCommitError("提案已变化或未预览，请重新预览后确认")

            proposals, relation_proposals = cls.selection(canvas, proposal_ids, relation_ids)
            entities, relations = [], []
            confirmed_at = timezone.now().isoformat()
            for proposal in proposals:
                provenance = {
                    "source": proposal.source,
                    "actor": "local-user",
                    "session": str(proposal.dialogue_session_id or ""),
                    "proposal": str(proposal.id),
                    "confirmed_at": confirmed_at,
                }
                entity = Entity.objects.create(
                    workspace=canvas.workspace,
                    branch=canvas.branch,
                    type=proposal.entity_type,
                    title=proposal.title.strip(),
                    content=proposal.content,
                    metadata={**proposal.metadata, "provenance": provenance},
                )
                proposal.created_entity = entity
                proposal.status = EntityProposal.Status.ACCEPTED
                proposal.save(update_fields=["created_entity", "status", "updated_at"])
                entities.append(entity)

            for proposal in relation_proposals:
                proposal.refresh_from_db()
                proposal.source_proposal.refresh_from_db()
                source = proposal.source_proposal.created_entity
                if proposal.target_proposal_id:
                    proposal.target_proposal.refresh_from_db()
                    target = proposal.target_proposal.created_entity
                else:
                    target = proposal.target_entity
                if source is None or target is None:
                    raise CanvasCommitError("关系端点尚未正式建立")
                # Pin any main-side endpoint created after the canvas branch
                # was opened. This is an explicit dependency, not a moving
                # view of unrelated main records.
                if source.branch_id != canvas.branch_id:
                    source = ensure_branch_entity_dependency(canvas.branch, source)
                if target.branch_id != canvas.branch_id:
                    target = ensure_branch_entity_dependency(canvas.branch, target)
                relation = Relation.objects.create(
                    workspace=canvas.workspace,
                    branch=canvas.branch,
                    source=source,
                    target=target,
                    relation_type=proposal.relation_type,
                    time_system=proposal.time_system,
                    valid_from=proposal.valid_from,
                    valid_to=proposal.valid_to,
                    properties={
                        **proposal.properties,
                        "provenance": {
                            "proposal": str(proposal.id),
                            "actor": "local-user",
                            "confirmed_at": confirmed_at,
                        },
                    },
                    weight=proposal.confidence or 1.0,
                )
                proposal.status = RelationProposal.Status.ACCEPTED
                proposal.created_relation = relation
                proposal.save(update_fields=["status", "created_relation", "updated_at"])
                relations.append(relation)

            export = GitRepositoryService(canvas.workspace).snapshot(branch=canvas.branch)
            job = CommitJob.objects.create(
                workspace=canvas.workspace,
                canvas_id=canvas.id,
                branch=canvas.branch,
                idempotency_key=idempotency_key,
                request_data=request_data,
                export_data=export,
                status=CommitJob.Status.DATABASE_COMMITTED,
                entity_ids=[str(entity.id) for entity in entities],
                relation_ids=[str(relation.id) for relation in relations],
            )
            projection_job = GraphProjectionJob.objects.create(
                workspace=canvas.workspace,
                commit_job=job,
            )

        cls.sync_pending(canvas.workspace)
        from apps.core.services.neo4j_projection import Neo4jProjection
        projection_ok, projection_error = Neo4jProjection().process_job(projection_job)
        if not projection_ok:
            projection = Neo4jProjection()
            if projection.uri and projection.password:
                CommitJob.objects.filter(pk=job.pk).update(
                    status=CommitJob.Status.PROJECTION_SYNC_FAILED,
                    error_message=projection_error,
                )
        job.refresh_from_db()
        for entity in entities:
            entity.refresh_from_db()
        return job, entities, relations

    @staticmethod
    def sync_job(job):
        """Synchronize exactly one claimed Git outbox job.

        The caller owns the lease; failures remain durable and are scheduled
        by :class:`OutboxWorker`.
        """
        from django.db.models import F

        job.refresh_from_db()
        if job.status not in (CommitJob.Status.DATABASE_COMMITTED, CommitJob.Status.GIT_SYNC_FAILED):
            return job
        CommitJob.objects.filter(pk=job.pk).update(
            status=CommitJob.Status.GIT_SYNCING,
            attempts=F("attempts") + 1,
            last_attempt_at=timezone.now(),
            error_message="",
        )
        try:
            commit_hash = GitRepositoryService(job.workspace).commit_job(job)
        except Exception as exc:  # noqa: BLE001 - persist every external Git failure for retry.
            with transaction.atomic():
                CommitJob.objects.filter(pk=job.pk).update(
                    status=CommitJob.Status.GIT_SYNC_FAILED,
                    error_message=str(exc),
                )
                Entity.objects.filter(id__in=job.entity_ids).update(
                    sync_status=Entity.SyncStatus.FAILED
                )
        else:
            with transaction.atomic():
                CommitJob.objects.filter(pk=job.pk).update(
                    status=CommitJob.Status.SYNCED,
                    commit_hash=commit_hash,
                    error_message="",
                )
                Entity.objects.filter(id__in=job.entity_ids).update(
                    sync_status=Entity.SyncStatus.SYNCED,
                    commit_hash=commit_hash,
                )
        job.refresh_from_db()
        return job

    @staticmethod
    def sync_pending(workspace):
        # Keep the synchronous API used by the MVP, but use the same leased
        # worker path as the standalone process so retries cannot race it.
        from apps.core.services.outbox_worker import OutboxWorker

        return OutboxWorker().run_once(workspace=workspace, include_projection=False)

