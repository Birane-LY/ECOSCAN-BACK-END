from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0005_organisation_details_demande"),
    ]

    operations = [
        migrations.AddField(
            model_name="compteur",
            name="categorie_tarifaire",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Catégorie tarifaire vérifiée du compteur, requise pour toute prévision de tranche.",
                max_length=50,
            ),
        ),
    ]
