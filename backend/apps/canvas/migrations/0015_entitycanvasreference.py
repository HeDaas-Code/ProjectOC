import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("canvas", "0014_entityproposal_operation_target")]
    operations = [
        migrations.CreateModel(
            name="EntityCanvasReference",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("branch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="entity_canvas_references", to="core.worldbranch")),
                ("container", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="entity_references", to="canvas.canvascontainer")),
                ("entity", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="canvas_references", to="core.entity")),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="entity_canvas_references", to="core.worldworkspace")),
            ],
        ),
        migrations.AddConstraint(model_name="entitycanvasreference", constraint=models.UniqueConstraint(fields=("container", "entity"), name="unique_entity_container_reference")),
    ]
