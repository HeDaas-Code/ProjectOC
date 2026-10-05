from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [("canvas", "0009_dialoguememory_manual_fields_and_audit")]

    operations = [
        migrations.CreateModel(
            name="CanvasSyncEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("room_key", models.CharField(max_length=500)),
                ("document_clock", models.PositiveBigIntegerField(default=0)),
                ("snapshot_hash", models.CharField(max_length=64)),
                ("diff", models.JSONField(blank=True, default=dict)),
                ("protocol_version", models.CharField(default="tldraw-sync-v2", max_length=80)),
                ("schema_version", models.CharField(default="oc-tldraw-2", max_length=120)),
                ("source", models.CharField(default="sync-service", max_length=80)),
                ("client_session", models.CharField(blank=True, default="", max_length=160)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="canvas_sync_events", to="core.worldbranch")),
                ("canvas", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sync_events", to="canvas.stagingcanvas")),
            ],
            options={
                "ordering": ["document_clock", "created_at"],
                "indexes": [
                    models.Index(fields=["canvas", "document_clock"], name="canvas_sync_event_cursor_idx"),
                    models.Index(fields=["room_key", "created_at"], name="canvas_sync_event_room_idx"),
                ],
                "constraints": [models.UniqueConstraint(fields=("canvas", "document_clock", "snapshot_hash"), name="canvas_sync_event_clock_hash")],
            },
        ),
    ]
