import json
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import WorldWorkspace
from apps.version_control.services.git_sync import GitRepositoryService


class TimelineDiffServiceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        response = self.client.post(
            "/api/v1/auth/bootstrap/",
            {"username": "timeline-diff-owner", "password": "correct-horse-battery", "email": "timeline-diff@example.test"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(WORLD_REPOS_ROOT=Path(self.tempdir.name))
        self.settings_override.enable()
        response = self.client.post("/api/v1/workspaces/", {"name": "Diff 测试世界"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.workspace = WorldWorkspace.objects.get(id=response.data["id"])

    def tearDown(self):
        self.settings_override.disable()
        self.tempdir.cleanup()

    def _commit_indexes(self, indexes, message):
        service = GitRepositoryService(self.workspace)
        service.ensure_repository()
        files = {
            ".worldconfig": json.dumps({"id": str(self.workspace.id), "branch": "main"}),
            **{f"indexes/{name.replace("_", "-")}.json": json.dumps(value, ensure_ascii=False, sort_keys=True) for name, value in indexes.items()},
        }
        service.write_snapshot(files)
        service._run(["add", "-A"])
        return service._commit_if_changed(message)

    def test_temporal_diff_reports_semantic_event_lifecycle_participant_relation_and_calendar_changes(self):
        before = {
            "entities": [
                {"id": "timeline-1", "type": "timeline", "title": "主线"},
                {"id": "event-1", "type": "event", "title": "边境相遇"},
                {"id": "event-removed", "type": "event", "title": "被删事件"},
                {"id": "character-a", "type": "character", "title": "阿尔"},
                {"id": "character-b", "type": "character", "title": "贝尔"},
            ],
            "relations": [{"id": "relation-1", "source": "character-a", "target": "character-b", "relation_type": "ally", "valid_from": 1, "valid_to": 10}],
            "time_systems": [{"id": "calendar-1", "name": "旧历", "epoch_label": "创世", "unit_name": "年", "units": [{"name": "年", "ticks": 1}]}],
            "conversions": [{"id": "conversion-1", "source_system": "calendar-1", "target_system": "calendar-2", "offset": 12, "numerator": 1, "denominator": 1}],
            "timeline_entries": [
                {"id": "entry-1", "timeline": "timeline-1", "event": "event-1", "event_title": "边境相遇", "start_value": 2, "end_value": 3, "sequence": 1, "summary": "两人相遇", "participants": [{"entity": "character-a", "role": "witness"}]},
                {"id": "entry-removed", "timeline": "timeline-1", "event": "event-removed", "event_title": "被删事件", "start_value": 4, "end_value": 4, "sequence": 2, "summary": "旧内容", "participants": []},
            ],
            "character_lifespans": [{"id": "life-a", "character": "character-a", "character_title": "阿尔", "time_system": "calendar-1", "start_value": 0, "end_value": 10}],
        }
        after = {
            **before,
            "relations": [{"id": "relation-1", "source": "character-a", "target": "character-b", "relation_type": "ally", "valid_from": 1, "valid_to": 20}, {"id": "relation-2", "source": "character-b", "target": "character-a", "relation_type": "rival", "valid_from": 8, "valid_to": None}],
            "time_systems": [{"id": "calendar-1", "name": "新历", "epoch_label": "创世", "unit_name": "年", "units": [{"name": "年", "ticks": 1}, {"name": "月", "ticks": 1 / 12}]}],
            "conversions": [{"id": "conversion-1", "source_system": "calendar-1", "target_system": "calendar-2", "offset": 13, "numerator": 1, "denominator": 1}],
            "timeline_entries": [
                {"id": "entry-1", "timeline": "timeline-1", "event": "event-1", "event_title": "边境相遇", "start_value": 5, "end_value": 6, "sequence": 1, "summary": "两人再次相遇", "participants": [{"entity": "character-a", "role": "witness"}, {"entity": "character-b", "role": "participant"}]},
                {"id": "entry-added", "timeline": "timeline-1", "event": "event-added", "event_title": "盟约签订", "start_value": 7, "end_value": 7, "sequence": 2, "summary": "签订盟约", "participants": [{"entity": "character-a", "role": "signer"}]},
            ],
            "character_lifespans": [{"id": "life-a", "character": "character-a", "character_title": "阿尔", "time_system": "calendar-1", "start_value": 0, "end_value": 20}],
        }
        first = self._commit_indexes(before, "timeline baseline")
        second = self._commit_indexes(after, "timeline evolution")
        service = GitRepositoryService(self.workspace)
        diff = service.temporal_diff(first, second)

        self.assertEqual([item["id"] for item in diff["addedEvents"]], ["entry-added"])
        self.assertEqual([item["id"] for item in diff["removedEvents"]], ["entry-removed"])
        self.assertEqual(diff["changedEvents"][0]["change_kind"], "moved")
        self.assertEqual(diff["changedEvents"][0]["id"], "entry-1")
        self.assertEqual(diff["participantChanges"][0]["added"][0]["entity"], "character-b")
        self.assertEqual(diff["lifecycleChanges"]["changed"][0]["change_kind"], "modified")
        self.assertEqual(diff["relationChanges"]["changed"][0]["change_kind"], "modified")
        self.assertEqual(diff["relationChanges"]["added"][0]["id"], "relation-2")
        self.assertTrue(any(item["code"] == "time_system_changed" for item in diff["warnings"]))
        self.assertTrue(any(item["code"] == "relation_changed" for item in diff["warnings"]))

    def test_empty_repository_returns_stable_warning(self):
        service = GitRepositoryService(self.workspace)
        service.ensure_repository()
        diff = service.temporal_diff()
        self.assertEqual(diff["schema_compatible"], True)
        self.assertEqual(diff["addedEvents"], [])
        self.assertEqual(diff["warnings"][0], "没有可比较的 Git 提交")
