"""Run the durable Git/projection outbox worker."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.core.services.outbox_worker import OutboxWorker


class Command(BaseCommand):
    help = "Process durable Git and Neo4j outbox jobs with leases and retry backoff"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one batch and exit")
        parser.add_argument("--loop", action="store_true", help="Keep polling until interrupted")
        parser.add_argument("--interval", type=float, default=5.0)
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--no-projection", action="store_true")

    def handle(self, *args, **options):
        worker = OutboxWorker()
        include_projection = not options["no_projection"]
        limit = max(1, options["limit"])

        def report(result):
            if result.processed:
                self.stdout.write(
                    f"outbox: commits {result.commits_succeeded} ok/{result.commits_failed} failed; "
                    f"projections {result.projections_succeeded} ok/{result.projections_failed} failed"
                )

        if options["loop"] and not options["once"]:
            try:
                worker.run_forever(
                    interval_seconds=max(0.1, options["interval"]),
                    limit=limit,
                    include_projection=include_projection,
                    on_result=report,
                )
            except KeyboardInterrupt:
                self.stdout.write("outbox worker stopped")
            return

        result = worker.run_once(limit=limit, include_projection=include_projection)
        report(result)
