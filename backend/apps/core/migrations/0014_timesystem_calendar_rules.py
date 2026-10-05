from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0013_timesystemconversion"),
    ]

    operations = [
        migrations.AddField(
            model_name="timesystem",
            name="calendar_rules",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
