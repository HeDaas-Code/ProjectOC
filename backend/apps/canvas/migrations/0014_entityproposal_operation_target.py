import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("canvas", "0013_canvascontainer")]
    operations = [
        migrations.AddField(
            model_name="entityproposal",
            name="operation",
            field=models.CharField(choices=[("create", "创建"), ("update", "修改"), ("archive", "归档")], default="create", max_length=20),
        ),
        migrations.AddField(
            model_name="entityproposal",
            name="target_entity",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="edit_proposals", to="core.entity"),
        ),
    ]
