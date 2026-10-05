from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.core.models import CommitJob, Entity, GraphProjectionJob, WorldWorkspace
from apps.core.services.outbox_worker import OutboxWorker


class OutboxWorkerTests(TestCase):
    def setUp(self):
        self.workspace = WorldWorkspace.objects.create(name="Worker World", slug="worker-world")

    def test_git_failure_is_leased_and_retried_with_backoff(self):
        entity = Entity.objects.create(
            workspace=self.workspace,
            type=Entity.EntityType.ITEM,
            title="待同步物品",
        )
        job = CommitJob.objects.create(
            workspace=self.workspace,
            idempotency_key="worker-git-1",
            status=CommitJob.Status.DATABASE_COMMITTED,
            entity_ids=[str(entity.id)],
        )
        worker = OutboxWorker(base_retry_seconds=60, max_retry_seconds=60, max_attempts=3)

        with patch("apps.canvas.services.commit.GitRepositoryService.commit_job", side_effect=RuntimeError("git offline")):
            result = worker.run_once(workspace=self.workspace, include_projection=False)

        self.assertEqual(result.commits_failed, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, CommitJob.Status.GIT_SYNC_FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.next_attempt_at)
        self.assertIsNone(job.lease_until)

        # The not-yet-due job is not claimed again.
        self.assertEqual(worker.run_once(workspace=self.workspace, include_projection=False).processed, 0)

        CommitJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        with patch("apps.canvas.services.commit.GitRepositoryService.commit_job", return_value="abc123"):
            result = worker.run_once(workspace=self.workspace, include_projection=False)

        self.assertEqual(result.commits_succeeded, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, CommitJob.Status.SYNCED)
        self.assertEqual(job.attempts, 2)
        self.assertEqual(job.commit_hash, "abc123")
        self.assertIsNone(job.next_attempt_at)
        self.assertIsNone(job.lease_until)

    def test_projection_failure_is_retried_and_lease_is_released(self):
        job = GraphProjectionJob.objects.create(workspace=self.workspace)
        worker = OutboxWorker(base_retry_seconds=30, max_retry_seconds=30, max_attempts=2)

        with patch("apps.core.services.neo4j_projection.Neo4jProjection.rebuild", side_effect=RuntimeError("neo4j offline")):
            result = worker.run_once(workspace=self.workspace, limit=1)

        self.assertEqual(result.projections_failed, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, GraphProjectionJob.Status.FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.next_attempt_at)
        self.assertIsNone(job.lease_until)

        GraphProjectionJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        with patch("apps.core.services.neo4j_projection.Neo4jProjection.rebuild"):
            result = worker.run_once(workspace=self.workspace, limit=1)

        self.assertEqual(result.projections_succeeded, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, GraphProjectionJob.Status.SYNCED)
        self.assertEqual(job.attempts, 2)
        self.assertIsNone(job.next_attempt_at)
        self.assertIsNone(job.lease_until)

    def test_active_lease_prevents_second_claim(self):
        job = CommitJob.objects.create(
            workspace=self.workspace,
            idempotency_key="worker-lease-1",
            status=CommitJob.Status.DATABASE_COMMITTED,
        )
        worker = OutboxWorker(lease_seconds=120)
        claimed = worker._claim_commits(workspace=self.workspace, limit=1)
        self.assertEqual([job.id], [item.id for item in claimed])
        self.assertEqual(worker._claim_commits(workspace=self.workspace, limit=1), [])
