import datetime
from decimal import Decimal
from django.db import migrations

TRANCHES = [
    (1, "Tranche 1 (tarif social)", "0", "150", "82.0000"),
    (2, "Tranche 2", "150", "250", "136.4900"),
    (3, "Tranche 3", "250", None, "159.3600"),
]

def seed(apps, schema_editor):
    Bareme = apps.get_model("energy", "BaremeTarifaire")
    for ordre, nom, kmin, kmax, prix in TRANCHES:
        Bareme.objects.get_or_create(
            categorie="DOMESTIQUE_PETITE_PUISSANCE", ordre=ordre,
            date_entree_vigueur=datetime.date(2026, 1, 1),
            defaults=dict(nom_tranche=nom, kwh_min=Decimal(kmin),
                          kwh_max=Decimal(kmax) if kmax else None,
                          prix_fcfa_par_kwh=Decimal(prix)),
        )

def unseed(apps, schema_editor):
    apps.get_model("energy", "BaremeTarifaire").objects.filter(
        date_entree_vigueur=datetime.date(2026, 1, 1)).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('energy', '0002_baremetarifaire_achatwoyofal'),
    ]

    operations = [migrations.RunPython(seed, unseed)]

