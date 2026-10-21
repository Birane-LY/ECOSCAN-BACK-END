from django.db import migrations, models


CATEGORIES = {
    "DOMESTIQUE_PETITE_PUISSANCE": "DPP",
    "DOMESTIQUE_MOYENNE_PUISSANCE": "DMP",
    "PROFESSIONNELLE_PETITE_PUISSANCE": "PPP",
    "PROFESSIONNEL_PETITE_PUISSANCE": "PPP",
    "PROFESSIONNELLE_MOYENNE_PUISSANCE": "PMP",
    "PROFESSIONNEL_MOYENNE_PUISSANCE": "PMP",
}


def normaliser_categories(apps, schema_editor):
    Compteur = apps.get_model("organizations", "Compteur")
    for ancienne, nouvelle in CATEGORIES.items():
        Compteur.objects.filter(categorie_tarifaire=ancienne).update(
            categorie_tarifaire=nouvelle
        )
    Compteur.objects.exclude(
        categorie_tarifaire__in=["", "DPP", "DMP", "PPP", "PMP"]
    ).update(categorie_tarifaire="")


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0006_compteur_categorie_tarifaire"),
    ]

    operations = [
        migrations.RunPython(normaliser_categories, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="compteur",
            name="categorie_tarifaire",
            field=models.CharField(
                blank=True,
                choices=[
                    ("DPP", "Domestique petite puissance"),
                    ("DMP", "Domestique moyenne puissance"),
                    ("PPP", "Professionnel petite puissance"),
                    ("PMP", "Professionnel moyenne puissance"),
                ],
                default="",
                help_text="Catégorie tarifaire vérifiée du compteur, requise pour toute prévision de tranche.",
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="compteur",
            name="mode_paiement",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Non configuré"),
                    ("PREPAYE", "Prépaiement (Woyofal)"),
                    ("POSTPAYE", "Postpaiement (facture)"),
                ],
                default="",
                help_text="Mode de paiement vérifié sur le contrat ou le compteur SENELEC.",
                max_length=10,
            ),
        ),
    ]
