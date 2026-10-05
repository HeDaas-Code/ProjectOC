from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_entity_base_entity_relation_archived_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="graphprojectionjob",
            name="commit_job",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="projection_job",
                to="core.commitjob",
            ),
        ),
    ]
