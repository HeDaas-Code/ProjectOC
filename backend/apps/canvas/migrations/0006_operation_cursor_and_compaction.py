from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("canvas", "0005_stagingcanvas_operation_sequence_canvasoperation"),
    ]

    operations = [
        migrations.AddField(
            model_name="stagingcanvas",
            name="snapshot_operation_cursor",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="stagingcanvas",
            name="operation_compacted_through",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="canvasrevision",
            name="operation_cursor",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
