import datetime
from decimal import Decimal

from django.db import migrations, models


BAREMES_2026 = {
    ("DPP", "POSTPAYE"): (
        ("Tranche 1", "0", "150", "82.0000"),
        ("Tranche 2", "150", "250", "136.4900"),
        ("Tranche 3", "250", None, "159.3600"),
    ),
    ("DPP", "PREPAYE"): (
        ("Tranche 1", "0", "150", "82.0000"),
        ("Tranche 2", "150", "250", "136.4900"),
        ("Tranche 3", "250", None, "136.4900"),
    ),
    ("DMP", "POSTPAYE"): (
        ("Tranche 1", "0", "50", "111.2300"),
        ("Tranche 2", "50", "300", "143.5400"),
        ("Tranche 3", "300", None, "158.4600"),
    ),
    ("DMP", "PREPAYE"): (
        ("Tranche 1", "0", "50", "111.2300"),
        ("Tranche 2", "50", "300", "143.5400"),
        ("Tranche 3", "300", None, "143.5400"),
    ),
    ("PPP", "POSTPAYE"): (
        ("Tranche 1", "0", "50", "147.4300"),
        ("Tranche 2", "50", "500", "189.8400"),
        ("Tranche 3", "500", None, "208.6300"),
    ),
    ("PPP", "PREPAYE"): (
        ("Tranche 1", "0", "50", "147.4300"),
        ("Tranche 2", "50", "500", "189.8400"),
        ("Tranche 3", "500", None, "189.8400"),
    ),
    ("PMP", "POSTPAYE"): (
        ("Tranche 1", "0", "100", "165.0100"),
        ("Tranche 2", "100", "500", "191.0100"),
        ("Tranche 3", "500", None, "210.8100"),
    ),
    ("PMP", "PREPAYE"): (
        ("Tranche 1", "0", "100", "165.0100"),
        ("Tranche 2", "100", "500", "191.0100"),
        ("Tranche 3", "500", None, "191.0100"),
    ),
}


def normaliser_anciennes_categories(apps, schema_editor):
    Bareme = apps.get_model("energy", "BaremeTarifaire")
    Bareme.objects.filter(
        categorie="DOMESTIQUE_PETITE_PUISSANCE"
    ).update(categorie="DPP")


def ajouter_baremes(apps, schema_editor):
    Bareme = apps.get_model("energy", "BaremeTarifaire")
    debut = datetime.date(2026, 1, 1)
    for (categorie, mode_paiement), tranches in BAREMES_2026.items():
        for ordre, (nom, kwh_min, kwh_max, prix) in enumerate(tranches, start=1):
            Bareme.objects.get_or_create(
                categorie=categorie,
                mode_paiement=mode_paiement,
                ordre=ordre,
                date_entree_vigueur=debut,
                defaults={
                    "nom_tranche": nom,
                    "kwh_min": Decimal(kwh_min),
                    "kwh_max": Decimal(kwh_max) if kwh_max is not None else None,
                    "prix_fcfa_par_kwh": Decimal(prix),
                },
            )


class Migration(migrations.Migration):

    dependencies = [
        ("energy", "0009_alter_releverituelenergetique_creneau"),
    ]

    operations = [
        migrations.AlterField(
            model_name="baremetarifaire",
            name="categorie",
            field=models.CharField(max_length=50),
        ),
        migrations.AddField(
            model_name="baremetarifaire",
            name="mode_paiement",
            field=models.CharField(
                choices=[
                    ("PREPAYE", "Prépaiement (Woyofal)"),
                    ("POSTPAYE", "Postpaiement (facture)"),
                ],
                default="POSTPAYE",
                max_length=10,
            ),
        ),
        migrations.AlterModelOptions(
            name="baremetarifaire",
            options={"ordering": ("categorie", "mode_paiement", "ordre")},
        ),
        migrations.RunPython(
            normaliser_anciennes_categories,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(ajouter_baremes, migrations.RunPython.noop),
    ]
