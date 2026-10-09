import uuid

import django.db.models.deletion
from django.db import migrations, models


def seed_canvas_containers(apps, schema_editor):
    CanvasContainer = apps.get_model("canvas", "CanvasContainer")
    StagingCanvas = apps.get_model("canvas", "StagingCanvas")
    for canvas in StagingCanvas.objects.filter(purpose="staging"):
        CanvasContainer.objects.get_or_create(
            canvas_id=canvas.id,
            defaults={"workspace_id": canvas.workspace_id, "branch_id": canvas.branch_id, "name": canvas.name},
        )


class Migration(migrations.Migration):
    dependencies = [("canvas", "0012_relationproposal_source_entity_and_more")]

    operations = [
        migrations.CreateModel(
            name="CanvasContainer",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("sort_order", models.IntegerField(default=0)),
                ("status", models.CharField(choices=[("active", "活跃"), ("archived", "归档"), ("committed", "已提交")], default="active", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="canvas_containers", to="core.worldbranch")),
                ("canvas", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="container", to="canvas.stagingcanvas")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="children", to="canvas.canvascontainer")),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="canvas_containers", to="core.worldworkspace")),
            ],
            options={"ordering": ["sort_order", "name", "created_at"]},
        ),
        migrations.AddConstraint(
            model_name="canvascontainer",
            constraint=models.UniqueConstraint(fields=("workspace", "parent", "name"), name="unique_container_name_under_parent"),
        ),
        migrations.RunPython(seed_canvas_containers, migrations.RunPython.noop),
    ]
