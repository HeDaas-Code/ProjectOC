from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0014_timesystem_calendar_rules")]

    operations = [
        migrations.AddField(
            model_name="worldworkspace",
            name="settings",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
