"""Live Neo4j projection checks.

The regular suite keeps Neo4j optional and uses PostgreSQL/SQLite doubles.  Run
this module explicitly inside the Compose network when a real Neo4j instance is
available::

    RUN_NEO4J_INTEGRATION=1 python manage.py test tests.test_neo4j_integration

The test deliberately exercises the same branch view used by the REST graph
API, then rebuilds twice to prove that projection replacement is idempotent.
"""
from __future__ import annotations

import os
import unittest
import uuid

from django.test import TransactionTestCase

from apps.core.models import Entity, Relation, WorldBranch, WorldWorkspace
from apps.core.services.neo4j_projection import Neo4jProjection


@unittest.skipUnless(
    os.getenv("RUN_NEO4J_INTEGRATION") == "1",
    "set RUN_NEO4J_INTEGRATION=1 to run against a real Neo4j instance",
)
class Neo4jLiveProjectionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.workspace = WorldWorkspace.objects.create(
            name=f"Neo4j live {uuid.uuid4().hex[:8]}",
            slug=f"neo4j-live-{uuid.uuid4().hex}",
        )
        self.main = WorldBranch.objects.create(workspace=self.workspace, name="main")
        self.branch = WorldBranch.objects.create(
            workspace=self.workspace,
            name="integration",
            git_ref=f"oc/branch/{uuid.uuid4().hex}",
            base_commit="base",
        )
        self.a = self._entity("Aether", "canonical_setting")
        self.b = self._entity("Lina", "character")
        self.tip = self._entity("Blue crystal", "floating_tip")
        self.relation_one = Relation.objects.create(
            workspace=self.workspace,
            branch=self.branch,
            source=self.tip,
            target=self.a,
            relation_type=Relation.RelationType.LINKED_TO,
        )
        self.relation_two = Relation.objects.create(
            workspace=self.workspace,
            branch=self.branch,
            source=self.a,
            target=self.b,
            relation_type=Relation.RelationType.INFLUENCES,
        )
        self.projection = Neo4jProjection()

    def _entity(self, title, entity_type):
        return Entity.objects.create(
            workspace=self.workspace,
            branch=self.branch,
            type=entity_type,
            title=title,
            content=title,
        )

    def tearDown(self):
        # Keep the external projection clean even though Django rolls back its
        # test database after this TransactionTestCase.
        try:
            driver = self.projection._driver()
            try:
                with driver.session() as session:
                    session.run(
                        "MATCH (n:OCEntity {workspace_id:$workspace}) DETACH DELETE n",
                        workspace=str(self.workspace.id),
                    ).consume()
            finally:
                driver.close()
        finally:
            super().tearDown()

    def test_branch_projection_path_impact_and_idempotent_rebuild(self):
        self.projection.rebuild(self.workspace)

        driver = self.projection._driver()
        try:
            with driver.session() as session:
                counts = session.run(
                    "MATCH (n:OCEntity {workspace_id:$workspace}) "
                    "RETURN n.view AS view, count(n) AS count ORDER BY view",
                    workspace=str(self.workspace.id),
                ).data()
                edge_count = session.run(
                    "MATCH ()-[r:OC_RELATION {view:$view}]->() RETURN count(r) AS count",
                    view=str(self.branch.id),
                ).single()["count"]
        finally:
            driver.close()

        self.assertEqual(counts, [{"view": str(self.branch.id), "count": 3}])
        self.assertEqual(edge_count, 2)

        path = self.projection.shortest_path(
            self.workspace, self.tip.id, self.b.id, self.branch
        )
        self.assertEqual(
            [node["title"] for node in path["nodes"]],
            ["Blue crystal", "Aether", "Lina"],
        )
        impact = self.projection.impact(self.workspace, self.a.id, self.branch)
        self.assertEqual({item["title"] for item in impact}, {"Blue crystal", "Lina"})

        # A full replacement must not duplicate nodes or relationships.
        self.projection.rebuild(self.workspace)
        driver = self.projection._driver()
        try:
            with driver.session() as session:
                self.assertEqual(
                    session.run(
                        "MATCH (n:OCEntity {workspace_id:$workspace}) RETURN count(n) AS count",
                        workspace=str(self.workspace.id),
                    ).single()["count"],
                    3,
                )
                self.assertEqual(
                    session.run(
                        "MATCH ()-[r:OC_RELATION {view:$view}]->() RETURN count(r) AS count",
                        view=str(self.branch.id),
                    ).single()["count"],
                    2,
                )
        finally:
            driver.close()
