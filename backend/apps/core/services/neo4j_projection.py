"""Rebuildable Neo4j projection.

PostgreSQL remains the authority.  Neo4j stores one isolated graph view for
``main`` and for every active branch so copy-on-write overrides cannot create
invisible duplicate nodes or leak into the default graph.
"""
from __future__ import annotations

import json
import os

from django.db.models import Q
from django.utils import timezone

from apps.core.models import GraphProjectionJob, WorldBranch
from apps.core.services.branching import (
    effective_entities,
    effective_entity_map,
    effective_relations,
    map_entity_id_to_effective,
    resolve_entity_for_branch,
)


class Neo4jProjection:
    def __init__(self):
        self.uri = os.getenv("NEO4J_URI", "")
        self.user = os.getenv("NEO4J_USER", "neo4j")
        self.password = os.getenv("NEO4J_PASSWORD", "")

    def _driver(self):
        if not self.uri or not self.password:
            raise RuntimeError("Neo4j projection is not configured")
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise RuntimeError("neo4j Python driver is not installed") from exc
        return GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    @staticmethod
    def _view_key(branch) -> str:
        return "main" if branch is None or str(branch) == "main" else str(getattr(branch, "id", branch))

    @classmethod
    def _records(cls, workspace):
        """Build projection records using the same overlay as PostgreSQL graph APIs."""
        views = [(None, "main")]
        views.extend(
            (branch, str(branch.id))
            for branch in WorldBranch.objects.filter(
                workspace=workspace, status=WorldBranch.Status.ACTIVE
            ).exclude(name="main").only("id")
        )
        nodes, edges = [], []
        for branch, view in views:
            entities = effective_entities(workspace, branch)
            _, logical_map, _ = effective_entity_map(workspace, branch)
            visible_ids = {entity.id for entity in entities}
            for entity in entities:
                nodes.append({
                    "id": str(entity.id),
                    "workspace_id": str(workspace.id),
                    "view": view,
                    "branch_id": str(entity.branch_id) if entity.branch_id else "main",
                    "base_entity": str(entity.base_entity_id) if entity.base_entity_id else None,
                    "type": entity.type,
                    "title": entity.title,
                    "content": entity.content,
                    "status": entity.status,
                    "metadata_json": json.dumps(entity.metadata, ensure_ascii=False, separators=(",", ":")),
                })
            for relation in effective_relations(workspace, branch):
                source_id = map_entity_id_to_effective(relation.source_id, logical_map)
                target_id = map_entity_id_to_effective(relation.target_id, logical_map)
                if source_id not in visible_ids or target_id not in visible_ids:
                    continue
                edges.append({
                    "id": str(relation.id),
                    "workspace_id": str(workspace.id),
                    "view": view,
                    "branch_id": str(relation.branch_id) if relation.branch_id else "main",
                    "base_relation": str(relation.base_relation_id) if relation.base_relation_id else None,
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "relation_type": relation.relation_type,
                    "weight": relation.weight,
                    "properties_json": json.dumps(relation.properties, ensure_ascii=False, separators=(",", ":")),
                })
        return nodes, edges

    def rebuild(self, workspace):
        nodes, edges = self._records(workspace)
        driver = self._driver()
        try:
            with driver.session() as session:
                session.execute_write(self._replace, str(workspace.id), nodes, edges)
        finally:
            driver.close()

    @staticmethod
    def _replace(tx, workspace_id, nodes, edges):
        tx.run("MATCH (n:OCEntity {workspace_id:$w}) DETACH DELETE n", w=workspace_id).consume()
        if nodes:
            tx.run(
                "UNWIND $rows AS row "
                "CREATE (:OCEntity {id:row.id, workspace_id:row.workspace_id, view:row.view, "
                "branch_id:row.branch_id, base_entity:row.base_entity, type:row.type, title:row.title, "
                "content:row.content, status:row.status, metadata_json:row.metadata_json})",
                rows=nodes,
            ).consume()
        if edges:
            tx.run(
                "UNWIND $rows AS row "
                "MATCH (s:OCEntity {workspace_id:row.workspace_id, view:row.view, id:row.source_id}) "
                "MATCH (t:OCEntity {workspace_id:row.workspace_id, view:row.view, id:row.target_id}) "
                "CREATE (s)-[:OC_RELATION {id:row.id, view:row.view, branch_id:row.branch_id, "
                "base_relation:row.base_relation, type:row.relation_type, weight:row.weight, "
                "properties_json:row.properties_json}]->(t)",
                rows=edges,
            ).consume()

    def process_job(self, job):
        from django.db.models import F

        GraphProjectionJob.objects.filter(
            pk=job.pk,
            status__in=[GraphProjectionJob.Status.PENDING, GraphProjectionJob.Status.FAILED],
        ).update(
            attempts=F("attempts") + 1,
            last_attempt_at=timezone.now(),
        )
        try:
            self.rebuild(job.workspace)
        except Exception as exc:
            GraphProjectionJob.objects.filter(pk=job.pk).update(status=GraphProjectionJob.Status.FAILED, error_message=str(exc))
            # An unconfigured optional projection is a normal local-dev
            # state: PostgreSQL graph APIs remain authoritative and the
            # related commit is still Git-synced. Only a configured but
            # unavailable projection blocks its projection sub-state.
            if job.commit_job_id and self.uri and self.password:
                from apps.core.models import CommitJob
                CommitJob.objects.filter(pk=job.commit_job_id).update(
                    status=CommitJob.Status.PROJECTION_SYNC_FAILED,
                    error_message=str(exc),
                )
            return False, str(exc)
        GraphProjectionJob.objects.filter(pk=job.pk).update(status=GraphProjectionJob.Status.SYNCED, error_message="")
        if job.commit_job_id:
            from apps.core.models import CommitJob
            CommitJob.objects.filter(
                pk=job.commit_job_id,
                status=CommitJob.Status.PROJECTION_SYNC_FAILED,
            ).update(status=CommitJob.Status.SYNCED, error_message="")
        return True, ""

    def health(self):
        try:
            driver = self._driver()
            try:
                driver.verify_connectivity()
            finally:
                driver.close()
            return {"available": True, "configured": True}
        except Exception as exc:
            return {"available": False, "configured": bool(self.uri and self.password), "error": str(exc)}

    def shortest_path(self, workspace, start, end, branch="main"):
        branch_obj = branch if isinstance(branch, WorldBranch) else None
        if branch not in (None, "main") and branch_obj is None:
            branch_obj = WorldBranch.objects.filter(workspace=workspace).filter(
                Q(id=branch) | Q(name=branch)
            ).first()
        view = self._view_key(branch_obj)
        resolved_start = resolve_entity_for_branch(start, workspace, branch_obj)
        resolved_end = resolve_entity_for_branch(end, workspace, branch_obj)
        if not resolved_start or not resolved_end:
            return None
        driver = self._driver()
        try:
            with driver.session() as session:
                result = session.run(
                    "MATCH (a:OCEntity {workspace_id:$w,view:$view,id:$a}) "
                    "MATCH (b:OCEntity {workspace_id:$w,view:$view,id:$b}) "
                    "MATCH p=shortestPath((a)-[:OC_RELATION*..12]-(b)) "
                    "WHERE all(n IN nodes(p) WHERE n.status='active') "
                    "RETURN [n IN nodes(p) | {id:n.id,title:n.title,type:n.type}] AS nodes, "
                    "[r IN relationships(p) | {type:r.type,id:r.id}] AS edges",
                    w=str(workspace.id), view=view, a=str(resolved_start.id), b=str(resolved_end.id),
                ).single()
                return dict(result) if result else None
        finally:
            driver.close()

    def impact(self, workspace, entity_id, branch="main"):
        branch_obj = branch if isinstance(branch, WorldBranch) else None
        if branch not in (None, "main") and branch_obj is None:
            branch_obj = WorldBranch.objects.filter(workspace=workspace).filter(
                Q(id=branch) | Q(name=branch)
            ).first()
        view = self._view_key(branch_obj)
        resolved = resolve_entity_for_branch(entity_id, workspace, branch_obj)
        if not resolved:
            return []
        driver = self._driver()
        try:
            with driver.session() as session:
                result = session.run(
                    "MATCH (n:OCEntity {workspace_id:$w,view:$view,id:$id})-[:OC_RELATION]-(other:OCEntity) "
                    "WHERE n.status='active' AND other.status='active' "
                    "RETURN other.id AS id,other.title AS title,count(*) AS degree "
                    "ORDER BY degree DESC",
                    w=str(workspace.id), view=view, id=str(resolved.id),
                )
                return [dict(row) for row in result]
        finally:
            driver.close()
