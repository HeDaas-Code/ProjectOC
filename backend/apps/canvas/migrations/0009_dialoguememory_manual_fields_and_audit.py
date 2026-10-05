from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [("canvas", "0008_rename_canvas_dialo_workspa_8d9f6e_idx_canvas_dial_workspa_928b2e_idx")]

    operations = [
        migrations.AddField(
            model_name="dialoguememory",
            name="manual_notes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="dialoguememory",
            name="archived_questions",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="DialogueMemoryAudit",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("action", models.CharField(max_length=40)),
                ("before", models.JSONField(blank=True, default=dict)),
                ("after", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="dialogue_memory_audits",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "memory",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="audits",
                        to="canvas.dialoguememory",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["memory", "created_at"], name="canvas_dial_memory__5b7b75_idx")],
            },
        ),
    ]
