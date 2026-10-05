from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.ai_agent.models import AgentRun
from apps.ai_agent.providers import OpenAICompatibleProvider, ProviderError
from apps.canvas.models import DialogueSession


class AgentAnalysisContractTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "agent-owner", "password": "agent-password", "email": "agent@example.test"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "Agent世界"}, format="json")
        self.assertEqual(workspace.status_code, 201)
        self.workspace_id = workspace.data["id"]
        canvas = self.client.post("/api/v1/canvases/", {"workspace": self.workspace_id, "name": "分析画布"}, format="json")
        self.assertEqual(canvas.status_code, 201)
        self.session = self.client.post(
            "/api/v1/dialogue/sessions/",
            {"workspace": self.workspace_id, "canvas": canvas.data["id"], "model": "local-test"},
            format="json",
        )
        self.assertEqual(self.session.status_code, 201)

    @override_settings(AGENT_LLM_ENABLED=False)
    def test_analysis_is_deterministic_and_auditable_without_provider(self):
        response = self.client.post(
            f"/api/v1/dialogue/sessions/{self.session.data['id']}/analysis/",
            {"mode": "consistency", "token_budget": 800},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        run = AgentRun.objects.get(pk=response.data["run_id"])
        self.assertEqual(run.status, AgentRun.Status.COMPLETED)
        self.assertIn("findings", response.data["result"])
        self.assertTrue(run.tool_calls.exists())
        evidence = self.client.get(f"/api/v1/agent/runs/{run.id}/evidence/")
        self.assertEqual(evidence.status_code, 200)

    def test_cancelled_run_is_not_marked_completed(self):
        from apps.ai_agent.services.analysis import AgentCancelledError, _raise_if_cancelled

        run = AgentRun.objects.create(
            workspace_id=self.workspace_id,
            mode=AgentRun.Mode.CONSISTENCY,
            status=AgentRun.Status.CANCELLED,
        )
        with self.assertRaises(AgentCancelledError):
            _raise_if_cancelled(run)


class ProviderFailureClassificationTests(TestCase):
    @override_settings(OPENAI_API_KEY="key", OPENAI_TIMEOUT=0.01)
    def test_provider_exposes_stable_timeout_code(self):
        provider = OpenAICompatibleProvider(base_url="http://127.0.0.1:1", api_key="key")
        with self.assertRaises(ProviderError) as raised:
            provider.list_models()
        self.assertIn(raised.exception.code, {"provider_unavailable", "provider_timeout"})

    def test_provider_usage_is_available_after_response(self):
        provider = OpenAICompatibleProvider(api_key="key")
        provider._request = Mock(return_value={
            "choices": [{"message": {"content": '{"summary":"ok","questions":[],"finding_notes":[]}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })
        result = provider.extract_structured_data(
            [{"role": "user", "content": "test"}],
            {"type": "object", "required": ["summary", "questions", "finding_notes"], "properties": {
                "summary": {"type": "string"}, "questions": {"type": "array"}, "finding_notes": {"type": "array"},
            }},
            model="local-test",
        )
        self.assertEqual(result["summary"], "ok")
        self.assertEqual(provider.last_usage["total_tokens"], 15)
