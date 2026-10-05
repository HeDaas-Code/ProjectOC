from django.test import TestCase
from rest_framework.test import APIClient

from apps.ai_agent.services.memory import refresh_memory
from apps.canvas.models import DialogueSession


class DialogueQuestionMemoryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "memory-owner", "password": "memory-password", "email": "memory@example.test"},
            format="json",
        )
        self.assertEqual(owner.status_code, 201)
        workspace = self.client.post("/api/v1/workspaces/", {"name": "问题记忆测试世界"}, format="json")
        self.assertEqual(workspace.status_code, 201)
        canvas = self.client.post(
            "/api/v1/canvases/", {"workspace": workspace.data["id"], "name": "问题测试画布"}, format="json"
        )
        self.assertEqual(canvas.status_code, 201)
        session = self.client.post(
            "/api/v1/dialogue/sessions/",
            {"workspace": workspace.data["id"], "canvas": canvas.data["id"], "model": "local-test"},
            format="json",
        )
        self.assertEqual(session.status_code, 201)
        self.session_id = session.data["id"]

    def _set_question(self, status):
        session = DialogueSession.objects.get(id=self.session_id)
        session.context = {
            **session.context,
            "question": {
                "text": "这个世界的代价是什么？",
                "reason": "帮助明确规则边界",
                "priority": "logic",
                "status": status,
            },
        }
        session.save(update_fields=["context", "updated_at"])
        return session

    def test_question_lifecycle_filters_closed_questions(self):
        session = self._set_question("pending")
        memory = refresh_memory(session)
        self.assertEqual(memory.open_questions[0]["status"], "pending")

        session = self._set_question("skipped")
        memory = refresh_memory(session)
        self.assertEqual(memory.open_questions, [])

        session = self._set_question("deferred")
        memory = refresh_memory(session)
        self.assertEqual(memory.open_questions[0]["status"], "deferred")

    def test_memory_archive_and_restore_are_versioned(self):
        session = self._set_question("deferred")
        memory = refresh_memory(session)
        response = self.client.patch(
            f"/api/v1/dialogue/sessions/{self.session_id}/memory/",
            {
                "expected_revision": memory.revision,
                "archive_question": {"text": "这个世界的代价是什么？", "reason": "稍后再处理"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["open_questions"], [])
        self.assertEqual(response.data["archived_questions"][0]["text"], "这个世界的代价是什么？")

        restored = self.client.patch(
            f"/api/v1/dialogue/sessions/{self.session_id}/memory/",
            {
                "expected_revision": response.data["revision"],
                "restore_question": {"text": "这个世界的代价是什么？"},
            },
            format="json",
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.data["archived_questions"], [])
        self.assertEqual(restored.data["open_questions"][0]["status"], "deferred")
