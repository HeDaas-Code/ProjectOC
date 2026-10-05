from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="workspaceinvite",
            name="email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
    ]
