from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("energy_assets", "0006_occupation_par_zone"),
    ]

    operations = [
        migrations.AddField(
            model_name="capteurpresence",
            name="occupation_simulee",
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
    ]
