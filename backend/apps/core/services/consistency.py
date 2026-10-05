"""Deterministic world consistency checks.

The report is deliberately read-only.  It runs against the same effective
branch overlay used by the graph API so that a branch can be reviewed without
leaking unmerged facts into ``main``.  AI may consume this result later, but it
must not be allowed to mutate a report or silently fix an issue.
"""
from __future__ import annotations

from collections import defaultdict

from apps.core.models import (
    CharacterLifespan,
    Entity,
    Relation,
    TimelineEntry,
    TimelineParticipation,
    WorldBranch,
    WorldWorkspace,
)
from apps.core.services.branching import (
    effective_entities,
    effective_entity_map,
    effective_relations,
    map_entity_id_to_effective,
)
from django.db.models import Exists, OuterRef, Q


def _issue(code, severity, message, *, entity_id=None, relation_id=None, metadata=None):
    result = {"code": code, "severity": severity, "message": message}
    if entity_id is not None:
        result["entity_id"] = str(entity_id)
    if relation_id is not None:
        result["relation_id"] = str(relation_id)
    if metadata:
        result["metadata"] = metadata
    return result


def _temporal_rows(workspace: WorldWorkspace, branch: WorldBranch | None):
    """Return effective temporal records using the same copy-on-write keying."""
    if branch is None:
        lifespans = CharacterLifespan.objects.filter(
            workspace=workspace, branch__isnull=True, archived=False
        )
        entries = TimelineEntry.objects.filter(
            workspace=workspace, branch__isnull=True, archived=False
        )
        return list(lifespans), list(entries)

    lifespan_overrides = CharacterLifespan.objects.filter(
        workspace=workspace,
        branch=branch,
        character_id=OuterRef("character_id"),
        time_system_id=OuterRef("time_system_id"),
    )
    entry_overrides = TimelineEntry.objects.filter(
        workspace=workspace,
        branch=branch,
        timeline_id=OuterRef("timeline_id"),
        event_id=OuterRef("event_id"),
        time_system_id=OuterRef("time_system_id"),
    )
    lifespans = CharacterLifespan.objects.filter(workspace=workspace).filter(
        Q(branch=branch, archived=False)
        | (Q(branch__isnull=True, archived=False) & ~Exists(lifespan_overrides))
    )
    entries = TimelineEntry.objects.filter(workspace=workspace).filter(
        Q(branch=branch, archived=False)
        | (Q(branch__isnull=True, archived=False) & ~Exists(entry_overrides))
    )
    return list(lifespans), list(entries)



def _identity_key(entity):
    """Return an explicit canonical identity key, never a guessed title."""
    metadata = entity.metadata if isinstance(entity.metadata, dict) else {}
    for key in ("canonical_identity", "canonical_id", "identity_key"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().casefold()
    return ""


def _branch_baseline_row(branch, collection, key, value):
    for row in (branch.base_snapshot or {}).get(collection, []):
        if str(row.get(key)) == str(value):
            return row
    return None


def _branch_override_conflicts(branch, entities, relations):
    """Detect copy-on-write rows that overwrite newer main data."""
    issues = []
    if not branch:
        return issues
    main_entities = {str(x.id): x for x in Entity.objects.filter(workspace=branch.workspace, branch__isnull=True)}
    for entity in entities:
        if not entity.base_entity_id:
            continue
        main = main_entities.get(str(entity.base_entity_id))
        baseline = _branch_baseline_row(branch, "entities", "id", entity.base_entity_id)
        if not main or not baseline:
            continue
        current = {"type": main.type, "title": main.title, "content": main.content, "metadata": main.metadata, "status": main.status}
        base = {key: baseline.get(key) for key in current}
        if current != base:
            issues.append(_issue(
                "branch_main_overlap", "warning",
                f"分支实体“{entity.title}”的主线基线已经变化，合并前需要三方复核。",
                entity_id=entity.id,
                metadata={"base_entity_id": str(entity.base_entity_id), "baseline": base, "main": current, "branch": {key: getattr(entity, key) for key in current}},
            ))
    main_relations = {str(x.id): x for x in Relation.objects.filter(workspace=branch.workspace, branch__isnull=True)}
    for relation in relations:
        if not relation.base_relation_id:
            continue
        main = main_relations.get(str(relation.base_relation_id))
        baseline = _branch_baseline_row(branch, "relations", "id", relation.base_relation_id)
        if not main or not baseline:
            continue
        current = {"source_id": str(main.source_id), "target_id": str(main.target_id), "relation_type": main.relation_type, "properties": main.properties, "valid_from": main.valid_from, "valid_to": main.valid_to, "archived": main.archived}
        base = {key: baseline.get(key) for key in current}
        if current != base:
            issues.append(_issue(
                "branch_main_overlap", "warning", "分支关系的主线基线已经变化，合并前需要三方复核。",
                relation_id=relation.id,
                metadata={"base_relation_id": str(relation.base_relation_id), "baseline": base, "main": current},
            ))
    return issues

def build_consistency_report(workspace: WorldWorkspace, branch: WorldBranch | None = None):
    entities = effective_entities(workspace, branch)
    relations = effective_relations(workspace, branch)
    _, logical_map, _ = effective_entity_map(workspace, branch)
    entity_by_id = {entity.id: entity for entity in entities}
    visible_ids = set(entity_by_id)
    issues = []
    outgoing = defaultdict(list)
    incoming = defaultdict(list)

    # Duplicate titles are ambiguous references and therefore errors.  Keep a
    # deterministic order so the report can be diffed and cached safely.
    titled = defaultdict(list)
    identities = defaultdict(list)
    for entity in entities:
        titled[entity.title.strip().casefold()].append(entity)
        identity = _identity_key(entity)
        if identity:
            identities[(entity.type, identity)].append(entity)
        if not entity.content.strip():
            issues.append(_issue(
                "empty_content", "warning", f"实体“{entity.title}”没有正文内容。",
                entity_id=entity.id,
            ))
    for normalized, rows in titled.items():
        if normalized and len(rows) > 1:
            ids = [str(row.id) for row in rows]
            for row in rows:
                issues.append(_issue(
                    "duplicate_title", "error", f"实体标题“{row.title}”在当前视图中重复。",
                    entity_id=row.id, metadata={"entity_ids": ids},
                ))

    for (entity_type, identity), rows in identities.items():
        if len(rows) > 1:
            ids = [str(row.id) for row in rows]
            for row in rows:
                issues.append(_issue(
                    "duplicate_canonical_identity", "error",
                    f"实体“{row.title}”与当前视图中的其他实体共享 canonical identity“{identity}”。",
                    entity_id=row.id, metadata={"identity": identity, "entity_type": entity_type, "entity_ids": ids},
                ))

    for relation in relations:
        source_id = map_entity_id_to_effective(relation.source_id, logical_map)
        target_id = map_entity_id_to_effective(relation.target_id, logical_map)
        source = entity_by_id.get(source_id)
        target = entity_by_id.get(target_id)
        if source is None or target is None:
            issues.append(_issue(
                "dangling_relation", "error", "关系引用了当前视图中不可见的实体。",
                relation_id=relation.id,
                metadata={"source_id": str(source_id), "target_id": str(target_id)},
            ))
            continue
        outgoing[source.id].append(relation)
        incoming[target.id].append(relation)

    for entity in entities:
        if not outgoing[entity.id] and not incoming[entity.id]:
            issues.append(_issue(
                "orphan_entity", "warning", f"实体“{entity.title}”没有任何出链或入链。",
                entity_id=entity.id,
            ))
        if entity.type == Entity.EntityType.FLOATING_TIP and not (outgoing[entity.id] or incoming[entity.id]):
            issues.append(_issue(
                "unlinked_floating_tip", "warning", f"游离设定“{entity.title}”尚未链接到其他实体。",
                entity_id=entity.id,
            ))

    # Keep archived references distinct from dangling references: the
    # remediation is to restore or archive the relation, not to invent a node.
    all_live_relations = Relation.objects.filter(workspace=workspace, archived=False).select_related("source", "target")
    for relation in all_live_relations:
        if relation.source.status == Entity.Status.ARCHIVED or relation.target.status == Entity.Status.ARCHIVED:
            issues.append(_issue(
                "archived_entity_referenced", "warning",
                "有效关系仍引用了已归档实体。", relation_id=relation.id,
                metadata={"source_id": str(relation.source_id), "target_id": str(relation.target_id), "source_status": relation.source.status, "target_status": relation.target.status},
            ))

    issues.extend(_branch_override_conflicts(branch, entities, relations))

    lifespans, timeline_entries = _temporal_rows(workspace, branch)
    lifespan_ids = {map_entity_id_to_effective(row.character_id, logical_map) for row in lifespans}
    for character in entities:
        if character.type == Entity.EntityType.CHARACTER and character.id not in lifespan_ids:
            issues.append(_issue(
                "character_without_lifespan", "warning", f"人物“{character.title}”没有生命周期记录。",
                entity_id=character.id,
            ))

    timeline_entry_counts = defaultdict(int)
    participation_counts = defaultdict(int)
    for entry in timeline_entries:
        timeline_id = map_entity_id_to_effective(entry.timeline_id, logical_map)
        event_id = map_entity_id_to_effective(entry.event_id, logical_map)
        if timeline_id not in visible_ids or event_id not in visible_ids:
            issues.append(_issue(
                "dangling_timeline_entry", "error", "时间线事件引用了当前视图中不可见的实体。",
                metadata={"entry_id": str(entry.id), "timeline_id": str(timeline_id), "event_id": str(event_id)},
            ))
            continue
        timeline_entry_counts[timeline_id] += 1
        participation_counts[entry.id] = TimelineParticipation.objects.filter(entry=entry).count()
        if entry.end_value is not None and entry.end_value < entry.start_value:
            issues.append(_issue(
                "invalid_time_range", "error", "时间线事件的结束时间早于开始时间。",
                metadata={"entry_id": str(entry.id)},
            ))
    for timeline in entities:
        if timeline.type == Entity.EntityType.TIMELINE and not timeline_entry_counts[timeline.id]:
            issues.append(_issue(
                "timeline_without_events", "warning", f"时间线“{timeline.title}”没有事件。",
                entity_id=timeline.id,
            ))
    for entry_id, count in participation_counts.items():
        if count == 0:
            issues.append(_issue(
                "event_without_participants", "info", "时间线事件尚未记录参与者。",
                metadata={"entry_id": str(entry_id)},
            ))

    lifespan_by_character_axis = {(str(map_entity_id_to_effective(row.character_id, logical_map)), str(row.time_system_id)): row for row in lifespans}
    for relation in relations:
        if relation.valid_from is None and relation.valid_to is None:
            continue
        for endpoint_id, endpoint_label in ((relation.source_id, "source"), (relation.target_id, "target")):
            entity = entity_by_id.get(map_entity_id_to_effective(endpoint_id, logical_map))
            if not entity or entity.type != Entity.EntityType.CHARACTER or not relation.time_system_id:
                continue
            life = lifespan_by_character_axis.get((str(entity.id), str(relation.time_system_id)))
            if not life:
                continue
            start = relation.valid_from if relation.valid_from is not None else relation.valid_to
            end = relation.valid_to if relation.valid_to is not None else relation.valid_from
            outside = (life.start_value is not None and start is not None and start < life.start_value) or (life.end_value is not None and end is not None and end > life.end_value)
            if outside:
                refs = [{"source_type": "relation", "source_id": str(relation.id), "field_path": "valid_from/valid_to", "excerpt": f"{relation.valid_from}..{relation.valid_to}", "relevance": 1.0}, {"source_type": "lifespan", "source_id": str(life.id), "field_path": "start_value/end_value", "excerpt": f"{life.start_value}..{life.end_value}", "relevance": 1.0}]
                issues.append(_issue("relation_outside_lifespan", "warning", f"关系有效期超出了{endpoint_label}人物的生命周期。", relation_id=relation.id, metadata={"endpoint": endpoint_label, "lifespan_id": str(life.id), "evidence": refs}))

    for relation in Relation.objects.filter(workspace=workspace, branch=branch):
        if relation.valid_from is not None and relation.valid_to is not None and relation.valid_to < relation.valid_from:
            # Normally prevented by the DB constraint; this catches imported
            # or legacy rows and keeps the report useful during migrations.
            issues.append(_issue(
                "invalid_relation_time_range", "error", "关系的有效时间区间无效。",
                relation_id=relation.id,
            ))

    # CONFLICTS_WITH is meaningful maintenance information even when all rows
    # are otherwise valid.  It is informational rather than an error because
    # intentional fictional conflicts are common.
    conflict_count = sum(1 for relation in relations if relation.relation_type == Relation.RelationType.CONFLICTS_WITH)
    if conflict_count:
        issues.append(_issue(
            "conflict_relation_count", "info", f"当前视图包含 {conflict_count} 条冲突关系。",
            metadata={"count": conflict_count},
        ))

    severity_counts = {severity: sum(1 for issue in issues if issue["severity"] == severity) for severity in ("error", "warning", "info")}
    score = max(0, 100 - severity_counts["error"] * 10 - severity_counts["warning"] * 3 - severity_counts["info"])
    issues.sort(key=lambda issue: ({"error": 0, "warning": 1, "info": 2}[issue["severity"]], issue["code"], issue.get("entity_id", issue.get("relation_id", ""))))
    return {
        "branch": str(branch.id) if branch else "main",
        "score": score,
        "summary": severity_counts,
        "issues": issues,
        "entity_count": len(entities),
        "relation_count": len(relations),
    }
