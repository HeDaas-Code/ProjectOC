"""Recover durable database/Git commit jobs after an interrupted process."""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.models import CommitJob, Entity, WorldBranch, WorldWorkspace
from apps.version_control.services.git_sync import GitRepositoryService


class Command(BaseCommand):
    help = "Reconcile database commit jobs with marker commits in world repositories"

    def add_arguments(self, parser):
        parser.add_argument("--workspace", dest="workspace_id")
        parser.add_argument(
            "--retry",
            action="store_true",
            help="After reconciliation, retry jobs that remain database-committed",
        )

    def handle(self, *args, **options):
        workspace_filter = options.get("workspace_id")
        workspaces = WorldWorkspace.objects.all().order_by("id")
        if workspace_filter:
            workspaces = workspaces.filter(id=workspace_filter)

        inspected = recovered = requeued = failed = 0
        for workspace in workspaces:
            repository = GitRepositoryService(workspace)
            jobs = list(
                CommitJob.objects.filter(
                    workspace=workspace,
                    status__in=[
                        CommitJob.Status.DATABASE_COMMITTED,
                        CommitJob.Status.GIT_SYNCING,
                        CommitJob.Status.GIT_SYNC_FAILED,
                    ],
                ).select_related("branch").order_by("created_at")
            )
            for job in jobs:
                inspected += 1
                try:
                    git = repository
                    branch = job.branch
                    if branch is not None:
                        ref, base_commit = repository.prepare_branch(branch)
                        if branch.git_ref != ref or branch.base_commit != base_commit:
                            WorldBranch.objects.filter(pk=branch.pk).update(
                                git_ref=ref, base_commit=base_commit
                            )
                            branch.git_ref, branch.base_commit = ref, base_commit
                        git = repository.branch_checkout(branch)

                    marker_commit = git.find_commit_by_marker(job, ref="HEAD")
                    if marker_commit:
                        with transaction.atomic():
                            CommitJob.objects.filter(pk=job.pk).update(
                                status=CommitJob.Status.SYNCED,
                                commit_hash=marker_commit,
                                error_message="",
                            )
                            Entity.objects.filter(id__in=job.entity_ids).update(
                                sync_status=Entity.SyncStatus.SYNCED,
                                commit_hash=marker_commit,
                            )
                        recovered += 1
                        continue

                    # No marker means no durable Git commit for this job. Put
                    # it back into the safe outbox state; --retry can replay it
                    # and the marker makes the replay idempotent.
                    with transaction.atomic():
                        CommitJob.objects.filter(pk=job.pk).update(
                            status=CommitJob.Status.DATABASE_COMMITTED,
                            error_message=(
                                "reconciled: no OC-Job marker found; safe to retry"
                            ),
                        )
                    requeued += 1
                except Exception as exc:
                    CommitJob.objects.filter(pk=job.pk).update(
                        status=CommitJob.Status.GIT_SYNC_FAILED,
                        error_message=f"reconciliation failed: {exc}",
                    )
                    failed += 1
                    self.stderr.write(f"{workspace.pk}/{job.pk}: {exc}")

            if options.get("retry"):
                from apps.canvas.services.commit import CanvasCommitService

                CanvasCommitService.sync_pending(workspace)

        self.stdout.write(
            self.style.SUCCESS(
                f"Commit jobs: {inspected} inspected, {recovered} recovered, "
                f"{requeued} requeued, {failed} failed"
            )
        )
