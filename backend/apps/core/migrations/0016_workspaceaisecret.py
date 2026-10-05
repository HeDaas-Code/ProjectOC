from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0015_worldworkspace_settings")]
    operations = [
        migrations.CreateModel(
            name="WorkspaceAISecret",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("api_key_encrypted", models.TextField(blank=True, default="")),
                ("api_key_last4", models.CharField(blank=True, default="", max_length=4)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("workspace", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ai_secret", to="core.worldworkspace")),
            ],
        ),
    ]
