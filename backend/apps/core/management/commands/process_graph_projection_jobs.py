"""Retry pending/failed Neo4j projection outbox jobs once."""
from django.core.management.base import BaseCommand
from apps.core.models import GraphProjectionJob
from apps.core.services.neo4j_projection import Neo4jProjection


class Command(BaseCommand):
    help = "Retry pending or failed Neo4j graph projection jobs"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        jobs = GraphProjectionJob.objects.filter(
            status__in=[GraphProjectionJob.Status.PENDING, GraphProjectionJob.Status.FAILED]
        ).select_related("workspace", "commit_job").order_by("created_at")[:max(0, options["limit"])]
        service = Neo4jProjection()
        succeeded = failed = 0
        for job in jobs:
            ok, error = service.process_job(job)
            if ok:
                succeeded += 1
            else:
                failed += 1
                self.stderr.write(f"{job.pk}: {error}")
        self.stdout.write(self.style.SUCCESS(f"Projection jobs: {succeeded} synced, {failed} failed"))
