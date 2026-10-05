"""Durable, retryable workers for Git and graph projection outboxes.

PostgreSQL remains the source of truth.  A short lease prevents two worker
processes from claiming the same job, while the idempotency marker in Git and
the rebuild semantics in the graph projection make retries safe.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import CommitJob, GraphProjectionJob


@dataclass(frozen=True)
class WorkerResult:
    commits_succeeded: int = 0
    commits_failed: int = 0
    projections_succeeded: int = 0
    projections_failed: int = 0

    @property
    def processed(self) -> int:
        return sum(
            (
                self.commits_succeeded,
                self.commits_failed,
                self.projections_succeeded,
                self.projections_failed,
            )
        )


class OutboxWorker:
    """Claim and process durable outbox jobs.

    The worker is deliberately framework-light so it can run as a standalone
    Django management command in the private deployment.  It does not require
    Celery or a broker, and multiple workers can safely share PostgreSQL.
    """

    def __init__(
        self,
        *,
        lease_seconds: int | None = None,
        base_retry_seconds: int | None = None,
        max_retry_seconds: int | None = None,
        max_attempts: int | None = None,
    ):
        self.lease_seconds = lease_seconds or int(os.getenv("OUTBOX_WORKER_LEASE_SECONDS", "120"))
        self.base_retry_seconds = base_retry_seconds or int(os.getenv("OUTBOX_RETRY_BASE_SECONDS", "5"))
        self.max_retry_seconds = max_retry_seconds or int(os.getenv("OUTBOX_RETRY_MAX_SECONDS", "900"))
        self.max_attempts = max_attempts or int(os.getenv("OUTBOX_MAX_ATTEMPTS", "12"))

    @staticmethod
    def _ready_filter(now):
        return Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)

    @staticmethod
    def _lease_filter(now):
        return Q(lease_until__isnull=True) | Q(lease_until__lte=now)

    def _claim_commits(self, *, workspace=None, limit=20):
        now = timezone.now()
        with transaction.atomic():
            query = CommitJob.objects.filter(
                status__in=[CommitJob.Status.DATABASE_COMMITTED, CommitJob.Status.GIT_SYNC_FAILED],
            ).filter(self._ready_filter(now), self._lease_filter(now)).order_by("created_at")
            if workspace is not None:
                query = query.filter(workspace=workspace)
            try:
                jobs = list(query.select_for_update(skip_locked=True)[:limit])
            except TypeError:  # SQLite and older backends ignore skip_locked.
                jobs = list(query.select_for_update()[:limit])
            lease_until = now + timedelta(seconds=self.lease_seconds)
            for job in jobs:
                CommitJob.objects.filter(pk=job.pk).update(lease_until=lease_until, last_attempt_at=now)
        return jobs

    def _claim_projections(self, *, workspace=None, limit=20):
        now = timezone.now()
        with transaction.atomic():
            query = GraphProjectionJob.objects.filter(
                status__in=[GraphProjectionJob.Status.PENDING, GraphProjectionJob.Status.FAILED],
            ).filter(self._ready_filter(now), self._lease_filter(now)).order_by("created_at")
            if workspace is not None:
                query = query.filter(workspace=workspace)
            try:
                jobs = list(query.select_for_update(skip_locked=True)[:limit])
            except TypeError:
                jobs = list(query.select_for_update()[:limit])
            lease_until = now + timedelta(seconds=self.lease_seconds)
            for job in jobs:
                GraphProjectionJob.objects.filter(pk=job.pk).update(lease_until=lease_until, last_attempt_at=now)
        return jobs

    def _retry_at(self, attempts: int):
        # attempts is one-based after the service starts processing.
        exponent = max(0, min(attempts - 1, 16))
        delay = min(self.max_retry_seconds, self.base_retry_seconds * (2**exponent))
        return timezone.now() + timedelta(seconds=delay)

    def _finish_commit(self, job: CommitJob):
        job.refresh_from_db()
        if job.status == CommitJob.Status.GIT_SYNC_FAILED:
            values = {"lease_until": None}
            if job.attempts >= self.max_attempts:
                values["next_attempt_at"] = None
            else:
                values["next_attempt_at"] = self._retry_at(job.attempts)
            CommitJob.objects.filter(pk=job.pk).update(**values)
        else:
            CommitJob.objects.filter(pk=job.pk).update(lease_until=None, next_attempt_at=None)

    def _finish_projection(self, job: GraphProjectionJob):
        job.refresh_from_db()
        if job.status == GraphProjectionJob.Status.FAILED:
            values = {"lease_until": None}
            if job.attempts >= self.max_attempts:
                values["next_attempt_at"] = None
            else:
                values["next_attempt_at"] = self._retry_at(job.attempts)
            GraphProjectionJob.objects.filter(pk=job.pk).update(**values)
        else:
            GraphProjectionJob.objects.filter(pk=job.pk).update(lease_until=None, next_attempt_at=None)

    def run_once(self, *, workspace=None, limit=20, include_projection=True) -> WorkerResult:
        """Process at most ``limit`` jobs from each outbox."""
        from apps.canvas.services.commit import CanvasCommitService
        from apps.core.services.neo4j_projection import Neo4jProjection

        commit_ok = commit_failed = projection_ok = projection_failed = 0
        for job in self._claim_commits(workspace=workspace, limit=limit):
            CanvasCommitService.sync_job(job)
            self._finish_commit(job)
            job.refresh_from_db()
            if job.status == CommitJob.Status.SYNCED:
                commit_ok += 1
            else:
                commit_failed += 1

        if include_projection:
            for job in self._claim_projections(workspace=workspace, limit=limit):
                Neo4jProjection().process_job(job)
                self._finish_projection(job)
                job.refresh_from_db()
                if job.status == GraphProjectionJob.Status.SYNCED:
                    projection_ok += 1
                else:
                    projection_failed += 1

        return WorkerResult(commit_ok, commit_failed, projection_ok, projection_failed)

    def run_forever(
        self,
        *,
        interval_seconds: float = 5.0,
        limit: int = 20,
        include_projection: bool = True,
        stop: Callable[[], bool] | None = None,
        on_result: Callable[[WorkerResult], None] | None = None,
    ):
        """Run until ``stop`` returns true; intended for the compose worker."""
        while True:
            if stop and stop():
                return
            result = self.run_once(limit=limit, include_projection=include_projection)
            if on_result:
                on_result(result)
            if result.processed == 0:
                time.sleep(max(0.1, interval_seconds))
