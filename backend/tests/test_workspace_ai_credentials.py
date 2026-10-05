import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import WorkspaceMembership
from apps.ai_agent.providers import configured_provider
from apps.core.models import WorkspaceAISecret, WorldWorkspace
from apps.core.services.workspace_credentials import decrypt_secret


class WorkspaceAICredentialTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.override = override_settings(
            WORLD_REPOS_ROOT=Path(self.tempdir.name),
            OPENAI_API_KEY="env-fallback-key",
            OPENAI_BASE_URL="https://env.example/v1",
            OPENAI_MODEL="env-model",
            WORKSPACE_CREDENTIALS_KEY="v1JX2dYQ5F4TXP4q0Xg8QpVY0G8uXq6ZQ3m0f3vKx2Q=",
        )
        self.override.enable()
        self.client = APIClient()
        response = self.client.post("/api/v1/auth/bootstrap/", {
            "username": "owner", "password": "correct-horse-battery", "email": "owner@example.test"
        }, format="json")
        self.assertEqual(response.status_code, 201)
        response = self.client.post("/api/v1/workspaces/", {"name": "Credential World"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.workspace_id = response.data["id"]
        self.workspace = WorldWorkspace.objects.get(id=self.workspace_id)

    def tearDown(self):
        self.override.disable()
        self.tempdir.cleanup()

    def test_owner_can_set_key_but_api_and_database_do_not_expose_plaintext(self):
        secret = "sk-workspace-super-secret"
        response = self.client.patch(
            f"/api/v1/workspaces/{self.workspace_id}/",
            {"ai_api_key": secret, "settings": {"ai": {"provider": "openai-compatible", "model": "workspace-model", "base_url": "https://workspace.example/v1"}}},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ai_api_key_configured"])
        self.assertEqual(response.data["ai_api_key_last4"], "cret")
        self.assertNotIn(secret, response.content.decode())
        stored = WorkspaceAISecret.objects.get(workspace=self.workspace)
        self.assertNotEqual(stored.api_key_encrypted, secret)
        self.assertEqual(decrypt_secret(stored.api_key_encrypted), secret)
        self.workspace.refresh_from_db()
        provider = configured_provider(self.workspace)
        self.assertEqual(provider.api_key, secret)
        self.assertEqual(provider.credential_source, "workspace")
        self.assertEqual(provider.base_url, "https://workspace.example/v1")
        self.assertEqual(provider.default_model, "workspace-model")

    def test_empty_key_clears_and_missing_key_uses_environment_fallback(self):
        response = self.client.patch(f"/api/v1/workspaces/{self.workspace_id}/", {"ai_api_key": "sk-temporary"}, format="json")
        self.assertEqual(response.status_code, 200)
        response = self.client.patch(f"/api/v1/workspaces/{self.workspace_id}/", {"ai_api_key": ""}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ai_api_key_configured"])
        self.assertEqual(response.data["ai_api_key_last4"], "")
        self.assertEqual(configured_provider(self.workspace).api_key, "env-fallback-key")
        self.assertEqual(configured_provider(self.workspace).credential_source, "environment")

    def test_reader_cannot_change_key(self):
        reader = get_user_model().objects.create_user(username="reader", password="reader-pass")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=reader, role="reader")
        self.client.force_authenticate(reader)
        response = self.client.patch(f"/api/v1/workspaces/{self.workspace_id}/", {"ai_api_key": "sk-nope"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(WorkspaceAISecret.objects.filter(workspace=self.workspace).exists())
