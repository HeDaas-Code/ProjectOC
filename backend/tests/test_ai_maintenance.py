import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.canvas.models import EntityProposal
from apps.core.models import Entity


class MaintenanceReviewApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "maintenance-owner", "password": "correct-horse-battery", "email": "maintenance@example.test"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            WORLD_REPOS_ROOT=Path(self.tempdir.name), OPENAI_API_KEY="", OPENAI_MODEL="maintenance-test"
        )
        self.settings_override.enable()
        workspace = self.client.post(
            "/api/v1/workspaces/", {"name": "维护测试世界", "description": ""}, format="json"
        )
        self.assertEqual(workspace.status_code, 201)
        self.workspace_id = workspace.data["id"]
        self.entity = Entity.objects.create(
            workspace_id=self.workspace_id,
            type=Entity.EntityType.FLOATING_TIP,
            title="尚未链接的游离设定",
            content="一个需要后续归属的物品。",
        )
        canvas = self.client.post(
            "/api/v1/canvases/", {"workspace": self.workspace_id, "name": "维护提案画布"}, format="json"
        )
        self.assertEqual(canvas.status_code, 201, canvas.data)
        self.canvas_id = canvas.data["id"]

    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def test_without_provider_returns_deterministic_non_mutating_advice(self):
        before = Entity.objects.values_list("title", "content", "status").get(pk=self.entity.id)
        response = self.client.post(f"/api/v1/workspaces/{self.workspace_id}/maintenance-review/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mode"], "deterministic_fallback")
        self.assertEqual(response.data["report"]["branch"], "main")
        self.assertTrue(response.data["suggestions"])
        self.assertIn("question", response.data)
        after = Entity.objects.values_list("title", "content", "status").get(pk=self.entity.id)
        self.assertEqual(before, after)

    def test_upstream_review_must_reference_report_issues_and_never_writes(self):
        provider = Mock()
        provider.api_key = "test-key"
        provider.extract_structured_data.return_value = {
            "suggestions": [{
                "issue_code": "unlinked_floating_tip",
                "action": "ask",
                "title": "确认游离设定的归属",
                "reason": "它目前没有正式链接。",
                "target_ids": [str(self.entity.id)],
            }],
            "question": {
                "text": "这个物品应当归属于哪个设定系？",
                "reason": "游离设定需要一个可追踪的归属。",
                "priority": "connection",
            },
        }
        before_count = Entity.objects.count()
        with patch("apps.ai_agent.views.configured_provider", return_value=provider):
            response = self.client.post(
                f"/api/v1/workspaces/{self.workspace_id}/maintenance-review/",
                {"model": "maintenance-test"}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mode"], "upstream")
        self.assertEqual(response.data["suggestions"][0]["issue_code"], "unlinked_floating_tip")
        self.assertEqual(Entity.objects.count(), before_count)
        provider.extract_structured_data.assert_called_once()

    def test_upstream_review_rejects_hallucinated_issue_reference(self):
        provider = Mock()
        provider.api_key = "test-key"
        provider.extract_structured_data.return_value = {
            "suggestions": [{
                "issue_code": "made_up_issue",
                "action": "inspect",
                "title": "不应被接受",
                "reason": "不存在于确定性报告。",
                "target_ids": [],
            }],
            "question": {"text": "跳过？", "reason": "测试", "priority": "expansion"},
        }
        with patch("apps.ai_agent.views.configured_provider", return_value=provider):
            response = self.client.post(
                f"/api/v1/workspaces/{self.workspace_id}/maintenance-review/", {}, format="json"
            )
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["status"], "unavailable")
        self.assertIn("report", response.data)


    def test_user_confirmed_draft_proposal_is_pending_and_idempotent(self):
        response = self.client.post(
            f"/api/v1/workspaces/{self.workspace_id}/maintenance-proposals/",
            {
                "canvas": self.canvas_id,
                "branch": "main",
                "action": "draft_proposal",
                "issue_code": "unlinked_floating_tip",
                "target_ids": [str(self.entity.id)],
                "entity_type": "canonical_setting",
                "title": "物品的归属设定",
                "content": "用户确认后补充的候选归属设定。",
                "reason": "先把游离物品纳入待审核设定。",
                "idempotency_key": "maintenance-proposal-1",
            }, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        proposal = EntityProposal.objects.get(pk=response.data["id"])
        self.assertEqual(proposal.status, EntityProposal.Status.PENDING)
        self.assertEqual(proposal.source, EntityProposal.Source.AI)
        self.assertEqual(proposal.metadata["maintenance"]["issue_code"], "unlinked_floating_tip")
        self.assertEqual(Entity.objects.filter(title="物品的归属设定").count(), 0)

        repeated = self.client.post(
            f"/api/v1/workspaces/{self.workspace_id}/maintenance-proposals/",
            {
                "canvas": self.canvas_id, "branch": "main", "action": "draft_proposal",
                "issue_code": "unlinked_floating_tip", "target_ids": [str(self.entity.id)],
                "entity_type": "canonical_setting", "title": "不同标题也不能重复创建",
                "content": "重复请求不应创建第二个提案。", "idempotency_key": "maintenance-proposal-1",
            }, format="json",
        )
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertTrue(repeated.data["idempotent"])
        self.assertEqual(EntityProposal.objects.filter(canvas_id=self.canvas_id).count(), 1)

    def test_stale_issue_and_reader_cannot_create_maintenance_proposal(self):
        stale = self.client.post(
            f"/api/v1/workspaces/{self.workspace_id}/maintenance-proposals/",
            {"canvas": self.canvas_id, "action": "draft_proposal", "issue_code": "not-current"},
            format="json",
        )
        self.assertEqual(stale.status_code, 409, stale.data)

        invite = self.client.post(
            "/api/v1/members/",
            {"workspace": self.workspace_id, "email": "maintenance-reader@example.test", "role": "reader"}, format="json",
        )
        self.assertEqual(invite.status_code, 201, invite.data)
        reader_client = APIClient()
        accepted = reader_client.post(
            "/api/v1/invites/signup/",
            {"token": invite.data["token"], "username": "maintenance-reader", "email": "maintenance-reader@example.test", "password": "reader-password-123"},
            format="json",
        )
        self.assertEqual(accepted.status_code, 201, accepted.data)
        denied = reader_client.post(
            f"/api/v1/workspaces/{self.workspace_id}/maintenance-proposals/",
            {"canvas": self.canvas_id, "action": "draft_proposal", "issue_code": "unlinked_floating_tip", "title": "不应创建", "entity_type": "canonical_setting"},
            format="json",
        )
        self.assertEqual(denied.status_code, 403, denied.data)
