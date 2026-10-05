"""Safely compact durable canvas operation logs.

Only operations already included in the persisted PostgreSQL snapshot may be
removed.  This command is intended for maintenance/recovery jobs; the sync
service still performs best-effort compaction after snapshots in normal
operation.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.canvas.models import CanvasOperation, StagingCanvas


class Command(BaseCommand):
    help = "Safely compact durable canvas operation logs after snapshot checkpoints"

    def add_arguments(self, parser):
        parser.add_argument("--canvas", dest="canvas_id", help="Only compact this canvas UUID")
        parser.add_argument("--workspace", dest="workspace_id", help="Only compact canvases in this workspace UUID")
        parser.add_argument(
            "--keep-last",
            type=int,
            default=100,
            help="Keep this many operations at or below the snapshot checkpoint (default: 100)",
        )
        parser.add_argument(
            "--min-operations",
            type=int,
            default=0,
            help="Skip canvases with fewer than this many persisted operations",
        )
        parser.add_argument("--include-archived", action="store_true", help="Also compact archived canvases")
        parser.add_argument("--dry-run", action="store_true", help="Report eligible deletions without deleting")

    def handle(self, *args, **options):
        keep_last = options["keep_last"]
        min_operations = options["min_operations"]
        if keep_last < 0:
            raise CommandError("--keep-last must be >= 0")
        if min_operations < 0:
            raise CommandError("--min-operations must be >= 0")

        canvases = StagingCanvas.objects.all().order_by("id")
        if options.get("canvas_id"):
            canvases = canvases.filter(id=options["canvas_id"])
        if options.get("workspace_id"):
            canvases = canvases.filter(workspace_id=options["workspace_id"])
        if not options["include_archived"]:
            canvases = canvases.exclude(status=StagingCanvas.Status.ARCHIVED)

        inspected = compacted = deleted = skipped = 0
        for candidate in canvases.iterator():
            inspected += 1
            result = self._compact_canvas(
                candidate.pk,
                keep_last=keep_last,
                min_operations=min_operations,
                dry_run=options["dry_run"],
            )
            if result["skipped"]:
                skipped += 1
            else:
                compacted += 1
                deleted += result["deleted"]
            self.stdout.write(
                f"{candidate.pk} {candidate.name}: "
                f"checkpoint={result['checkpoint']} target={result['target']} "
                f"deleted={result['deleted']}" + (" (dry-run)" if options["dry_run"] else "")
            )

        mode = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write(
            self.style.SUCCESS(
                f"Canvas operation compaction: inspected={inspected}, "
                f"eligible={compacted}, skipped={skipped}, {mode}={deleted}"
            )
        )

    @staticmethod
    def _compact_canvas(canvas_id, *, keep_last: int, min_operations: int, dry_run: bool) -> dict:
        with transaction.atomic():
            canvas = StagingCanvas.objects.select_for_update().get(pk=canvas_id)
            total = CanvasOperation.objects.filter(canvas=canvas).count()
            if total < min_operations:
                return {"skipped": True, "checkpoint": canvas.snapshot_operation_cursor, "target": canvas.operation_compacted_through, "deleted": 0}

            # Never compact beyond the checkpoint materialized in the durable
            # snapshot, and keep the newest operations in that safe range for
            # reconnect diagnostics/replay.
            checkpoint = min(canvas.snapshot_operation_cursor, canvas.operation_sequence)
            target = max(canvas.operation_compacted_through, checkpoint - keep_last)
            if target <= canvas.operation_compacted_through:
                return {"skipped": True, "checkpoint": checkpoint, "target": canvas.operation_compacted_through, "deleted": 0}

            deletable = CanvasOperation.objects.filter(canvas=canvas, sequence__lte=target)
            count = deletable.count()
            if not dry_run:
                deletable.delete()
                canvas.operation_compacted_through = target
                canvas.save(update_fields=["operation_compacted_through", "updated_at"])
            return {"skipped": False, "checkpoint": checkpoint, "target": target, "deleted": count}
