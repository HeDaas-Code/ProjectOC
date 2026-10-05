from django.db import models

from apps.core.models import Entity, Relation, WorldBranch, WorldWorkspace
from apps.core.services.branching import (
    effective_entity_map,
    effective_entities,
    effective_relations,
    map_entity_id_to_effective,
    resolve_entity_for_branch,
)


class GraphProvider:
    """PostgreSQL-backed graph provider.

    Branch reads use the same copy-on-write overlay as the entity/relation REST
    endpoints.  Neo4j can implement this interface later, but it must return
    this exact node/edge shape and never become the source of truth.
    """

    @staticmethod
    def _branch(workspace: WorldWorkspace, branch):
        if not branch or branch == "main":
            return None
        if isinstance(branch, WorldBranch):
            return branch
        return WorldBranch.objects.filter(
            workspace=workspace,
            status=WorldBranch.Status.ACTIVE,
        ).filter(models.Q(id=branch) | models.Q(name=branch)).first()

    def graph(self, workspace: WorldWorkspace, entity_type: str | None = None, branch=None):
        branch_obj = self._branch(workspace, branch)
        entities = effective_entities(workspace, branch_obj)
        if entity_type:
            entities = [entity for entity in entities if entity.type == entity_type]
        entity_ids = {entity.id for entity in entities}
        _, logical_map, _ = effective_entity_map(workspace, branch_obj)

        edges = []
        for relation in effective_relations(workspace, branch_obj):
            source_id = map_entity_id_to_effective(relation.source_id, logical_map)
            target_id = map_entity_id_to_effective(relation.target_id, logical_map)
            if source_id not in entity_ids or target_id not in entity_ids:
                continue
            edges.append(
                {
                    "data": {
                        "id": str(relation.id),
                        "source": str(source_id),
                        "target": str(target_id),
                        "relationType": relation.relation_type,
                        "label": relation.get_relation_type_display(),
                        "weight": relation.weight,
                        "properties": relation.properties,
                        "branch": str(relation.branch_id) if relation.branch_id else "main",
                        "baseRelation": str(relation.base_relation_id) if relation.base_relation_id else None,
                    }
                }
            )

        return {
            "nodes": [
                {
                    "data": {
                        "id": str(entity.id),
                        "title": entity.title,
                        "type": entity.type,
                        "status": entity.status,
                        "metadata": entity.metadata,
                        "branch": str(entity.branch_id) if entity.branch_id else "main",
                        "baseEntity": str(entity.base_entity_id) if entity.base_entity_id else None,
                    }
                }
                for entity in entities
            ],
            "edges": edges,
            "branch": str(branch_obj.id) if branch_obj else "main",
        }

    def links(self, entity: Entity, branch=None):
        branch_obj = self._branch(entity.workspace, branch)
        effective = resolve_entity_for_branch(entity.id, entity.workspace, branch_obj)
        if effective is None:
            return {
                "entity": {"id": str(entity.id), "title": entity.title, "type": entity.type},
                "outgoing": [],
                "incoming": [],
            }

        entities = effective_entities(entity.workspace, branch_obj)
        _, logical_map, _ = effective_entity_map(entity.workspace, branch_obj)
        visible_ids = {row.id for row in entities}
        outgoing, incoming = [], []
        for relation in effective_relations(entity.workspace, branch_obj):
            source_id = map_entity_id_to_effective(relation.source_id, logical_map)
            target_id = map_entity_id_to_effective(relation.target_id, logical_map)
            if source_id not in visible_ids or target_id not in visible_ids:
                continue
            if source_id == effective.id:
                outgoing.append(self._relation_payload(relation, "outgoing", logical_map))
            if target_id == effective.id:
                incoming.append(self._relation_payload(relation, "incoming", logical_map))
        outgoing.sort(key=lambda item: (item["relationType"], item["otherEntity"]["title"]))
        incoming.sort(key=lambda item: (item["relationType"], item["otherEntity"]["title"]))
        return {
            "entity": {
                "id": str(effective.id),
                "title": effective.title,
                "type": effective.type,
                "branch": str(effective.branch_id) if effective.branch_id else "main",
                "baseEntity": str(effective.base_entity_id) if effective.base_entity_id else None,
            },
            "outgoing": outgoing,
            "incoming": incoming,
        }

    @staticmethod
    def _relation_payload(relation: Relation, direction: str, logical_map=None):
        if logical_map is None:
            other = relation.target if direction == "outgoing" else relation.source
        else:
            raw_id = relation.target_id if direction == "outgoing" else relation.source_id
            other = logical_map.get(raw_id) or (relation.target if direction == "outgoing" else relation.source)
        return {
            "id": str(relation.id),
            "direction": direction,
            "relationType": relation.relation_type,
            "relationLabel": relation.get_relation_type_display(),
            "otherEntity": {
                "id": str(other.id),
                "title": other.title,
                "type": other.type,
            },
            "properties": relation.properties,
            "validFrom": relation.valid_from,
            "validTo": relation.valid_to,
            "branch": str(relation.branch_id) if relation.branch_id else "main",
            "baseRelation": str(relation.base_relation_id) if relation.base_relation_id else None,
        }


class ConflictService:
    @staticmethod
    def for_proposal(workspace, entity_type: str, title: str, content: str):
        conflicts = []
        existing = Entity.objects.filter(workspace=workspace, title__iexact=title).first()
        if existing:
            conflicts.append({
                "kind": "duplicate_title",
                "severity": "warning",
                "entityId": str(existing.id),
                "message": f"已有同名实体“{existing.title}”，请确认是更新它还是创建新实体。",
            })
        return conflicts
