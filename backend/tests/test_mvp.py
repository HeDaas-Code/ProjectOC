import tempfile
from pathlib import Path
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from apps.canvas.models import CanvasContainer, CanvasOperation, DialogueMemory, DialogueMemoryAudit, EntityCanvasReference, EntityProposal, StagingCanvas
from apps.core.models import CommitJob, Entity, Relation, WorldBranch, WorldWorkspace


class MvpApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post("/api/v1/auth/bootstrap/", {"username":"owner", "password":"correct-horse-battery", "email":"owner@example.test"}, format="json")
        self.assertEqual(owner.status_code, 201)
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name), OPENAI_API_KEY="", OPENAI_MODEL="local-test-model")
        self.settings_override.enable()
        workspace_response = self.client.post("/api/v1/workspaces/", {"name": "魔法世界", "description": "测试"}, format="json")
        self.assertEqual(workspace_response.status_code, 201)
        self.workspace_id = workspace_response.data["id"]
        canvas_response = self.client.post("/api/v1/canvases/", {
            "workspace": self.workspace_id, "name": "第一次产婆会话", "snapshot": {"store": {}}
        }, format="json")
        self.assertEqual(canvas_response.status_code, 201)
        self.canvas_id = canvas_response.data["id"]

    def test_graph_formal_relation_review_and_isolation(self):
        canvas = StagingCanvas.objects.get(pk=self.canvas_id)
        graph = StagingCanvas.objects.create(workspace=canvas.workspace, branch=canvas.branch, purpose="graph")
        source = Entity.objects.create(workspace=canvas.workspace, branch=canvas.branch, type="character", title="甲")
        target = Entity.objects.create(workspace=canvas.workspace, branch=canvas.branch, type="character", title="乙")
        url = f"/api/v1/canvases/{graph.id}"
        created = self.client.post(url + "/graph-relation-proposals/", {
            "source_entity": str(source.id), "target_entity": str(target.id), "relation_type": "KNOWS",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        selection = {"relation_proposal_ids": [created.data["id"]]}
        preview = self.client.post(url + "/graph-preview/", selection, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        confirmed = self.client.post(url + "/graph-commit/", {
            **selection, "preview_token": preview.data["preview_token"], "idempotency_key": "graph-review-test",
        }, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(Relation.objects.filter(branch=canvas.branch, source=source, target=target).count(), 1)
        self.assertEqual(Relation.objects.filter(branch=None).count(), 0)
        invalid = self.client.post(url + "/graph-preview/", {"relation_proposal_ids": []}, format="json")
        self.assertEqual(invalid.status_code, 400)

    def test_canvas_container_creates_child_canvas_and_rejects_reader(self):
        created = self.client.post("/api/v1/canvas-containers/", {
            "workspace": self.workspace_id, "branch": "main", "name": "魔法体系",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        container = CanvasContainer.objects.get(pk=created.data["id"])
        self.assertEqual(container.canvas.purpose, StagingCanvas.Purpose.STAGING)
        child = self.client.post("/api/v1/canvas-containers/", {
            "workspace": self.workspace_id, "branch": "main", "parent": str(container.id), "name": "星辰魔法",
        }, format="json")
        self.assertEqual(child.status_code, 201, child.data)
        self.assertEqual(str(child.data["parent"]), str(container.id))
        listed = self.client.get(f"/api/v1/canvas-containers/?workspace={self.workspace_id}&branch=main")
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(row["id"] == str(container.id) for row in listed.data))
        cycle = self.client.patch(f"/api/v1/canvas-containers/{container.id}/", {"parent": child.data["id"]}, format="json")
        self.assertEqual(cycle.status_code, 400)
        container.refresh_from_db()
        self.assertIsNone(container.parent_id)
        before = container.canvas.snapshot_version
        projection = self.client.get(f"/api/v1/canvas-containers/{container.id}/projection/")
        self.assertEqual(projection.status_code, 200, projection.data)
        self.assertEqual(len(projection.data["children"]), 1)
        container.canvas.refresh_from_db()
        self.assertEqual(container.canvas.snapshot_version, before)
        from django.contrib.auth import get_user_model
        from apps.accounts.models import WorkspaceMembership
        reader = get_user_model().objects.create_user(username="container-reader", password="reader-password")
        WorkspaceMembership.objects.create(workspace_id=self.workspace_id, user=reader, role="reader")
        client = APIClient()
        client.force_authenticate(reader)
        self.assertEqual(client.get(f"/api/v1/canvas-containers/{container.id}/projection/").status_code, 200)
        self.assertEqual(client.patch(f"/api/v1/canvas-containers/{container.id}/", {"name": "禁止"}, format="json").status_code, 403)
        self.assertEqual(client.delete(f"/api/v1/canvas-containers/{container.id}/").status_code, 403)

        entity = Entity.objects.create(workspace=container.workspace, branch_id=container.branch_id, type="character", title="共享实体", content="共享内容")
        reference = self.client.post("/api/v1/entity-canvas-references/", {"container": str(container.id), "entity": str(entity.id)}, format="json")
        self.assertEqual(reference.status_code, 201, reference.data)
        self.assertTrue(EntityCanvasReference.objects.filter(container=container, entity=entity).exists())
        projection = self.client.get(f"/api/v1/canvas-containers/{container.id}/projection/")
        self.assertTrue(any(node["entity_id"] == str(entity.id) for node in projection.data["nodes"]))

    def test_existing_entity_update_requires_review_and_preview(self):
        canvas = StagingCanvas.objects.get(pk=self.canvas_id)
        entity = Entity.objects.create(workspace=canvas.workspace, branch_id=canvas.branch_id, type="character", title="旧名", content="旧内容")
        proposal = self.client.post("/api/v1/proposals/", {
            "workspace": self.workspace_id, "canvas": self.canvas_id, "operation": "update",
            "target_entity": str(entity.id), "entity_type": "character", "title": "新名", "content": "新内容",
        }, format="json")
        self.assertEqual(proposal.status_code, 201, proposal.data)
        entity.refresh_from_db()
        self.assertEqual(entity.title, "旧名")
        preview = self.client.post(f"/api/v1/canvases/{self.canvas_id}/preview/", {"proposal_ids": [proposal.data["id"]], "relation_proposal_ids": []}, format="json")
        self.assertEqual(preview.status_code, 200)
        self.assertIn('"operation": "update"', preview.data["diff"])
        commit = self.client.post(f"/api/v1/canvases/{self.canvas_id}/commit/", {"proposal_ids": [proposal.data["id"]], "relation_proposal_ids": [], "idempotency_key": "entity-update-review", "preview_token": preview.data["preview_token"]}, format="json")
        self.assertEqual(commit.status_code, 200, commit.data)
        entity.refresh_from_db()
        self.assertEqual(entity.title, "新名")
        self.assertEqual(entity.content, "新内容")


    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def test_canvas_optimistic_version_dialogue_proposal_and_commit(self):
        saved = self.client.patch(f"/api/v1/canvases/{self.canvas_id}/", {
            "snapshot": {"store": {"shape:one": {"type": "text", "text": "魔法"}}},
            "expected_version": 1,
        }, format="json")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.data["snapshot_version"], 2)
        stale = self.client.patch(f"/api/v1/canvases/{self.canvas_id}/", {
            "snapshot": {"store": {}}, "expected_version": 1,
        }, format="json")
        self.assertEqual(stale.status_code, 409)

        session = self.client.post("/api/v1/dialogue/sessions/", {
            "workspace": self.workspace_id, "canvas": self.canvas_id, "model": "fallback-model",
        }, format="json")
        self.assertEqual(session.status_code, 201)
        message = self.client.post(f"/api/v1/dialogue/sessions/{session.data['id']}/messages/", {
            "content": "我想创建一个魔法体系，名字叫星辰魔法，它来自星辰辐射。",
        }, format="json")
        self.assertEqual(message.status_code, 200)
        stream = b"".join(message.streaming_content).decode("utf-8")
        self.assertIn("event: complete", stream)
        proposal = EntityProposal.objects.get(canvas_id=self.canvas_id)

        preview = self.client.post(f"/api/v1/canvases/{self.canvas_id}/preview/", {
            "proposal_ids": [str(proposal.id)], "relation_proposal_ids": [],
        }, format="json")
        self.assertEqual(preview.status_code, 200)
        commit = self.client.post(f"/api/v1/canvases/{self.canvas_id}/commit/", {
            "proposal_ids": [str(proposal.id)],
            "relation_proposal_ids": [],
            "idempotency_key": "test-commit-1",
            "preview_token": preview.data["preview_token"],
        }, format="json")
        self.assertEqual(commit.status_code, 200)
        canvas = __import__("apps.canvas.models", fromlist=["StagingCanvas"]).StagingCanvas.objects.get(id=self.canvas_id)
        self.assertEqual(canvas.branch.name.startswith("canvas-"), True)
        self.assertEqual(self.client.get(f"/api/v1/graph/?workspace={self.workspace_id}").data["nodes"], [])
        branch_graph = self.client.get(f"/api/v1/graph/?workspace={self.workspace_id}&branch={canvas.branch_id}")
        self.assertEqual(len(branch_graph.data["nodes"]), 1)
        branch_entity_id = branch_graph.data["nodes"][0]["data"]["id"]
        # Detail and link queries must resolve the selected branch, not silently
        # fall back to main when an entity only exists in the branch.
        detail_url = f"/api/v1/entities/{branch_entity_id}/?workspace={self.workspace_id}&branch={canvas.branch_id}"
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/entities/{branch_entity_id}/?workspace={self.workspace_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/entities/{branch_entity_id}/links/?branch={canvas.branch_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/entities/{branch_entity_id}/?workspace={self.workspace_id}&branch=missing-branch").status_code, 404)
        self.assertEqual(Entity.objects.filter(title=proposal.title).count(), 1)
        self.assertEqual(CommitJob.objects.count(), 1)
        self.assertEqual(commit.data["job"]["status"], "synced")
        from apps.version_control.services.git_sync import GitRepositoryService
        git = GitRepositoryService(canvas.workspace)
        branch_history = git.branch_checkout(canvas.branch)._run(["log", "--format=%s", "-1"])
        self.assertIn("Confirm", branch_history)
        self.assertEqual(git._run(["show", f"main:settings/{Entity.objects.get(title=proposal.title).id}.md"], check=False), "")
        self.merge_canvas_branch()

        repeated = self.client.post(f"/api/v1/canvases/{self.canvas_id}/commit/", {
            "proposal_ids": [str(proposal.id)], "idempotency_key": "test-commit-1",
        }, format="json")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(Entity.objects.filter(title=proposal.title).count(), 1)

    def merge_canvas_branch(self):
        from apps.canvas.models import StagingCanvas
        canvas=StagingCanvas.objects.get(id=self.canvas_id)
        preview=self.client.post(f"/api/v1/branches/{canvas.branch_id}/merge-preview/",{},format="json")
        self.assertEqual(preview.status_code,200)
        merged=self.client.post(f"/api/v1/branches/{canvas.branch_id}/merge/",{"preview_token":preview.data["preview_token"],"resolutions":{}},format="json")
        self.assertEqual(merged.status_code,200)
        return merged

    def test_graph_links_and_provider_fallback(self):
        proposals = []
        for entity_type, title, content in [
            ("canonical_setting", "星辰魔法", "来自星辰。"),
            ("floating_tip", "白昼衰减", "白天减弱。"),
        ]:
            response = self.client.post("/api/v1/proposals/", {
                "workspace": self.workspace_id, "canvas": self.canvas_id,
                "entity_type": entity_type, "title": title, "content": content,
            }, format="json")
            self.assertEqual(response.status_code, 201)
            proposals.append(response.data)
        relation = self.client.post("/api/v1/relation-proposals/", {
            "source_proposal": proposals[1]["id"],
            "target_proposal": proposals[0]["id"], "relation_type": "LINKED_TO",
        }, format="json")
        self.assertEqual(relation.status_code, 201)
        preview = self.client.post(f"/api/v1/canvases/{self.canvas_id}/preview/", {
            "proposal_ids": [p["id"] for p in proposals],
            "relation_proposal_ids": [relation.data["id"]],
        }, format="json")
        self.assertEqual(preview.status_code, 200)
        committed = self.client.post(f"/api/v1/canvases/{self.canvas_id}/commit/", {
            "proposal_ids": [p["id"] for p in proposals],
            "relation_proposal_ids": [relation.data["id"]],
            "idempotency_key": "graph-commit", "preview_token": preview.data["preview_token"],
        }, format="json")
        self.assertEqual(committed.status_code, 200)
        merge_result = self.merge_canvas_branch()
        entity_a = Entity.objects.get(title="星辰魔法")
        entity_b = Entity.objects.get(title="白昼衰减")
        graph = self.client.get(f"/api/v1/graph/?workspace={self.workspace_id}")
        self.assertEqual(len(graph.data["nodes"]), 2)
        links = self.client.get(f"/api/v1/entities/{entity_a.id}/links/")
        self.assertEqual(len(links.data["incoming"]), 1)
        self.assertEqual(links.data["incoming"][0]["otherEntity"]["id"], str(entity_b.id))
        job = CommitJob.objects.get(id=committed.data["job"]["id"])
        history = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/git/history/")
        self.assertEqual(history.status_code, 200)
        latest_diff = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/git/diff/")
        self.assertEqual(latest_diff.status_code, 200)
        self.assertIn("星辰魔法", latest_diff.data["diff"])
        between = self.client.get(
            f"/api/v1/workspaces/{self.workspace_id}/git/diff/?from={merge_result.data['job']['commit_hash']}&to={merge_result.data['job']['commit_hash']}"
        )
        self.assertEqual(between.status_code, 200)
        self.assertEqual(between.data["diff"], "")

        models = self.client.get("/api/v1/ai/providers/default/models/")
        self.assertEqual(models.status_code, 200)
        self.assertEqual(models.data["models"][0]["id"], "local-test-model")
        self.assertNotIn("api_key", models.data)

    def test_formal_entities_cannot_bypass_proposal_review(self):
        entity = self.client.post("/api/v1/entities/", {
            "workspace": self.workspace_id, "type": "canonical_setting", "title": "越权实体", "content": "不应直接创建",
        }, format="json")
        self.assertEqual(entity.status_code, 405)
        self.assertFalse(Entity.objects.filter(title="越权实体").exists())

    def test_workspace_assigns_repo_path(self):
        workspace = WorldWorkspace.objects.get(id=self.workspace_id)
        self.assertTrue(workspace.repo_path)

class AccountAccessTests(TestCase):
    def test_bootstrap_is_one_time_and_workspace_isolation(self):
        client = APIClient()
        self.assertEqual(client.get("/api/v1/workspaces/").status_code, 403)
        first = client.post("/api/v1/auth/bootstrap/", {"username":"root", "password":"secure-password-1", "email":"root@example.test"}, format="json")
        self.assertEqual(first.status_code, 201)
        second = APIClient().post("/api/v1/auth/bootstrap/", {"username":"attacker", "password":"secure-password-2"}, format="json")
        self.assertEqual(second.status_code, 409)
        workspace = client.post("/api/v1/workspaces/", {"name":"私有世界"}, format="json")
        self.assertEqual(workspace.status_code, 201)
        outsider = APIClient()
        outsider.post("/api/v1/auth/login/", {"username":"root", "password":"secure-password-1"}, format="json")
        self.assertEqual(outsider.get("/api/v1/workspaces/").status_code, 200)
        self.assertEqual(len(outsider.get("/api/v1/workspaces/").data), 1)
        self.assertEqual(outsider.get("/api/v1/workspaces/" + workspace.data["id"] + "/").status_code, 200)

    def test_reader_cannot_mutate_canvas(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import WorkspaceMembership
        owner_client=APIClient(); owner_client.post("/api/v1/auth/bootstrap/", {"username":"admin","password":"secure-password-3"},format="json")
        ws=owner_client.post("/api/v1/workspaces/", {"name":"World"},format="json").data
        canvas=owner_client.post("/api/v1/canvases/", {"workspace":ws["id"],"name":"Canvas","snapshot":{}},format="json").data
        reader=get_user_model().objects.create_user(username="reader",password="secure-password-4")
        WorkspaceMembership.objects.create(workspace_id=ws["id"],user=reader,role="reader")
        reader_client=APIClient(); reader_client.force_authenticate(reader)
        self.assertEqual(reader_client.patch(f"/api/v1/canvases/{canvas['id']}/", {"snapshot":{"x":1},"expected_version":1},format="json").status_code,403)

class InviteSignupTests(TestCase):
    def test_invited_user_can_register_once_but_open_signup_is_closed(self):
        owner=APIClient(); owner.post("/api/v1/auth/bootstrap/", {"username":"owner","password":"secure-password-5","email":"owner@example.test"},format="json")
        workspace=owner.post("/api/v1/workspaces/", {"name":"Invite World"},format="json").data
        invite=owner.post("/api/v1/members/", {"workspace":workspace["id"],"email":"writer@example.test","role":"editor"},format="json")
        self.assertEqual(invite.status_code,201)
        visitor=APIClient()
        response=visitor.post("/api/v1/invites/signup/", {"token":invite.data["token"],"username":"writer","email":"writer@example.test","password":"secure-password-6"},format="json")
        self.assertEqual(response.status_code,201)
        self.assertEqual(visitor.get("/api/v1/workspaces/").data[0]["id"],workspace["id"])
        replay=APIClient().post("/api/v1/invites/signup/", {"token":invite.data["token"],"username":"writer2","email":"writer@example.test","password":"secure-password-7"},format="json")
        self.assertEqual(replay.status_code,400)


class BranchMergeTests(TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name))
        self.settings_override.enable()
        self.client=APIClient()
        self.client.post("/api/v1/auth/bootstrap/", {"username":"owner","password":"strong-passphrase-1"},format="json")
        self.workspace=self.client.post("/api/v1/workspaces/", {"name":"Branch World"},format="json").data
        self.canvas=self.client.post("/api/v1/canvases/", {"workspace":self.workspace["id"],"name":"Draft"},format="json").data
    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()
    def commit_title(self,title,key):
        p=self.client.post("/api/v1/proposals/",{"workspace":self.workspace["id"],"canvas":self.canvas["id"],"entity_type":"item","title":title,"content":"draft"},format="json").data
        preview=self.client.post(f"/api/v1/canvases/{self.canvas['id']}/preview/",{"proposal_ids":[p["id"]],"relation_proposal_ids":[]},format="json").data
        return self.client.post(f"/api/v1/canvases/{self.canvas['id']}/commit/",{"proposal_ids":[p["id"]],"relation_proposal_ids":[],"idempotency_key":key,"preview_token":preview["preview_token"]},format="json")
    def test_merge_conflict_requires_explicit_user_resolution_and_is_idempotent(self):
        from apps.canvas.models import StagingCanvas
        from apps.core.models import Entity
        first=self.commit_title("同名器物","first")
        branch_id=StagingCanvas.objects.get(id=self.canvas["id"]).branch_id
        self.assertEqual(first.data["job"]["status"], "synced")
        branch_history=self.client.get(f"/api/v1/workspaces/{self.workspace['id']}/git/history/?branch={branch_id}")
        self.assertEqual(branch_history.status_code,200)
        self.assertTrue(any("Confirm" in item["message"] for item in branch_history.data["history"]))
        main_history=self.client.get(f"/api/v1/workspaces/{self.workspace['id']}/git/history/")
        self.assertFalse(any("Confirm item" in item["message"] for item in main_history.data["history"]))
        preview=self.client.post(f"/api/v1/branches/{branch_id}/merge-preview/",{},format="json")
        self.assertEqual(preview.status_code,200)
        self.client.post(f"/api/v1/branches/{branch_id}/merge/",{"preview_token":preview.data["preview_token"]},format="json").data
        self.assertEqual(Entity.objects.filter(workspace_id=self.workspace["id"],branch__isnull=True).count(),1)
        second_canvas=self.client.post("/api/v1/canvases/",{"workspace":self.workspace["id"],"name":"Second"},format="json").data
        self.canvas=second_canvas
        self.commit_title("同名器物","second")
        branch2=StagingCanvas.objects.get(id=second_canvas["id"]).branch_id
        preview2=self.client.post(f"/api/v1/branches/{branch2}/merge-preview/",{},format="json")
        self.assertEqual(len(preview2.data["conflicts"]),1)
        unresolved=self.client.post(f"/api/v1/branches/{branch2}/merge/",{"preview_token":preview2.data["preview_token"]},format="json")
        self.assertEqual(unresolved.status_code,400)
        conflict_id=preview2.data["conflicts"][0]["branch_entity"]
        merged=self.client.post(f"/api/v1/branches/{branch2}/merge/",{"preview_token":preview2.data["preview_token"],"resolutions":{conflict_id:"keep_both"}},format="json")
        self.assertEqual(merged.status_code,200)
        self.assertEqual(Entity.objects.filter(workspace_id=self.workspace["id"],branch__isnull=True,title="同名器物").count(),2)
        repeated=self.client.post(f"/api/v1/branches/{branch2}/merge/",{"preview_token":"stale"},format="json")
        self.assertEqual(repeated.status_code,200)
        self.assertTrue(repeated.data["idempotent"])

    def test_materialized_baseline_isolation_and_unchanged_rows_do_not_overwrite_main(self):
        from apps.core.models import WorldBranch

        original = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="基线物品", content="分支创建时的内容",
        )
        created = self.client.post("/api/v1/branches/", {
            "workspace": self.workspace["id"], "name": "isolated-view",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        branch_id = created.data["id"]
        branch_row = Entity.objects.get(branch_id=branch_id, base_entity=original)

        # A later main addition is not implicitly visible in this immutable view.
        late = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="后来加入", content="只在 main",
        )
        original.title = "main 后续修改"
        original.content = "main 新内容"
        original.save(update_fields=["title", "content", "updated_at"])

        graph = self.client.get(f"/api/v1/graph/?workspace={self.workspace['id']}&branch={branch_id}")
        titles = {node["data"]["title"] for node in graph.data["nodes"]}
        self.assertIn("基线物品", titles)
        self.assertNotIn("后来加入", titles)
        self.assertNotEqual(branch_row.id, original.id)

        preview = self.client.post(f"/api/v1/branches/{branch_id}/merge-preview/", {}, format="json")
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.data["conflicts"])
        merged = self.client.post(f"/api/v1/branches/{branch_id}/merge/", {
            "preview_token": preview.data["preview_token"],
        }, format="json")
        self.assertEqual(merged.status_code, 200, merged.data)
        original.refresh_from_db()
        self.assertEqual(original.title, "main 后续修改")
        self.assertEqual(original.content, "main 新内容")
        self.assertTrue(Entity.objects.filter(pk=late.pk, branch__isnull=True).exists())
        self.assertEqual(WorldBranch.objects.get(pk=branch_id).status, WorldBranch.Status.MERGED)

    def test_three_way_merge_auto_combines_non_overlapping_entity_fields(self):
        entity = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="基线标题", content="基线内容",
            metadata={"origin": "base"},
        )
        branch = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace["id"], "name": "non-overlap-merge"}, format="json",
        ).data["id"]

        branch_update = self.client.patch(
            f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}",
            {"content": "分支内容"}, format="json",
        )
        self.assertEqual(branch_update.status_code, 200, branch_update.data)
        entity.title = "主线标题"
        entity.save(update_fields=["title", "updated_at"])

        preview = self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertFalse(preview.data["conflicts"])
        change = next(item for item in preview.data["entity_changes"] if item["base_entity"] == str(entity.id))
        self.assertTrue(change["auto_mergeable"])
        self.assertEqual(change["merge_candidate"]["title"], "主线标题")
        self.assertEqual(change["merge_candidate"]["content"], "分支内容")

        merged = self.client.post(
            f"/api/v1/branches/{branch}/merge/",
            {"preview_token": preview.data["preview_token"], "resolutions": {}}, format="json",
        )
        self.assertEqual(merged.status_code, 200, merged.data)
        entity.refresh_from_db()
        self.assertEqual(entity.title, "主线标题")
        self.assertEqual(entity.content, "分支内容")
        self.assertEqual(entity.metadata, {"origin": "base"})

    def test_three_way_merge_accepts_field_resolution_and_user_final_value(self):
        entity = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="字段冲突", content="基线内容",
        )
        branch = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace["id"], "name": "field-resolution"}, format="json",
        ).data["id"]
        self.assertEqual(
            self.client.patch(
                f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}",
                {"content": "分支内容"}, format="json",
            ).status_code,
            200,
        )
        entity.content = "主线内容"
        entity.save(update_fields=["content", "updated_at"])

        preview = self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json")
        conflict = next(item for item in preview.data["conflicts"] if item["kind"] == "entity_update")
        self.assertEqual(conflict["conflicting_fields"], ["content"])
        self.assertEqual(conflict["field_changes"]["content"]["base"], "基线内容")
        self.assertEqual(conflict["field_changes"]["content"]["main"], "主线内容")
        self.assertEqual(conflict["field_changes"]["content"]["branch"], "分支内容")

        merged = self.client.post(
            f"/api/v1/branches/{branch}/merge/",
            {
                "preview_token": preview.data["preview_token"],
                "resolutions": {
                    conflict["branch_entity"]: {
                        "fields": {"content": {"value": "共同确认后的内容"}},
                    },
                },
            }, format="json",
        )
        self.assertEqual(merged.status_code, 200, merged.data)
        entity.refresh_from_db()
        self.assertEqual(entity.content, "共同确认后的内容")

    def test_ai_merge_suggestion_is_non_binding_and_field_scoped(self):
        from unittest.mock import Mock, patch

        entity = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="AI 冲突", content="基线内容",
        )
        branch = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace["id"], "name": "ai-suggestion"}, format="json",
        ).data["id"]
        self.client.patch(
            f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}",
            {"content": "分支内容"}, format="json",
        )
        entity.content = "主线内容"
        entity.save(update_fields=["content", "updated_at"])
        preview = self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json")
        conflict = next(item for item in preview.data["conflicts"] if item["kind"] == "entity_update")
        provider = Mock()
        provider.extract_structured_data.return_value = {
            "suggestions": [{
                "conflict_id": conflict["branch_entity"],
                "choice": "keep_branch",
                "reason": "分支内容补充了当前设定的最新约束。",
                "confidence": 0.82,
                "fields": {
                    "content": {"choice": "keep_branch", "reason": "分支更完整。", "confidence": 0.82},
                },
            }],
        }
        with patch("apps.core.views.configured_provider", return_value=provider):
            response = self.client.post(
                f"/api/v1/branches/{branch}/merge-suggestion/",
                {"preview_token": preview.data["preview_token"], "model": "review-model"},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "suggested")
        self.assertEqual(response.data["suggestions"][0]["choice"], "keep_branch")
        self.assertEqual(response.data["suggestions"][0]["fields"]["content"]["choice"], "keep_branch")
        provider.extract_structured_data.assert_called_once()
        entity.refresh_from_db()
        self.assertEqual(entity.content, "主线内容")

    def test_ai_merge_suggestion_failure_preserves_preview_for_manual_review(self):
        from unittest.mock import Mock, patch
        from apps.ai_agent.providers import ProviderError

        entity = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="AI 失败", content="基线内容",
        )
        branch = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace["id"], "name": "ai-failure"}, format="json",
        ).data["id"]
        self.client.patch(
            f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}",
            {"content": "分支内容"}, format="json",
        )
        entity.content = "主线内容"
        entity.save(update_fields=["content", "updated_at"])
        preview = self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json")
        provider = Mock()
        provider.extract_structured_data.side_effect = ProviderError("上游不可用")
        with patch("apps.core.views.configured_provider", return_value=provider):
            response = self.client.post(
                f"/api/v1/branches/{branch}/merge-suggestion/",
                {"preview_token": preview.data["preview_token"]},
                format="json",
            )
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["status"], "unavailable")
        self.assertEqual(response.data["preview_token"], preview.data["preview_token"])
        self.assertEqual(response.data["suggestions"], [])
        self.assertEqual(self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json").status_code, 200)
        entity.refresh_from_db()
        self.assertEqual(entity.content, "主线内容")

    def test_ai_merge_suggestion_rejects_stale_preview_before_calling_provider(self):
        from unittest.mock import Mock, patch

        entity = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="过期建议", content="基线内容",
        )
        branch = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace["id"], "name": "stale-suggestion"}, format="json",
        ).data["id"]
        self.client.patch(
            f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}",
            {"content": "分支内容"}, format="json",
        )
        entity.content = "主线内容"
        entity.save(update_fields=["content", "updated_at"])
        preview = self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json")
        provider = Mock()
        with patch("apps.core.views.configured_provider", return_value=provider):
            response = self.client.post(
                f"/api/v1/branches/{branch}/merge-suggestion/",
                {"preview_token": "stale-token"}, format="json",
            )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["preview"]["preview_token"], preview.data["preview_token"])
        provider.extract_structured_data.assert_not_called()

    def test_ai_merge_suggestion_rejects_untrusted_provider_output(self):
        from unittest.mock import Mock, patch

        entity = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="非法建议", content="基线内容",
        )
        branch = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace["id"], "name": "invalid-suggestion"}, format="json",
        ).data["id"]
        self.client.patch(
            f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}",
            {"content": "分支内容"}, format="json",
        )
        entity.content = "主线内容"
        entity.save(update_fields=["content", "updated_at"])
        preview = self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json")
        provider = Mock()
        provider.extract_structured_data.return_value = {
            "suggestions": [{
                "conflict_id": "not-a-real-conflict",
                "choice": "keep_branch",
                "reason": "不应被接受",
                "confidence": 1,
            }],
        }
        with patch("apps.core.views.configured_provider", return_value=provider):
            response = self.client.post(
                f"/api/v1/branches/{branch}/merge-suggestion/",
                {"preview_token": preview.data["preview_token"]}, format="json",
            )
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["status"], "unavailable")
        entity.refresh_from_db()
        self.assertEqual(entity.content, "主线内容")

    def test_field_resolution_keep_branch_is_backward_compatible_with_whole_row_choice(self):
        entity = Entity.objects.create(
            workspace_id=self.workspace["id"], type="item", title="保留字段", content="基线内容",
        )
        branch = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace["id"], "name": "field-keep-branch"}, format="json",
        ).data["id"]
        self.client.patch(
            f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}",
            {"content": "分支内容"}, format="json",
        )
        entity.content = "主线内容"
        entity.save(update_fields=["content", "updated_at"])
        preview = self.client.post(f"/api/v1/branches/{branch}/merge-preview/", {}, format="json")
        conflict = next(item for item in preview.data["conflicts"] if item["kind"] == "entity_update")
        merged = self.client.post(
            f"/api/v1/branches/{branch}/merge/",
            {
                "preview_token": preview.data["preview_token"],
                "resolutions": {
                    conflict["branch_entity"]: {"fields": {"content": "keep_branch"}},
                },
            }, format="json",
        )
        self.assertEqual(merged.status_code, 200, merged.data)
        entity.refresh_from_db()
        self.assertEqual(entity.content, "分支内容")

    def test_branch_entity_and_relation_archives_merge_as_tombstones(self):
        from apps.core.models import Relation

        source = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="源")
        target = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="目标")
        relation = Relation.objects.create(
            workspace_id=self.workspace["id"], source=source, target=target,
            relation_type="LINKED_TO",
        )
        created = self.client.post("/api/v1/branches/", {
            "workspace": self.workspace["id"], "name": "archive-changes",
        }, format="json")
        branch_id = created.data["id"]

        archived_entity = self.client.delete(
            f"/api/v1/entities/{source.id}/?workspace={self.workspace['id']}&branch={branch_id}"
        )
        self.assertEqual(archived_entity.status_code, 204)
        archived_relation = self.client.delete(
            f"/api/v1/relations/{relation.id}/?workspace={self.workspace['id']}&branch={branch_id}"
        )
        self.assertEqual(archived_relation.status_code, 204)
        self.assertTrue(Entity.objects.get(pk=source.pk).status == Entity.Status.ACTIVE)
        self.assertFalse(Relation.objects.get(pk=relation.pk).archived)

        preview = self.client.post(f"/api/v1/branches/{branch_id}/merge-preview/", {}, format="json")
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.data["conflicts"])
        merged = self.client.post(f"/api/v1/branches/{branch_id}/merge/", {
            "preview_token": preview.data["preview_token"],
        }, format="json")
        self.assertEqual(merged.status_code, 200, merged.data)
        source.refresh_from_db()
        relation.refresh_from_db()
        self.assertEqual(source.status, Entity.Status.ARCHIVED)
        self.assertTrue(relation.archived)
        # The branch retains its copy-on-write rows for audit/history.
        self.assertTrue(Entity.objects.filter(branch_id=branch_id, base_entity=source).exists())
        self.assertTrue(Relation.objects.filter(branch_id=branch_id, base_relation=relation).exists())

    def _merge_branch(self, branch_id, resolutions=None):
        preview = self.client.post(f"/api/v1/branches/{branch_id}/merge-preview/", {}, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        payload = {"preview_token": preview.data["preview_token"]}
        if resolutions:
            payload["resolutions"] = resolutions
        response = self.client.post(f"/api/v1/branches/{branch_id}/merge/", payload, format="json")
        return preview, response

    def test_main_archive_and_unchanged_branch_preserves_main_tombstone(self):
        entity = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="被归档物品", content="原始内容")
        branch = self.client.post("/api/v1/branches/", {"workspace": self.workspace["id"], "name": "archive-main-unchanged"}, format="json").data["id"]
        self.assertEqual(self.client.delete(f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}").status_code, 204)
        preview, merged = self._merge_branch(branch)
        self.assertFalse(preview.data["conflicts"])
        self.assertEqual(merged.status_code, 200, merged.data)
        entity.refresh_from_db()
        self.assertEqual(entity.status, Entity.Status.ARCHIVED)

    def test_main_archive_and_branch_edit_requires_resolution(self):
        entity = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="归档冲突物品", content="基线")
        branch = self.client.post("/api/v1/branches/", {"workspace": self.workspace["id"], "name": "archive-entity-conflict"}, format="json").data["id"]
        self.assertEqual(self.client.patch(f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}", {"content": "分支继续维护"}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}").status_code, 204)
        preview, unresolved = self._merge_branch(branch)
        self.assertEqual(unresolved.status_code, 400)
        conflict = next(item for item in preview.data["conflicts"] if item["kind"] == "entity_update")
        _, keep_main = self._merge_branch(branch, {conflict["branch_entity"]: "keep_main"})
        self.assertEqual(keep_main.status_code, 200, keep_main.data)
        entity.refresh_from_db()
        self.assertEqual(entity.status, Entity.Status.ARCHIVED)

    def test_main_archive_and_branch_edit_keep_branch_restores_active_entity(self):
        entity = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="恢复冲突物品", content="基线")
        branch = self.client.post("/api/v1/branches/", {"workspace": self.workspace["id"], "name": "restore-entity-conflict"}, format="json").data["id"]
        self.assertEqual(self.client.patch(f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}", {"content": "保留分支版本"}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}").status_code, 204)
        preview, _ = self._merge_branch(branch)
        conflict = next(item for item in preview.data["conflicts"] if item["kind"] == "entity_update")
        _, merged = self._merge_branch(branch, {conflict["branch_entity"]: "keep_branch"})
        self.assertEqual(merged.status_code, 200, merged.data)
        entity.refresh_from_db()
        self.assertEqual(entity.status, Entity.Status.ACTIVE)
        self.assertEqual(entity.content, "保留分支版本")

    def test_branch_archive_merges_entity_tombstone_without_conflict(self):
        entity = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="分支归档物品", content="基线")
        branch = self.client.post("/api/v1/branches/", {"workspace": self.workspace["id"], "name": "branch-archive-entity"}, format="json").data["id"]
        self.assertEqual(self.client.delete(f"/api/v1/entities/{entity.id}/?workspace={self.workspace['id']}&branch={branch}").status_code, 204)
        preview, merged = self._merge_branch(branch)
        self.assertFalse(preview.data["conflicts"])
        self.assertEqual(merged.status_code, 200, merged.data)
        entity.refresh_from_db()
        self.assertEqual(entity.status, Entity.Status.ARCHIVED)

    def test_main_archive_and_branch_edit_relation_requires_resolution(self):
        from apps.core.models import Relation
        source = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="关系源")
        target = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="关系目标")
        relation = Relation.objects.create(workspace_id=self.workspace["id"], source=source, target=target, relation_type="LINKED_TO", properties={"strength": 1})
        branch = self.client.post("/api/v1/branches/", {"workspace": self.workspace["id"], "name": "archive-relation-conflict"}, format="json").data["id"]
        self.assertEqual(self.client.patch(f"/api/v1/relations/{relation.id}/?workspace={self.workspace['id']}&branch={branch}", {"properties": {"strength": 2}}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/relations/{relation.id}/?workspace={self.workspace['id']}").status_code, 204)
        preview, unresolved = self._merge_branch(branch)
        self.assertEqual(unresolved.status_code, 400)
        conflict = next(item for item in preview.data["conflicts"] if item["kind"] == "relation_update")
        _, keep_main = self._merge_branch(branch, {conflict["branch_relation"]: "keep_main"})
        self.assertEqual(keep_main.status_code, 200, keep_main.data)
        relation.refresh_from_db()
        self.assertTrue(relation.archived)

    def test_branch_archive_merges_relation_tombstone_without_conflict(self):
        from apps.core.models import Relation
        source = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="关系归档源")
        target = Entity.objects.create(workspace_id=self.workspace["id"], type="item", title="关系归档目标")
        relation = Relation.objects.create(workspace_id=self.workspace["id"], source=source, target=target, relation_type="LINKED_TO")
        branch = self.client.post("/api/v1/branches/", {"workspace": self.workspace["id"], "name": "branch-archive-relation"}, format="json").data["id"]
        self.assertEqual(self.client.delete(f"/api/v1/relations/{relation.id}/?workspace={self.workspace['id']}&branch={branch}").status_code, 204)
        preview, merged = self._merge_branch(branch)
        self.assertFalse(preview.data["conflicts"])
        self.assertEqual(merged.status_code, 200, merged.data)
        relation.refresh_from_db()
        self.assertTrue(relation.archived)

    def test_explicit_git_branch_baseline_and_archive_are_auditable(self):
        created=self.client.post("/api/v1/branches/",{"workspace":self.workspace["id"],"name":"research"},format="json")
        self.assertEqual(created.status_code,201)
        self.assertTrue(created.data["base_commit"])
        self.assertEqual(created.data["git_ref"],f"oc/branch/{created.data['id'].replace('-', '')}")
        archived=self.client.post(f"/api/v1/branches/{created.data['id']}/archive/",{},format="json")
        self.assertEqual(archived.status_code,200)
        repeated=self.client.post(f"/api/v1/branches/{created.data['id']}/archive/",{},format="json")
        self.assertTrue(repeated.data["idempotent"])
        self.assertEqual(self.client.get(f"/api/v1/graph/?workspace={self.workspace['id']}&branch=research").status_code,404)

    def test_projection_outbox_jobs_can_fail_and_be_retried(self):
        from unittest.mock import patch
        from django.core.management import call_command
        from apps.core.models import GraphProjectionJob
        from apps.core.services.neo4j_projection import Neo4jProjection

        failed = GraphProjectionJob.objects.create(workspace_id=self.workspace["id"])
        with patch.object(Neo4jProjection, "rebuild", side_effect=RuntimeError("Neo4j offline")):
            ok, error = Neo4jProjection().process_job(failed)
        self.assertFalse(ok)
        self.assertEqual(error, "Neo4j offline")
        failed.refresh_from_db()
        self.assertEqual(failed.status, GraphProjectionJob.Status.FAILED)
        self.assertEqual(failed.attempts, 1)

        with patch.object(Neo4jProjection, "rebuild", return_value=None):
            call_command("process_graph_projection_jobs", stdout=__import__("io").StringIO())
        failed.refresh_from_db()
        self.assertEqual(failed.status, GraphProjectionJob.Status.SYNCED)
        self.assertEqual(failed.attempts, 2)

class TemporalWorldbuildingTests(TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        self.client = APIClient()
        response = self.client.post("/api/v1/auth/bootstrap/", {"username": "temporal-owner", "password": "correct-horse-battery", "email": "temporal@example.test"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name))
        self.settings_override.enable()
        response = self.client.post("/api/v1/workspaces/", {"name": "Chronology Test"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.workspace_id = response.data["id"]
        canvas_response = self.client.post("/api/v1/canvases/", {"workspace": self.workspace_id, "name": "时间关系审核"}, format="json")
        self.assertEqual(canvas_response.status_code, 201)
        self.canvas_id = canvas_response.data["id"]
        self.branch_id = canvas_response.data["branch"]

    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def test_custom_time_axis_character_lifecycle_and_timeline_slice(self):
        from apps.core.models import Entity, Relation, TimeSystem

        axis_response = self.client.post("/api/v1/time-systems/", {
            "workspace": self.workspace_id,
            "name": "星历",
            "epoch_label": "星海纪元元年",
            "unit_name": "日",
            "units": [{"name": "年", "ticks": 360}, {"name": "月", "ticks": 30}],
            "display_format": "星历 {value} {unit}",
        }, format="json")
        self.assertEqual(axis_response.status_code, 201, axis_response.data)
        axis_id = axis_response.data["id"]

        timeline = Entity.objects.create(workspace_id=self.workspace_id, type="timeline", title="王朝兴衰")
        event = Entity.objects.create(workspace_id=self.workspace_id, type="event", title="月蚀之夜")
        older_character = Entity.objects.create(workspace_id=self.workspace_id, type="character", title="岚")
        younger_character = Entity.objects.create(workspace_id=self.workspace_id, type="character", title="澄")

        for person, start, end in ((older_character, 0, 100), (younger_character, 30, 40)):
            response = self.client.post("/api/v1/character-lifespans/", {
                "workspace": self.workspace_id,
                "character": str(person.id),
                "time_system": axis_id,
                "start_value": start,
                "end_value": end,
            }, format="json")
            self.assertEqual(response.status_code, 201, response.data)

        duplicate = self.client.post("/api/v1/character-lifespans/", {
            "workspace": self.workspace_id, "character": str(older_character.id), "time_system": axis_id,
            "start_value": 1, "end_value": 99,
        }, format="json")
        self.assertEqual(duplicate.status_code, 400)

        invalid = self.client.post("/api/v1/character-lifespans/", {
            "workspace": self.workspace_id,
            "character": str(older_character.id),
            "time_system": axis_id,
            "start_value": 20,
            "end_value": 10,
        }, format="json")
        self.assertEqual(invalid.status_code, 400)

        entry_response = self.client.post("/api/v1/timeline-entries/", {
            "workspace": self.workspace_id,
            "timeline": str(timeline.id),
            "event": str(event.id),
            "time_system": axis_id,
            "start_value": 35,
            "end_value": 35,
            "summary": "两位角色在月蚀之夜相遇。",
            "participants": [
                {"entity": str(older_character.id), "role": "守望者"},
                {"entity": str(younger_character.id), "role": "目击者"},
            ],
        }, format="json")
        self.assertEqual(entry_response.status_code, 201, entry_response.data)
        self.assertEqual(len(entry_response.data["participant_details"]), 2)
        Relation.objects.create(
            workspace_id=self.workspace_id, source=older_character, target=younger_character,
            relation_type="KNOWS", time_system_id=axis_id, valid_from=32, valid_to=38,
        )
        from apps.version_control.services.git_sync import GitRepositoryService
        export = GitRepositoryService(Entity.objects.get(pk=timeline.id).workspace).snapshot()
        self.assertIn("indexes/time-systems.json", export)
        self.assertIn(str(entry_response.data["id"]), export["indexes/timeline-entries.json"])
        self.assertIn(str(older_character.id), export["indexes/character-lifespans.json"])
        self.assertIn(str(axis_id), export["indexes/relations.json"])
        service = GitRepositoryService(Entity.objects.get(pk=timeline.id).workspace)
        service.write_snapshot(export)
        service._run(["add", "."])
        historical_commit = service._commit_if_changed("chronology snapshot")
        historical = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis_id}&at=35&commit={historical_commit}")
        self.assertEqual(historical.status_code, 200, historical.data)
        self.assertEqual(historical.data["commit"], historical_commit)
        self.assertEqual({item["title"] for item in historical.data["characters"]}, {"岚", "澄"})
        self.assertEqual({node["data"]["title"] for node in historical.data["nodes"]}, {"岚", "澄", "月蚀之夜"})
        self.assertEqual(len(historical.data["edges"]), 3)

        before = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis_id}&at=20")
        self.assertEqual(before.status_code, 200, before.data)
        self.assertEqual({item["title"] for item in before.data["characters"]}, {"岚"})
        self.assertEqual(len(before.data["edges"]), 0)

        at_event = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis_id}&at=35")
        self.assertEqual(at_event.status_code, 200, at_event.data)
        self.assertEqual({item["title"] for item in at_event.data["characters"]}, {"岚", "澄"})
        self.assertEqual({node["data"]["title"] for node in at_event.data["nodes"]}, {"岚", "澄", "月蚀之夜"})
        self.assertEqual(len(at_event.data["edges"]), 3)

        after_death = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis_id}&at=50")
        self.assertEqual({item["title"] for item in after_death.data["characters"]}, {"岚"})
        self.assertEqual(after_death.data["edges"], [])

        inaccessible_axis = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system=00000000-0000-0000-0000-000000000000&at=35")
        self.assertEqual(inaccessible_axis.status_code, 404)

    def test_branch_chronology_overlay_is_isolated_and_copy_on_write(self):
        from apps.core.models import CharacterLifespan, Entity, TimeSystem, TimelineEntry

        axis = TimeSystem.objects.create(workspace_id=self.workspace_id, name="branch-axis", unit_name="day")
        other_axis = TimeSystem.objects.create(workspace_id=self.workspace_id, name="other-branch-axis", unit_name="day")
        timeline = Entity.objects.create(workspace_id=self.workspace_id, type="timeline", title="主线年表")
        event = Entity.objects.create(workspace_id=self.workspace_id, type="event", title="相遇")
        character = Entity.objects.create(workspace_id=self.workspace_id, type="character", title="主线人物")
        main_entry = TimelineEntry.objects.create(
            workspace_id=self.workspace_id, timeline=timeline, event=event, time_system=axis,
            start_value=10, summary="main value",
        )
        main_span = CharacterLifespan.objects.create(
            workspace_id=self.workspace_id, character=character, time_system=axis,
            start_value=0, end_value=100,
        )

        url = f"?workspace={self.workspace_id}&branch={self.branch_id}"
        changed_entry = self.client.patch(f"/api/v1/timeline-entries/{main_entry.id}/{url}", {
            "start_value": 25, "summary": "branch value",
        }, format="json")
        self.assertEqual(changed_entry.status_code, 200, changed_entry.data)
        changed_span = self.client.patch(f"/api/v1/character-lifespans/{main_span.id}/{url}", {
            "end_value": 50,
        }, format="json")
        self.assertEqual(changed_span.status_code, 200, changed_span.data)

        main_entry.refresh_from_db(); main_span.refresh_from_db()
        self.assertEqual(main_entry.start_value, 10)
        self.assertEqual(main_span.end_value, 100)
        branch_entries = self.client.get(f"/api/v1/timeline-entries/{url}")
        self.assertEqual(branch_entries.status_code, 200)
        self.assertEqual(len(branch_entries.data), 1)
        self.assertEqual(branch_entries.data[0]["start_value"], 25)
        branch_spans = self.client.get(f"/api/v1/character-lifespans/{url}")
        self.assertEqual(branch_spans.status_code, 200)
        self.assertEqual(len(branch_spans.data), 1)
        self.assertEqual(branch_spans.data[0]["end_value"], 50)

        # Editing an already-created override must retain branch routing. The
        # detail endpoint's default queryset is main-only, so omitting the query
        # context would either 404 or risk writing to the wrong view.
        changed_span_again = self.client.patch(
            f"/api/v1/character-lifespans/{branch_spans.data[0]['id']}/{url}",
            {"end_value": 45}, format="json",
        )
        self.assertEqual(changed_span_again.status_code, 200, changed_span_again.data)
        self.assertEqual(changed_span_again.data["end_value"], 45)
        changed_entry_again = self.client.patch(
            f"/api/v1/timeline-entries/{branch_entries.data[0]['id']}/{url}",
            {"summary": "edited branch override"}, format="json",
        )
        self.assertEqual(changed_entry_again.status_code, 200, changed_entry_again.data)
        main_entry.refresh_from_db(); main_span.refresh_from_db()
        self.assertEqual(main_entry.summary, "main value")
        self.assertEqual(main_span.end_value, 100)

        # A payload cannot relocate a main row into an arbitrary branch; only
        # the validated query context controls copy-on-write routing.
        from apps.core.models import WorldBranch
        other_branch = WorldBranch.objects.create(workspace_id=self.workspace_id, name="other-branch")
        main_patch = self.client.patch(
            f"/api/v1/character-lifespans/{main_span.id}/",
            {"end_value": 90, "branch": str(other_branch.id)}, format="json",
        )
        self.assertEqual(main_patch.status_code, 200, main_patch.data)
        main_span.refresh_from_db()
        self.assertIsNone(main_span.branch_id)
        self.assertEqual(main_span.end_value, 90)
        from apps.version_control.services.git_sync import GitRepositoryService
        branch_export = GitRepositoryService(WorldWorkspace.objects.get(pk=self.workspace_id)).snapshot(
            branch=__import__("apps.core.models", fromlist=["WorldBranch"]).WorldBranch.objects.get(pk=self.branch_id),
        )
        export_entries = __import__("json").loads(branch_export["indexes/timeline-entries.json"])
        export_spans = __import__("json").loads(branch_export["indexes/character-lifespans.json"])
        self.assertEqual(len([row for row in export_entries if row["timeline"] == str(timeline.id) and row["event"] == str(event.id)]), 1)
        self.assertEqual(len([row for row in export_spans if row["character"] == str(character.id)]), 1)
        self.assertEqual(next(row for row in export_entries if row["timeline"] == str(timeline.id))["start_value"], 25)
        self.assertEqual(next(row for row in export_spans if row["character"] == str(character.id))["end_value"], 45)

        main_at_10 = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.id}&at=10")
        branch_at_10 = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.id}&at=10&branch={self.branch_id}")
        self.assertIn("相遇", {node["data"]["title"] for node in main_at_10.data["nodes"]})
        self.assertNotIn("相遇", {node["data"]["title"] for node in branch_at_10.data["nodes"]})
        branch_at_25 = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.id}&at=25&branch={self.branch_id}")
        self.assertIn("相遇", {node["data"]["title"] for node in branch_at_25.data["nodes"]})
        main_at_60 = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.id}&at=60")
        branch_at_60 = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.id}&at=60&branch={self.branch_id}")
        self.assertIn("主线人物", {person["title"] for person in main_at_60.data["characters"]})
        self.assertNotIn("主线人物", {person["title"] for person in branch_at_60.data["characters"]})

        refused_delete = self.client.delete(f"/api/v1/timeline-entries/{main_entry.id}/{url}")
        self.assertEqual(refused_delete.status_code, 404)
        self.assertTrue(TimelineEntry.objects.filter(pk=main_entry.pk).exists())

    def test_branch_chronology_delete_uses_tombstones_and_merge_removes_main(self):
        from apps.core.models import CharacterLifespan, Entity, TimeSystem, TimelineEntry, WorldBranch

        axis = TimeSystem.objects.create(workspace_id=self.workspace_id, name="delete-axis")
        timeline = Entity.objects.create(workspace_id=self.workspace_id, type="timeline", title="删除测试年表")
        event = Entity.objects.create(workspace_id=self.workspace_id, type="event", title="删除测试事件")
        character = Entity.objects.create(workspace_id=self.workspace_id, type="character", title="删除测试角色")
        main_entry = TimelineEntry.objects.create(
            workspace_id=self.workspace_id, timeline=timeline, event=event, time_system=axis,
            start_value=10, summary="keep on main",
        )
        main_span = CharacterLifespan.objects.create(
            workspace_id=self.workspace_id, character=character, time_system=axis,
            start_value=0, end_value=100,
        )
        context = f"?workspace={self.workspace_id}&branch={self.branch_id}"
        deleted_entry = self.client.delete(f"/api/v1/timeline-entries/{main_entry.id}/{context}")
        deleted_span = self.client.delete(f"/api/v1/character-lifespans/{main_span.id}/{context}")
        self.assertEqual(deleted_entry.status_code, 204, deleted_entry.data)
        self.assertEqual(deleted_span.status_code, 204, deleted_span.data)
        main_entry.refresh_from_db(); main_span.refresh_from_db()
        self.assertEqual(main_entry.summary, "keep on main")
        self.assertEqual(main_span.end_value, 100)
        self.assertFalse(self.client.get(f"/api/v1/timeline-entries/{context}").data)
        self.assertFalse(self.client.get(f"/api/v1/character-lifespans/{context}").data)
        self.assertTrue(TimelineEntry.objects.filter(branch_id=self.branch_id, archived=True, timeline=timeline, event=event).exists())
        self.assertTrue(CharacterLifespan.objects.filter(branch_id=self.branch_id, archived=True, character=character).exists())

        preview = self.client.post(f"/api/v1/branches/{self.branch_id}/merge-preview/", {}, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        merged = self.client.post(f"/api/v1/branches/{self.branch_id}/merge/", {
            "preview_token": preview.data["preview_token"], "resolutions": {},
        }, format="json")
        self.assertEqual(merged.status_code, 200, merged.data)
        self.assertFalse(TimelineEntry.objects.filter(pk=main_entry.id).exists())
        self.assertFalse(CharacterLifespan.objects.filter(pk=main_span.id).exists())
        self.assertEqual(WorldBranch.objects.get(pk=self.branch_id).status, WorldBranch.Status.MERGED)

    def test_chronology_branch_merge_promotes_overrides_atomically(self):
        from apps.core.models import CharacterLifespan, Entity, TimeSystem, TimelineEntry, WorldBranch

        axis = TimeSystem.objects.create(workspace_id=self.workspace_id, name="merge-axis")
        other_axis = TimeSystem.objects.create(workspace_id=self.workspace_id, name="merge-other-axis")
        timeline = Entity.objects.create(workspace_id=self.workspace_id, type="timeline", title="合并年表")
        event = Entity.objects.create(workspace_id=self.workspace_id, type="event", title="合并事件")
        character = Entity.objects.create(workspace_id=self.workspace_id, type="character", title="合并角色")
        main_entry = TimelineEntry.objects.create(
            workspace_id=self.workspace_id, timeline=timeline, event=event, time_system=axis,
            start_value=5, summary="before",
        )
        main_span = CharacterLifespan.objects.create(
            workspace_id=self.workspace_id, character=character, time_system=axis,
            start_value=0, end_value=90,
        )
        separate_axis_span = CharacterLifespan.objects.create(
            workspace_id=self.workspace_id, character=character, time_system=other_axis,
            start_value=1, end_value=2,
        )
        branch_entry = TimelineEntry.objects.create(
            workspace_id=self.workspace_id, branch_id=self.branch_id, timeline=timeline, event=event,
            time_system=axis, start_value=20, summary="after",
        )
        branch_span = CharacterLifespan.objects.create(
            workspace_id=self.workspace_id, branch_id=self.branch_id, character=character,
            time_system=axis, start_value=0, end_value=40,
        )

        first = self.client.post(f"/api/v1/branches/{self.branch_id}/merge-preview/", {}, format="json")
        second = self.client.post(f"/api/v1/branches/{self.branch_id}/merge-preview/", {}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["preview_token"], second.data["preview_token"])
        merged = self.client.post(f"/api/v1/branches/{self.branch_id}/merge/", {
            "preview_token": first.data["preview_token"], "resolutions": {},
        }, format="json")
        self.assertEqual(merged.status_code, 200, merged.data)

        main_entry.refresh_from_db(); main_span.refresh_from_db()
        self.assertEqual(main_entry.start_value, 20)
        self.assertEqual(main_entry.summary, "after")
        self.assertEqual(main_span.end_value, 40)
        separate_axis_span.refresh_from_db()
        self.assertEqual(separate_axis_span.end_value, 2)
        self.assertFalse(TimelineEntry.objects.filter(pk=branch_entry.pk).exists())
        self.assertFalse(CharacterLifespan.objects.filter(pk=branch_span.pk).exists())
        self.assertEqual(WorldBranch.objects.get(pk=self.branch_id).status, WorldBranch.Status.MERGED)
        repeated = self.client.post(f"/api/v1/branches/{self.branch_id}/merge/", {
            "preview_token": "stale", "resolutions": {},
        }, format="json")
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.data["idempotent"])

    def test_time_system_write_permissions_and_validation(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import WorkspaceMembership
        from apps.core.models import TimeSystem, WorldWorkspace

        axis = TimeSystem.objects.create(workspace_id=self.workspace_id, name="existing")
        reader = get_user_model().objects.create_user(username="timeline-reader", password="reader-password-1")
        WorkspaceMembership.objects.create(workspace_id=self.workspace_id, user=reader, role="reader")
        reader_client = APIClient(); reader_client.force_authenticate(reader)
        self.assertEqual(reader_client.get(f"/api/v1/time-systems/?workspace={self.workspace_id}").status_code, 200)
        self.assertEqual(reader_client.patch(f"/api/v1/time-systems/{axis.id}/", {"name": "tampered"}, format="json").status_code, 403)
        updated_axis = self.client.patch(f"/api/v1/time-systems/{axis.id}/", {"name": "renamed"}, format="json")
        self.assertEqual(updated_axis.status_code, 200, updated_axis.data)
        self.assertEqual(updated_axis.data["name"], "renamed")

        other_workspace = WorldWorkspace.objects.create(name="Another world", slug="another-world")
        invalid_units = self.client.post("/api/v1/time-systems/", {
            "workspace": self.workspace_id, "name": "bad", "units": [{"name": "year", "ticks": 0}],
        }, format="json")
        self.assertEqual(invalid_units.status_code, 400)
        wrong_branch = self.client.post("/api/v1/character-lifespans/", {
            "workspace": self.workspace_id, "branch": str(self.branch_id),
            "character": str(__import__("apps.core.models", fromlist=["Entity"]).Entity.objects.create(
                workspace=other_workspace, type="character", title="foreign").id),
            "time_system": str(axis.id), "start_value": 0,
        }, format="json")
        self.assertEqual(wrong_branch.status_code, 400)

    def test_approved_relationship_proposal_preserves_temporal_validity(self):
        from apps.core.models import Entity, Relation
        axis = self.client.post("/api/v1/time-systems/", {"workspace": self.workspace_id, "name": "纪元", "unit_name": "年", "units": []}, format="json")
        self.assertEqual(axis.status_code, 201, axis.data)
        target = Entity.objects.create(workspace_id=self.workspace_id, type="character", title="目标角色")
        source = self.client.post("/api/v1/proposals/", {
            "workspace": self.workspace_id, "canvas": self.canvas_id, "entity_type": "character", "title": "源角色", "content": ""}, format="json")
        self.assertEqual(source.status_code, 201, source.data)
        relation = self.client.post("/api/v1/relation-proposals/", {
            "source_proposal": source.data["id"], "target_entity": str(target.id), "relation_type": "KNOWS"}, format="json")
        self.assertEqual(relation.status_code, 201, relation.data)
        edited = self.client.patch(f"/api/v1/relation-proposals/{relation.data['id']}/", {
            "time_system": axis.data["id"], "valid_from": 12, "valid_to": 20}, format="json")
        self.assertEqual(edited.status_code, 200, edited.data)
        preview = self.client.post(f"/api/v1/canvases/{self.canvas_id}/preview/", {
            "proposal_ids": [source.data["id"]], "relation_proposal_ids": [relation.data["id"]]}, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        committed = self.client.post(f"/api/v1/canvases/{self.canvas_id}/commit/", {
            "proposal_ids": [source.data["id"]], "relation_proposal_ids": [relation.data["id"]],
            "idempotency_key": "dated-relation", "preview_token": preview.data["preview_token"]}, format="json")
        self.assertEqual(committed.status_code, 200, committed.data)
        confirmed = Relation.objects.get(pk=relation.data["created_relation"] if relation.data.get("created_relation") else committed.data["job"]["relation_ids"][0])
        self.assertEqual(str(confirmed.time_system_id), axis.data["id"])
        self.assertEqual((confirmed.valid_from, confirmed.valid_to), (12, 20))
        branch_query = f"&branch={self.branch_id}"
        before = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.data['id']}&at=11{branch_query}")
        during = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.data['id']}&at=15{branch_query}")
        after = self.client.get(f"/api/v1/graph/slice/?workspace={self.workspace_id}&time_system={axis.data['id']}&at=21{branch_query}")
        self.assertEqual(before.data["edges"], [])
        self.assertEqual(len(during.data["edges"]), 1)
        self.assertEqual(after.data["edges"], [])


class SyncCollaborationTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import WorkspaceMembership
        self.client = APIClient()
        response = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "sync-owner", "password": "sync-owner-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "Sync World"}, format="json")
        self.assertEqual(workspace.status_code, 201, workspace.data)
        self.workspace_id = workspace.data["id"]
        canvas = self.client.post(
            "/api/v1/canvases/",
            {"workspace": self.workspace_id, "name": "Shared canvas", "snapshot": {"store": {}}},
            format="json",
        )
        self.assertEqual(canvas.status_code, 201, canvas.data)
        self.canvas_id = canvas.data["id"]
        self.reader = get_user_model().objects.create_user(
            username="sync-reader", password="sync-reader-password"
        )
        WorkspaceMembership.objects.create(
            workspace_id=self.workspace_id, user=self.reader, role="reader"
        )

    def test_sync_ticket_is_scoped_and_reader_role_is_preserved(self):
        from apps.canvas.services.sync_ticket import verify_sync_ticket

        owner_ticket = self.client.post(
            f"/api/v1/canvases/{self.canvas_id}/sync-ticket/", {"ttl": 300}, format="json"
        )
        self.assertEqual(owner_ticket.status_code, 200, owner_ticket.data)
        claims = verify_sync_ticket(owner_ticket.data["ticket"])
        self.assertEqual(claims["canvas"], self.canvas_id)
        self.assertEqual(claims["workspace"], self.workspace_id)
        self.assertEqual(claims["role"], "owner")
        with self.assertRaises(ValueError):
            verify_sync_ticket(owner_ticket.data["ticket"], now=owner_ticket.data["expires_at"])

        reader_client = APIClient()
        reader_client.force_authenticate(self.reader)
        reader_ticket = reader_client.post(
            f"/api/v1/canvases/{self.canvas_id}/sync-ticket/", {}, format="json"
        )
        self.assertEqual(reader_ticket.status_code, 200, reader_ticket.data)
        self.assertEqual(reader_ticket.data["role"], "reader")
        self.assertEqual(verify_sync_ticket(reader_ticket.data["ticket"])["role"], "reader")

        outsider = APIClient()
        self.assertEqual(
            outsider.post(f"/api/v1/canvases/{self.canvas_id}/sync-ticket/", {}, format="json").status_code,
            403,
        )

    def test_internal_sync_state_uses_secret_compare_and_swap_and_revision_history(self):
        endpoint = f"/api/v1/canvases/{self.canvas_id}/sync-state/"
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        headers = {"HTTP_X_SYNC_INTERNAL_SECRET": "projectoc-sync-local-secret"}
        state = self.client.get(endpoint, **headers)
        self.assertEqual(state.status_code, 200, state.data)
        self.assertEqual(state.data["snapshot_version"], 1)

        saved = self.client.post(
            endpoint,
            {"snapshot": {"store": {"shape:one": {"type": "text"}}}, "expected_version": 1},
            format="json",
            **headers,
        )
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data["snapshot_version"], 2)
        self.assertEqual(self.client.get(f"/api/v1/canvases/{self.canvas_id}/revisions/").data[0]["version"], 1)

        stale = self.client.post(
            endpoint,
            {"snapshot": {"store": {}}, "expected_version": 1},
            format="json",
            **headers,
        )
        self.assertEqual(stale.status_code, 409, stale.data)
        self.assertEqual(stale.data["snapshot_version"], 2)

        archived = self.client.patch(
            f"/api/v1/canvases/{self.canvas_id}/",
            {"status": "archived", "expected_version": 2},
            format="json",
        )
        self.assertEqual(archived.status_code, 200, archived.data)
        rejected = self.client.post(
            endpoint,
            {"snapshot": {"store": {"shape:two": {}}}, "expected_version": 3},
            format="json",
            **headers,
        )
        self.assertEqual(rejected.status_code, 409, rejected.data)

    def test_reader_can_read_ticket_but_cannot_save_canvas_or_issue_state(self):
        reader_client = APIClient()
        reader_client.force_authenticate(self.reader)
        self.assertEqual(
            reader_client.patch(
                f"/api/v1/canvases/{self.canvas_id}/",
                {"snapshot": {"reader": True}, "expected_version": 1},
                format="json",
            ).status_code,
            403,
        )
        # The internal endpoint is never a user-facing write escape hatch.
        self.assertEqual(
            reader_client.post(
                f"/api/v1/canvases/{self.canvas_id}/sync-state/",
                {"snapshot": {}, "expected_version": 1},
                format="json",
                HTTP_X_SYNC_INTERNAL_SECRET="wrong",
            ).status_code,
            403,
        )

class CommitReconciliationTests(TestCase):
    def setUp(self):
        from django.test import override_settings

        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name))
        self.settings_override.enable()
        self.client = APIClient()
        response = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "owner", "password": "strong-recovery-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "Recovery World"}, format="json")
        self.assertEqual(workspace.status_code, 201, workspace.data)
        self.workspace = WorldWorkspace.objects.get(pk=workspace.data["id"])

    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def test_marker_commit_recovers_a_job_left_in_git_syncing(self):
        from django.core.management import call_command
        from apps.version_control.services.git_sync import GitRepositoryService

        job = CommitJob.objects.create(
            workspace=self.workspace,
            idempotency_key="recovery-marker",
            status=CommitJob.Status.DATABASE_COMMITTED,
            request_data={"kind": "test"},
        )
        commit_hash = GitRepositoryService(self.workspace).commit_job(job)
        job.status = CommitJob.Status.GIT_SYNCING
        job.commit_hash = ""
        job.save(update_fields=["status", "commit_hash", "updated_at"])

        call_command("reconcile_commit_jobs", workspace=str(self.workspace.id))
        job.refresh_from_db()
        self.assertEqual(job.status, CommitJob.Status.SYNCED)
        self.assertEqual(job.commit_hash, commit_hash)

    def test_missing_marker_is_requeued_and_retry_is_idempotent(self):
        from django.core.management import call_command

        job = CommitJob.objects.create(
            workspace=self.workspace,
            idempotency_key="recovery-retry",
            status=CommitJob.Status.GIT_SYNCING,
            request_data={"kind": "test"},
        )
        call_command("reconcile_commit_jobs", workspace=str(self.workspace.id))
        job.refresh_from_db()
        self.assertEqual(job.status, CommitJob.Status.DATABASE_COMMITTED)
        self.assertIn("no OC-Job marker", job.error_message)

        call_command("reconcile_commit_jobs", workspace=str(self.workspace.id), retry=True)
        job.refresh_from_db()
        self.assertEqual(job.status, CommitJob.Status.SYNCED)
        self.assertTrue(job.commit_hash)

        # The marker lookup means a second retry does not create a second
        # commit for the same durable job.
        from apps.version_control.services.git_sync import GitRepositoryService
        history_before = GitRepositoryService(self.workspace).history()
        call_command("reconcile_commit_jobs", workspace=str(self.workspace.id), retry=True)
        history_after = GitRepositoryService(self.workspace).history()
        self.assertEqual(history_after, history_before)

    def test_git_status_reports_branch_ref_and_pending_jobs(self):
        branch_response = self.client.post(
            "/api/v1/branches/",
            {"workspace": str(self.workspace.id), "name": "status-branch"},
            format="json",
        )
        self.assertEqual(branch_response.status_code, 201, branch_response.data)
        response = self.client.get(f"/api/v1/workspaces/{self.workspace.id}/git/status/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["main_ref_exists"])
        self.assertEqual(len(response.data["branches"]), 1)
        self.assertTrue(response.data["branches"][0]["ref_exists"])
        self.assertEqual(response.data["pending_jobs"], {})

    def test_missing_branch_ref_is_recreated_from_recorded_baseline(self):
        from django.core.management import call_command
        from apps.version_control.services.git_sync import GitRepositoryService

        branch_response = self.client.post(
            "/api/v1/branches/",
            {"workspace": str(self.workspace.id), "name": "lost-ref"},
            format="json",
        )
        self.assertEqual(branch_response.status_code, 201, branch_response.data)
        branch = self.workspace.branches.get(pk=branch_response.data["id"])
        base_commit = branch.base_commit
        repository = GitRepositoryService(self.workspace)
        repository._run(["branch", "-D", branch.git_ref])
        self.assertFalse(repository._run(["show-ref", "--verify", f"refs/heads/{branch.git_ref}"], check=False))

        CommitJob.objects.create(
            workspace=self.workspace,
            branch=branch,
            idempotency_key="recovery-branch-ref",
            status=CommitJob.Status.DATABASE_COMMITTED,
            request_data={"kind": "test"},
        )
        call_command("reconcile_commit_jobs", workspace=str(self.workspace.id))
        branch.refresh_from_db()
        self.assertEqual(branch.base_commit, base_commit)
        self.assertTrue(repository._run(["show-ref", "--verify", f"refs/heads/{branch.git_ref}"], check=False))
        self.assertEqual(repository._run(["rev-parse", branch.git_ref]), base_commit)

class ProjectionCommitStateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        response = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "owner", "password": "projection-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "Projection World"}, format="json")
        self.assertEqual(workspace.status_code, 201, workspace.data)
        self.workspace = WorldWorkspace.objects.get(pk=workspace.data["id"])

    def test_configured_projection_failure_is_retryable_without_replaying_git(self):
        import os
        from unittest.mock import patch
        from apps.core.models import GraphProjectionJob
        from apps.core.services.neo4j_projection import Neo4jProjection

        commit = CommitJob.objects.create(
            workspace=self.workspace,
            idempotency_key="projection-state",
            status=CommitJob.Status.SYNCED,
            commit_hash="a" * 40,
        )
        projection_job = GraphProjectionJob.objects.create(
            workspace=self.workspace, commit_job=commit,
        )
        with patch.dict(os.environ, {"NEO4J_URI": "bolt://unavailable", "NEO4J_PASSWORD": "secret"}), \
                patch.object(Neo4jProjection, "rebuild", side_effect=RuntimeError("neo4j down")):
            ok, error = Neo4jProjection().process_job(projection_job)
        self.assertFalse(ok)
        self.assertEqual(error, "neo4j down")
        commit.refresh_from_db()
        self.assertEqual(commit.status, CommitJob.Status.PROJECTION_SYNC_FAILED)

        with patch.dict(os.environ, {"NEO4J_URI": "bolt://available", "NEO4J_PASSWORD": "secret"}), \
                patch.object(Neo4jProjection, "rebuild", return_value=None):
            ok, error = Neo4jProjection().process_job(projection_job)
        self.assertTrue(ok)
        self.assertEqual(error, "")
        commit.refresh_from_db()
        projection_job.refresh_from_db()
        self.assertEqual(commit.status, CommitJob.Status.SYNCED)
        self.assertEqual(projection_job.status, GraphProjectionJob.Status.SYNCED)


class CanvasOperationLogTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.secret = "sync-test-secret"
        self.settings_override = override_settings(SYNC_INTERNAL_SECRET=self.secret)
        self.settings_override.enable()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "sync-owner", "password": "sync-password"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "Sync World"}, format="json")
        self.assertEqual(workspace.status_code, 201, workspace.data)
        canvas = self.client.post(
            "/api/v1/canvases/",
            {"workspace": workspace.data["id"], "name": "Sync Canvas", "snapshot": {"document": {"store": {}}}},
            format="json",
        )
        self.assertEqual(canvas.status_code, 201, canvas.data)
        self.canvas_id = canvas.data["id"]

    def tearDown(self):
        self.settings_override.disable()

    def internal(self, method, suffix="", data=None, **kwargs):
        return getattr(self.client, method)(
            f"/api/v1/canvases/{self.canvas_id}/sync-ops/{suffix}",
            data=data,
            HTTP_X_SYNC_INTERNAL_SECRET=self.secret,
            format="json",
            **kwargs,
        )

    @staticmethod
    def put(op_id, clock, text):
        return {
            "kind": "put", "clientId": "client-a", "clock": clock, "opId": op_id,
            "record": {"id": f"shape:{op_id}", "typeName": "shape", "text": text},
        }

    def test_retired_writes_do_not_mutate_historical_state(self):
        self.assertEqual(self.client.get(f"/api/v1/canvases/{self.canvas_id}/sync-ops/").status_code, 403)
        response = self.internal("post", data={"operations": [self.put("one", 1, "old")]})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(CanvasOperation.objects.filter(canvas_id=self.canvas_id).exists())
        rejected = self.client.post(
            f"/api/v1/canvases/{self.canvas_id}/sync-state/",
            {"snapshot": {}, "expected_version": 1, "sync_metadata": {"protocol": "records-v1"}},
            format="json", HTTP_X_SYNC_INTERNAL_SECRET=self.secret,
        )
        self.assertEqual(rejected.status_code, 410)
        self.assertEqual(StagingCanvas.objects.get(pk=self.canvas_id).snapshot_version, 1)

    def test_historical_operations_remain_readable_for_migration(self):
        for index in range(1, 3):
            CanvasOperation.objects.create(canvas_id=self.canvas_id, sequence=index,
                op_id=f"op-{index}", client_id="client-a", clock=index,
                operation=self.put(f"op-{index}", index, str(index)))
        StagingCanvas.objects.filter(pk=self.canvas_id).update(operation_sequence=2)
        page = self.internal("get", suffix="?after=0&limit=1")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data["operations"][0]["sequence"], 1)
        self.assertTrue(page.data["has_more"])
        rest = self.internal("get", suffix="?after=1")
        self.assertEqual(rest.data["operations"][0]["sequence"], 2)
        self.assertFalse(rest.data["has_more"])


class TimeSystemConversionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "owner", "password": "correct-horse-battery", "email": "owner@example.test"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name))
        self.settings_override.enable()
        workspace = self.client.post("/api/v1/workspaces/", {"name": "历法测试", "description": ""}, format="json")
        self.assertEqual(workspace.status_code, 201)
        self.workspace_id = workspace.data["id"]

    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def create_system(self, name):
        response = self.client.post(
            "/api/v1/time-systems/",
            {"workspace": self.workspace_id, "name": name, "unit_name": "tick", "units": []},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def test_conversion_routes_are_validated_composed_and_exported(self):
        source = self.create_system("纪元 A")
        middle = self.create_system("纪元 B")
        target = self.create_system("纪元 C")
        first = self.client.post(
            "/api/v1/time-system-conversions/",
            {
                "workspace": self.workspace_id, "source_system": source,
                "target_system": middle, "numerator": 2,
                "denominator": 1, "offset": 3, "label": "A 到 B",
            },
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post(
            "/api/v1/time-system-conversions/",
            {
                "workspace": self.workspace_id, "source_system": middle,
                "target_system": target, "numerator": 1,
                "denominator": 2, "offset": -4, "label": "B 到 C",
            },
            format="json",
        )
        self.assertEqual(second.status_code, 201, second.data)
        duplicate = self.client.post(
            "/api/v1/time-system-conversions/",
            {"workspace": self.workspace_id, "source_system": source, "target_system": middle},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("target_system", duplicate.data)
        self_same = self.client.post(
            "/api/v1/time-system-conversions/",
            {"workspace": self.workspace_id, "source_system": source, "target_system": source},
            format="json",
        )
        self.assertEqual(self_same.status_code, 400)

        converted = self.client.get(
            f"/api/v1/time-systems/{source}/convert/?target_system={target}&value=10"
        )
        self.assertEqual(converted.status_code, 200, converted.data)
        # (10 * 2 + 3) * 1/2 - 4 = 15/2
        self.assertEqual(converted.data["converted_numerator"], 15)
        self.assertEqual(converted.data["converted_denominator"], 2)
        self.assertFalse(converted.data["exact"])
        self.assertEqual(converted.data["path"], [source, middle, target])

        from apps.core.models import TimeSystemConversion
        from apps.version_control.services.git_sync import GitRepositoryService
        self.assertEqual(TimeSystemConversion.objects.count(), 2)
        snapshot = GitRepositoryService(__import__("apps.core.models", fromlist=["WorldWorkspace"]).WorldWorkspace.objects.get(id=self.workspace_id)).snapshot()
        self.assertIn("indexes/time-system-conversions.json", snapshot)
        self.assertIn(source, snapshot["indexes/time-system-conversions.json"])

    def test_temporal_git_snapshot_and_structured_diff(self):
        from apps.core.models import TimeSystemConversion, WorldWorkspace
        from apps.version_control.services.git_sync import GitRepositoryService

        source = self.create_system("旧历")
        target = self.create_system("新历")
        self.client.post(
            "/api/v1/time-system-conversions/",
            {"workspace": self.workspace_id, "source_system": source, "target_system": target, "numerator": 1, "denominator": 1, "offset": 12},
            format="json",
        )
        service = GitRepositoryService(WorldWorkspace.objects.get(id=self.workspace_id))
        service.write_snapshot(service.snapshot())
        service._run(["add", "."])
        first = service._commit_if_changed("temporal baseline")
        conversion = TimeSystemConversion.objects.get(workspace_id=self.workspace_id)
        conversion.label = "新历换算规则"
        conversion.save(update_fields=["label", "updated_at"])
        service.write_snapshot(service.snapshot())
        service._run(["add", "."])
        second = service._commit_if_changed("temporal label")

        snapshot = service.temporal_snapshot(first)
        self.assertEqual(snapshot["commit"], first)
        self.assertIn("entities", snapshot)
        self.assertIn("relations", snapshot)
        self.assertEqual(len(snapshot["conversions"]), 1)
        temporal = service.temporal_diff(first, second)
        self.assertEqual(temporal["from"], first)
        self.assertEqual(temporal["to"], second)
        self.assertEqual(len(temporal["changes"]["conversions"]["changed"]), 1)
        api_diff = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/git/temporal/?from={first}&to={second}")
        self.assertEqual(api_diff.status_code, 200, api_diff.data)
        self.assertEqual(len(api_diff.data["diff"]["changes"]["conversions"]["changed"]), 1)
        api_snapshot = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/git/temporal/?snapshot=1&commit={second}")
        self.assertEqual(api_snapshot.status_code, 200, api_snapshot.data)
        self.assertEqual(api_snapshot.data["snapshot"]["commit"], second)

class CalendarRulesTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post("/api/v1/auth/bootstrap/", {"username": "calendar-owner", "password": "correct-horse-battery", "email": "calendar-owner@example.test"}, format="json")
        self.assertEqual(owner.status_code, 201, owner.data)
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name), OPENAI_API_KEY="", OPENAI_MODEL="local-test-model")
        self.settings_override.enable()
        workspace_response = self.client.post("/api/v1/workspaces/", {"name": "日历世界", "description": "测试"}, format="json")
        self.assertEqual(workspace_response.status_code, 201, workspace_response.data)
        self.workspace_id = workspace_response.data["id"]

    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def test_variable_calendar_rules_are_normalized_and_exported(self):
        from apps.core.calendar import format_calendar_value
        from apps.core.models import TimeSystem, WorldWorkspace
        from apps.version_control.services.git_sync import GitRepositoryService

        response = self.client.post(
            "/api/v1/time-systems/",
            {
                "workspace": self.workspace_id,
                "name": "王历",
                "unit_name": "日",
                "units": [],
                "calendar_rules": {
                    "mode": "variable_months",
                    "months": [{"name": "霜月", "days": 28}, {"name": "融月", "days": 35}],
                    "leap_rule": {"cycle": 5, "extra_days": 2},
                    "year_zero": True,
                    "era": "王国纪年",
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["calendar_rules"]["months"][0]["days"], 28)
        self.assertEqual(format_calendar_value(0, response.data["calendar_rules"], "日"), "王国纪年 0年 霜月1日")
        self.assertEqual(format_calendar_value(63, response.data["calendar_rules"], "日"), "王国纪年 1年 霜月1日")
        self.assertEqual(format_calendar_value(315, response.data["calendar_rules"], "日"), "王国纪年 5年 霜月1日")

        duplicate_month = self.client.post(
            "/api/v1/time-systems/",
            {
                "workspace": self.workspace_id,
                "name": "坏历",
                "unit_name": "日",
                "calendar_rules": {"mode": "variable_months", "months": [{"name": "月", "days": 1}, {"name": "月", "days": 2}]},
            },
            format="json",
        )
        self.assertEqual(duplicate_month.status_code, 400)
        self.assertIn("calendar_rules", duplicate_month.data)
        snapshot = GitRepositoryService(WorldWorkspace.objects.get(id=self.workspace_id)).snapshot()
        self.assertIn('"calendar_rules"', snapshot["indexes/time-systems.json"])

    def test_calendar_rules_reject_invalid_leap_rule(self):
        response = self.client.post(
            "/api/v1/time-systems/",
            {
                "workspace": self.workspace_id,
                "name": "坏闰历",
                "unit_name": "日",
                "calendar_rules": {"mode": "variable_months", "months": [{"name": "月", "days": 30}], "leap_rule": {"cycle": 0}},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("calendar_rules", response.data)


class CanvasOperationCompactionCommandTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.secret = "compaction-command-secret"
        self.settings_override = override_settings(SYNC_INTERNAL_SECRET=self.secret)
        self.settings_override.enable()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "compact-owner", "password": "compact-password"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "Compaction World"}, format="json")
        self.assertEqual(workspace.status_code, 201, workspace.data)
        canvas = self.client.post(
            "/api/v1/canvases/",
            {"workspace": workspace.data["id"], "name": "Compaction Canvas", "snapshot": {"document": {"store": {}}}},
            format="json",
        )
        self.assertEqual(canvas.status_code, 201, canvas.data)
        self.canvas_id = canvas.data["id"]

    def tearDown(self):
        self.settings_override.disable()

    def _append(self, count):
        operations = [
            {
                "kind": "put",
                "clientId": "command-client",
                "clock": index,
                "opId": f"command-op-{index}",
                "record": {"id": f"shape:command-{index}", "typeName": "shape", "text": str(index)},
            }
            for index in range(1, count + 1)
        ]
        for index, operation in enumerate(operations, 1):
            CanvasOperation.objects.create(canvas_id=self.canvas_id, sequence=index,
                op_id=operation["opId"], client_id=operation["clientId"], clock=operation["clock"], operation=operation)
        StagingCanvas.objects.filter(pk=self.canvas_id).update(operation_sequence=count)

    def test_management_command_compacts_only_checkpointed_operations(self):
        self._append(6)
        canvas = StagingCanvas.objects.get(pk=self.canvas_id)
        canvas.snapshot_operation_cursor = 5
        canvas.save(update_fields=["snapshot_operation_cursor"])

        output = tempfile.SpooledTemporaryFile(mode="w+")
        call_command("compact_canvas_operations", "--canvas", self.canvas_id, "--keep-last", "2", stdout=output)
        output.seek(0)
        self.assertIn("deleted=3", output.read())
        self.assertEqual(list(CanvasOperation.objects.filter(canvas=canvas).values_list("sequence", flat=True)), [4, 5, 6])
        canvas.refresh_from_db()
        self.assertEqual(canvas.operation_compacted_through, 3)

    def test_management_command_dry_run_does_not_mutate(self):
        self._append(4)
        canvas = StagingCanvas.objects.get(pk=self.canvas_id)
        canvas.snapshot_operation_cursor = 4
        canvas.save(update_fields=["snapshot_operation_cursor"])
        call_command("compact_canvas_operations", "--canvas", self.canvas_id, "--keep-last", "1", "--dry-run")
        self.assertEqual(CanvasOperation.objects.filter(canvas=canvas).count(), 4)
        canvas.refresh_from_db()
        self.assertEqual(canvas.operation_compacted_through, 0)

class ConsistencyReportTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        response = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "consistency-owner", "password": "correct-horse-battery", "email": "consistency@example.test"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "一致性测试世界"}, format="json")
        self.assertEqual(workspace.status_code, 201)
        self.workspace_id = workspace.data["id"]

    def test_report_detects_issues_and_returns_stable_summary(self):
        from apps.core.models import Relation

        duplicate_one = Entity.objects.create(
            workspace_id=self.workspace_id, type=Entity.EntityType.ITEM, title="重复标题", content="内容一"
        )
        duplicate_two = Entity.objects.create(
            workspace_id=self.workspace_id, type=Entity.EntityType.ITEM, title=" 重复标题 ", content=""
        )
        floating = Entity.objects.create(
            workspace_id=self.workspace_id, type=Entity.EntityType.FLOATING_TIP, title="游离灵感", content="一条 tip"
        )
        character = Entity.objects.create(
            workspace_id=self.workspace_id, type=Entity.EntityType.CHARACTER, title="无生命周期人物", content="人物"
        )
        report = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/consistency/")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.data["branch"], "main")
        self.assertEqual(report.data["summary"]["error"], 2)  # duplicate title is reported per entity
        codes = {issue["code"] for issue in report.data["issues"]}
        self.assertIn("duplicate_title", codes)
        self.assertIn("empty_content", codes)
        self.assertIn("unlinked_floating_tip", codes)
        self.assertIn("character_without_lifespan", codes)
        self.assertLess(report.data["score"], 100)

        # A valid link removes the orphan condition for the linked entities,
        # while the report remains read-only and deterministic.
        Relation.objects.create(
            workspace_id=self.workspace_id,
            source=duplicate_one,
            target=floating,
            relation_type=Relation.RelationType.LINKED_TO,
        )
        refreshed = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/consistency/")
        self.assertEqual(refreshed.status_code, 200)
        refreshed_codes = {issue["code"] for issue in refreshed.data["issues"]}
        self.assertNotIn("unlinked_floating_tip", refreshed_codes)
        orphan_ids = {issue.get("entity_id") for issue in refreshed.data["issues"] if issue["code"] == "orphan_entity"}
        self.assertNotIn(str(duplicate_one.id), orphan_ids)
        self.assertNotIn(str(floating.id), orphan_ids)
        self.assertEqual(self.client.get(f"/api/v1/entities/{character.id}/").status_code, 200)

    def test_report_uses_branch_effective_view(self):
        base = Entity.objects.create(
            workspace_id=self.workspace_id, type=Entity.EntityType.ITEM, title="主分支物品", content="主分支"
        )
        branch_response = self.client.post(
            "/api/v1/branches/", {"workspace": self.workspace_id, "name": "consistency-review"}, format="json"
        )
        self.assertEqual(branch_response.status_code, 201)
        branch_id = branch_response.data["id"]
        branch_entity = Entity.objects.create(
            workspace_id=self.workspace_id,
            branch_id=branch_id,
            type=Entity.EntityType.FLOATING_TIP,
            title="分支游离设定",
            content="只在分支存在",
        )
        main = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/consistency/")
        branch = self.client.get(f"/api/v1/workspaces/{self.workspace_id}/consistency/?branch={branch_id}")
        self.assertEqual(main.status_code, 200)
        self.assertEqual(branch.status_code, 200)
        self.assertNotIn(str(branch_entity.id), {issue.get("entity_id") for issue in main.data["issues"]})
        self.assertIn(str(branch_entity.id), {issue.get("entity_id") for issue in branch.data["issues"]})
        self.assertEqual(branch.data["branch"], branch_id)


class DialogueBranchContextTests(TestCase):
    """The Agent must reason over the canvas branch, not workspace-wide rows."""

    def setUp(self):
        self.client = APIClient()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "owner", "password": "branch-context-password"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201, owner.data)
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name))
        self.settings_override.enable()
        workspace = self.client.post(
            "/api/v1/workspaces/", {"name": "对话分支上下文世界"}, format="json"
        )
        self.assertEqual(workspace.status_code, 201, workspace.data)
        self.workspace_id = workspace.data["id"]

        # This row exists when the canvas branch is created and is materialized
        # into the branch as a copy-on-write baseline.
        self.base = Entity.objects.create(
            workspace_id=self.workspace_id,
            type=Entity.EntityType.CANONICAL_SETTING,
            title="主分支基线标题",
            content="基线内容",
        )
        canvas = self.client.post(
            "/api/v1/canvases/",
            {"workspace": self.workspace_id, "name": "分支对话画布"},
            format="json",
        )
        self.assertEqual(canvas.status_code, 201, canvas.data)
        self.canvas_id = canvas.data["id"]
        self.canvas = StagingCanvas.objects.select_related("branch").get(pk=self.canvas_id)
        self.branch = self.canvas.branch
        self.assertIsNotNone(self.branch)

        # The branch overrides the baseline and adds a branch-only entity.
        self.branch_base = Entity.objects.get(branch=self.branch, base_entity=self.base)
        self.branch_base.title = "分支覆盖标题"
        self.branch_base.content = "分支内容"
        self.branch_base.save(update_fields=["title", "content", "updated_at"])
        self.branch_entity = Entity.objects.create(
            workspace_id=self.workspace_id,
            branch=self.branch,
            type=Entity.EntityType.FLOATING_TIP,
            title="当前分支游离设定",
            content="只在当前画布分支中存在",
        )
        Relation.objects.create(
            workspace_id=self.workspace_id,
            branch=self.branch,
            source=self.branch_entity,
            target=self.branch_base,
            relation_type=Relation.RelationType.LINKED_TO,
        )

        # This main row is created after the branch baseline and must not leak
        # into the active branch's Agent context.
        self.main_after_branch = Entity.objects.create(
            workspace_id=self.workspace_id,
            type=Entity.EntityType.ITEM,
            title="分支创建后才出现的主分支实体",
            content="不可见于当前分支",
        )

        other_branch = WorldBranch.objects.create(
            workspace_id=self.workspace_id, name="另一个工作分支"
        )
        self.other_branch_entity = Entity.objects.create(
            workspace_id=self.workspace_id,
            branch=other_branch,
            type=Entity.EntityType.ITEM,
            title="另一个分支的实体",
            content="不可见于当前画布",
        )
        session = self.client.post(
            "/api/v1/dialogue/sessions/",
            {"workspace": self.workspace_id, "canvas": self.canvas_id},
            format="json",
        )
        self.assertEqual(session.status_code, 201, session.data)
        self.session_id = session.data["id"]

    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def test_context_uses_effective_branch_entities_and_relations(self):
        import json
        from apps.ai_agent.services.dialogue import DialogueOrchestrator
        from apps.canvas.models import DialogueSession

        session = DialogueSession.objects.select_related("workspace", "canvas__branch").get(
            pk=self.session_id
        )
        messages = DialogueOrchestrator(session, provider=object()).context()
        self.assertEqual(len(messages), 2)
        payload = json.loads(messages[1]["content"].split("\n", 1)[1])

        self.assertEqual(payload["branch"]["id"], str(self.branch.id))
        self.assertEqual(payload["branch"]["name"], self.branch.name)
        titles = {item["title"] for item in payload["entities"]}
        self.assertIn("分支覆盖标题", titles)
        self.assertIn("当前分支游离设定", titles)
        self.assertNotIn("主分支基线标题", titles)
        self.assertNotIn("分支创建后才出现的主分支实体", titles)
        self.assertNotIn("另一个分支的实体", titles)

        self.assertEqual(len(payload["relations"]), 1)
        relation = payload["relations"][0]
        self.assertEqual(relation["source_title"], "当前分支游离设定")
        self.assertEqual(relation["target_title"], "分支覆盖标题")
        self.assertEqual(relation["relation_type"], Relation.RelationType.LINKED_TO)


class DialogueMemoryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "memory-owner", "password": "memory-password", "email": "memory@example.test"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        workspace = self.client.post(
            "/api/v1/workspaces/", {"name": "记忆世界", "description": "memory"}, format="json"
        )
        self.assertEqual(workspace.status_code, 201)
        self.workspace_id = workspace.data["id"]
        canvas = self.client.post(
            "/api/v1/canvases/",
            {"workspace": self.workspace_id, "name": "记忆画布", "snapshot": {"store": {}}},
            format="json",
        )
        self.assertEqual(canvas.status_code, 201)
        self.canvas_id = canvas.data["id"]

    def test_memory_separates_pending_notes_and_confirmed_facts_across_sessions(self):
        from apps.ai_agent.services.dialogue import DialogueOrchestrator
        from apps.canvas.models import DialogueSession

        created = self.client.post(
            "/api/v1/dialogue/sessions/",
            {"workspace": self.workspace_id, "canvas": self.canvas_id, "model": "fallback-model"},
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        first_session_id = created.data["id"]
        response = self.client.post(
            f"/api/v1/dialogue/sessions/{first_session_id}/messages/",
            {"content": "创建一个魔法体系：星辰魔法，来自星辰辐射。"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)

        memory = DialogueMemory.objects.get(scope_key__startswith=f"workspace:{self.workspace_id}:")
        self.assertEqual(memory.confirmed_facts, [])
        self.assertTrue(any(note["title"].startswith("创建一个魔法体系") for note in memory.working_notes))
        self.assertEqual(memory.revision > 0, True)
        memory_response = self.client.get(f"/api/v1/dialogue/sessions/{first_session_id}/memory/")
        self.assertEqual(memory_response.status_code, 200)
        self.assertEqual(memory_response.data["confirmed_facts"], [])
        self.assertTrue(memory_response.data["working_notes"])

        proposal = EntityProposal.objects.get(canvas_id=self.canvas_id)
        preview = self.client.post(
            f"/api/v1/canvases/{self.canvas_id}/preview/",
            {"proposal_ids": [str(proposal.id)], "relation_proposal_ids": []},
            format="json",
        )
        self.assertEqual(preview.status_code, 200)
        committed = self.client.post(
            f"/api/v1/canvases/{self.canvas_id}/commit/",
            {
                "proposal_ids": [str(proposal.id)],
                "relation_proposal_ids": [],
                "idempotency_key": "memory-commit",
                "preview_token": preview.data["preview_token"],
            },
            format="json",
        )
        self.assertEqual(committed.status_code, 200)

        second = DialogueSession.objects.create(
            workspace_id=self.workspace_id,
            canvas_id=self.canvas_id,
            model="fallback-model",
        )
        context = DialogueOrchestrator(second, provider=object()).context()
        memory_payload = __import__("json").loads(context[1]["content"].split("\n", 1)[1])["memory"]
        self.assertTrue(any(fact.get("title", "").startswith("创建一个魔法体系") for fact in memory_payload["confirmed_facts"]))
        self.assertEqual(memory_payload["working_notes"], [])
        self.assertEqual(memory_payload["scope_key"], f"workspace:{self.workspace_id}:branch:{StagingCanvas.objects.get(id=self.canvas_id).branch_id}")

class DialogueMemoryEditingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "memory-editor", "password": "memory-password", "email": "memory-editor@example.test"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "记忆审计世界"}, format="json")
        self.assertEqual(workspace.status_code, 201)
        self.workspace_id = workspace.data["id"]
        canvas = self.client.post(
            "/api/v1/canvases/",
            {"workspace": self.workspace_id, "name": "审计画布", "snapshot": {"store": {}}},
            format="json",
        )
        self.canvas_id = canvas.data["id"]
        session = self.client.post(
            "/api/v1/dialogue/sessions/",
            {"workspace": self.workspace_id, "canvas": self.canvas_id, "model": "fallback-model"},
            format="json",
        )
        self.session_id = session.data["id"]

    def test_manual_notes_questions_and_audit_are_version_checked(self):
        initial = self.client.get(f"/api/v1/dialogue/sessions/{self.session_id}/memory/")
        self.assertEqual(initial.status_code, 200)
        revision = initial.data["revision"]
        updated = self.client.patch(
            f"/api/v1/dialogue/sessions/{self.session_id}/memory/",
            {"expected_revision": revision, "manual_notes": [{"text": "保留星辰魔法的代价设定"}]},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["manual_notes"][0]["text"], "保留星辰魔法的代价设定")
        stale = self.client.patch(
            f"/api/v1/dialogue/sessions/{self.session_id}/memory/",
            {"expected_revision": revision, "manual_notes": [{"text": "旧内容"}]},
            format="json",
        )
        self.assertEqual(stale.status_code, 409)

        session_patch = self.client.patch(
            f"/api/v1/dialogue/sessions/{self.session_id}/",
            {"context": {"question": {"text": "星辰魔法的代价是什么？", "reason": "基础逻辑", "status": "pending"}}},
            format="json",
        )
        self.assertEqual(session_patch.status_code, 200)
        with_question = self.client.get(f"/api/v1/dialogue/sessions/{self.session_id}/memory/")
        self.assertTrue(any(q["text"] == "星辰魔法的代价是什么？" for q in with_question.data["open_questions"]))
        archived = self.client.patch(
            f"/api/v1/dialogue/sessions/{self.session_id}/memory/",
            {"expected_revision": with_question.data["revision"], "archive_question": {"text": "星辰魔法的代价是什么？"}},
            format="json",
        )
        self.assertEqual(archived.status_code, 200)
        self.assertFalse(any(q["text"] == "星辰魔法的代价是什么？" for q in archived.data["open_questions"]))
        self.assertEqual(archived.data["archived_questions"][0]["text"], "星辰魔法的代价是什么？")

        audited = self.client.get(f"/api/v1/dialogue/sessions/{self.session_id}/memory/?audit=1")
        self.assertEqual(audited.status_code, 200)
        self.assertGreaterEqual(len(audited.data["audit"]), 2)
        self.assertEqual(DialogueMemoryAudit.objects.count(), 2)
