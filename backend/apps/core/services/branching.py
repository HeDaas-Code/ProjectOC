"""Helpers for resolving a workspace's effective branch view.

PostgreSQL stores the branch rows as immutable-ish change records.  A row on a
branch with ``base_entity``/``base_relation`` set is a copy-on-write override
of a main row; a row without a base pointer is a branch-local addition.  The
helpers in this module keep that overlay logic in one place so REST, graph and
merge code do not accidentally expose both the base and its override.
"""
from __future__ import annotations

import json
from typing import Iterable

from django.db.models import Q

from apps.core.models import Entity, Relation, WorldBranch, WorldWorkspace


def capture_main_snapshot(workspace: WorldWorkspace) -> dict:
    """Return a JSON-safe baseline snapshot for a newly-created branch."""
    entities = list(
        Entity.objects.filter(workspace=workspace, branch__isnull=True)
        .values(
            "id", "type", "title", "content", "metadata", "status",
        )
    )
    relations = list(
        Relation.objects.filter(workspace=workspace, branch__isnull=True)
        .values(
            "id", "source_id", "target_id", "relation_type", "properties",
            "weight", "time_system_id", "valid_from", "valid_to", "archived",
        )
    )
    # UUIDs and other values returned by ``values`` need to be converted before
    # being stored in a JSONField.  Round-tripping through json is deliberate:
    # it also makes the snapshot independent from model instances.
    return json.loads(json.dumps({"entities": entities, "relations": relations}, default=str))


def materialize_branch_baseline(branch: WorldBranch) -> None:
    """Materialize the immutable main baseline into copy-on-write branch rows.

    A branch must not read the moving ``main`` rows after it is created. The
    baseline is represented by branch-owned copies whose base pointers retain
    logical identity. Editing one of these rows turns it into the branch
    change-set without changing the baseline snapshot or main. The operation
    is idempotent so branch creation/retry can safely call it more than once.
    """
    if branch.name == "main":
        return

    snapshot = branch.base_snapshot or capture_main_snapshot(branch.workspace)
    if not branch.base_snapshot:
        WorldBranch.objects.filter(pk=branch.pk).update(base_snapshot=snapshot)
        branch.base_snapshot = snapshot

    main_entities = {
        str(row.id): row
        for row in Entity.objects.filter(workspace=branch.workspace, branch__isnull=True)
    }
    branch_entities = {
        str(row.base_entity_id): row
        for row in Entity.objects.filter(
            workspace=branch.workspace, branch=branch, base_entity__isnull=False
        )
    }
    for raw in snapshot.get("entities", []):
        logical_id = str(raw.get("id"))
        base = main_entities.get(logical_id)
        if not base or logical_id in branch_entities:
            continue
        Entity.objects.create(
            workspace=branch.workspace, branch=branch, base_entity=base,
            type=raw.get("type", base.type), title=raw.get("title", base.title),
            content=raw.get("content", base.content), metadata=raw.get("metadata") or {},
            status=raw.get("status", base.status), git_path=base.git_path,
            commit_hash=base.commit_hash, sync_status=Entity.SyncStatus.SYNCED,
        )

    main_relations = {
        str(row.id): row
        for row in Relation.objects.filter(workspace=branch.workspace, branch__isnull=True)
    }
    # Relations in the branch view must point at the branch's materialized
    # entity rows, not at moving main rows.  Apart from preserving isolation,
    # this prevents an FK cascade on a later main deletion from erasing the
    # branch's inherited relation.
    branch_entity_by_base = {
        str(row.base_entity_id): row
        for row in Entity.objects.filter(
            workspace=branch.workspace, branch=branch, base_entity__isnull=False
        )
    }
    branch_relations = {
        str(row.base_relation_id): row
        for row in Relation.objects.filter(
            workspace=branch.workspace, branch=branch, base_relation__isnull=False
        )
    }
    for raw in snapshot.get("relations", []):
        logical_id = str(raw.get("id"))
        base = main_relations.get(logical_id)
        if not base or logical_id in branch_relations:
            continue
        source_id = raw.get("source_id") or base.source_id
        target_id = raw.get("target_id") or base.target_id
        source_copy = branch_entity_by_base.get(str(source_id))
        target_copy = branch_entity_by_base.get(str(target_id))
        Relation.objects.create(
            workspace=branch.workspace, branch=branch, base_relation=base,
            source_id=source_copy.id if source_copy else source_id,
            target_id=target_copy.id if target_copy else target_id,
            relation_type=raw.get("relation_type", base.relation_type),
            properties=raw.get("properties") or {}, weight=raw.get("weight", base.weight),
            time_system_id=raw.get("time_system_id") or base.time_system_id,
            valid_from=raw.get("valid_from", base.valid_from),
            valid_to=raw.get("valid_to", base.valid_to),
            archived=raw.get("archived", base.archived),
        )




def ensure_branch_entity_dependency(branch: WorldBranch, entity: Entity) -> Entity:
    """Pin a main entity referenced by a branch-local record into the branch.

    Normally all entities that existed at branch creation are already
    materialized. A branch can nevertheless reference a main entity created
    later (for example a proposal linking to a newly-created target). Pinning
    that explicit dependency keeps the branch view deterministic without
    exposing unrelated later main changes.
    """
    if entity.branch_id == branch.id:
        return entity
    existing = Entity.objects.filter(
        workspace=branch.workspace, branch=branch, base_entity=entity
    ).first()
    if existing:
        return existing
    pinned = Entity.objects.create(
        workspace=branch.workspace, branch=branch, base_entity=entity,
        type=entity.type, title=entity.title, content=entity.content,
        metadata=entity.metadata, status=entity.status, git_path=entity.git_path,
        commit_hash=entity.commit_hash, sync_status=Entity.SyncStatus.SYNCED,
    )
    snapshot = branch.base_snapshot or {}
    entities = list(snapshot.get("entities", []))
    if not any(str(row.get("id")) == str(entity.id) for row in entities):
        entities.append({
            "id": str(entity.id), "type": entity.type, "title": entity.title,
            "content": entity.content, "metadata": entity.metadata,
            "status": entity.status,
        })
        snapshot["entities"] = entities
        WorldBranch.objects.filter(pk=branch.pk).update(base_snapshot=snapshot)
        branch.base_snapshot = snapshot
    return pinned


def branch_entity_queryset(workspace: WorldWorkspace, branch: WorldBranch):
    """Return the immutable-baseline plus local entity view for a branch."""
    return Entity.objects.filter(workspace=workspace, branch=branch, status=Entity.Status.ACTIVE)


def branch_relation_queryset(workspace: WorldWorkspace, branch: WorldBranch):
    """Return the immutable-baseline plus local relation view for a branch."""
    return Relation.objects.filter(workspace=workspace, branch=branch, archived=False)


def effective_entities(workspace: WorldWorkspace, branch: WorldBranch | None = None) -> list[Entity]:
    """Materialize active entities for main or an immutable branch baseline."""
    return list(Entity.objects.filter(
        workspace=workspace, branch=branch, status=Entity.Status.ACTIVE
    ).order_by("title", "created_at"))


def effective_entity_map(workspace: WorldWorkspace, branch: WorldBranch | None = None):
    """Return both node ids and logical-main-id -> effective entity mappings."""
    rows = effective_entities(workspace, branch)
    by_id = {row.id: row for row in rows}
    logical = {}
    for row in rows:
        logical[row.base_entity_id or row.id] = row
        logical[row.id] = row
    return rows, logical, by_id


def effective_relations(workspace: WorldWorkspace, branch: WorldBranch | None = None) -> list[Relation]:
    """Materialize active relations for main or an immutable branch baseline."""
    return list(Relation.objects.filter(
        workspace=workspace, branch=branch, archived=False
    ).select_related("source", "target").order_by("relation_type", "created_at"))


def resolve_entity_for_branch(entity_id, workspace: WorldWorkspace, branch: WorldBranch | None, *, include_archived=False):
    """Resolve a logical or physical entity in the selected immutable view."""
    if branch is None:
        queryset = Entity.objects.filter(id=entity_id, workspace=workspace, branch__isnull=True)
        if not include_archived:
            queryset = queryset.filter(status=Entity.Status.ACTIVE)
        return queryset.first()

    direct = Entity.objects.filter(id=entity_id, workspace=workspace, branch=branch).first()
    if direct is not None:
        return direct if include_archived or direct.status == Entity.Status.ACTIVE else None
    base = Entity.objects.filter(id=entity_id, workspace=workspace, branch__isnull=True).first()
    if base is None:
        return None
    override = Entity.objects.filter(workspace=workspace, branch=branch, base_entity=base).first()
    if override is not None:
        return override if include_archived or override.status == Entity.Status.ACTIVE else None
    return None


def resolve_entity_for_branch_write(entity_id, workspace: WorldWorkspace, branch: WorldBranch):
    """Resolve an entity row for a branch mutation, including tombstones."""
    direct = Entity.objects.filter(id=entity_id, workspace=workspace, branch=branch).first()
    if direct is not None:
        return direct
    base = Entity.objects.filter(id=entity_id, workspace=workspace, branch__isnull=True).first()
    if base is None:
        return None
    return Entity.objects.filter(workspace=workspace, branch=branch, base_entity=base).first()


def resolve_relation_for_branch(relation_id, workspace: WorldWorkspace, branch: WorldBranch | None, *, include_archived=False):
    if branch is None:
        queryset = Relation.objects.filter(id=relation_id, workspace=workspace, branch__isnull=True)
        if not include_archived:
            queryset = queryset.filter(archived=False)
        return queryset.first()

    direct = Relation.objects.filter(id=relation_id, workspace=workspace, branch=branch).first()
    if direct is not None:
        return direct if include_archived or not direct.archived else None
    base = Relation.objects.filter(id=relation_id, workspace=workspace, branch__isnull=True).first()
    if base is None:
        return None
    override = Relation.objects.filter(workspace=workspace, branch=branch, base_relation=base).first()
    if override is not None:
        return override if include_archived or not override.archived else None
    return None


def resolve_relation_for_branch_write(relation_id, workspace: WorldWorkspace, branch: WorldBranch):
    direct = Relation.objects.filter(id=relation_id, workspace=workspace, branch=branch).first()
    if direct is not None:
        return direct
    base = Relation.objects.filter(id=relation_id, workspace=workspace, branch__isnull=True).first()
    if base is None:
        return None
    return Relation.objects.filter(workspace=workspace, branch=branch, base_relation=base).first()


def map_entity_id_to_effective(entity_id, logical_map):
    row = logical_map.get(entity_id)
    return row.id if row is not None else entity_id
