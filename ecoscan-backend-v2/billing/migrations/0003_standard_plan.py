from django.db import migrations


def create_standard_plan(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.get_or_create(
        code="standard",
        defaults={
            "nom": "Standard",
            "description": "Plan standard EcoScan.",
            "prix_mensuel": "10000.00",
            "prix_annuel": "100000.00",
            "devise": "XOF",
            "limites": {},
            "fonctionnalites": {},
            "actif": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_abonnement_fin_grace"),
    ]

    operations = [
        migrations.RunPython(create_standard_plan, migrations.RunPython.noop),
    ]
