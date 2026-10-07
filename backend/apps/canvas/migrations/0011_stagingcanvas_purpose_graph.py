from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("canvas", "0010_canvassyncevent")]

    operations = [
        migrations.AddField(
            model_name="stagingcanvas",
            name="purpose",
            field=models.CharField(choices=[("staging", "灵感暂存"), ("graph", "世界观图谱")], default="staging", max_length=16),
        ),
        migrations.AddConstraint(
            model_name="stagingcanvas",
            constraint=models.UniqueConstraint(condition=models.Q(purpose="graph"), fields=("workspace", "branch"), name="unique_graph_canvas_workspace_branch"),
        ),
    ]
