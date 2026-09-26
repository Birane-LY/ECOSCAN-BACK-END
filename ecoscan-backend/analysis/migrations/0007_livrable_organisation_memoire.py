import django.db.models.deletion
from django.db import migrations, models


def rattacher_organisations_existantes(apps, schema_editor):
    Livrable = apps.get_model("analysis", "Livrable")
    for livrable in Livrable.objects.select_related("fiche_projet").iterator():
        livrable.organisation_id = livrable.fiche_projet.organisation_id
        livrable.save(update_fields=("organisation",))


class Migration(migrations.Migration):
    dependencies = [
        ("analysis", "0006_recommandation_anomalie"),
    ]

    operations = [
        migrations.AddField(
            model_name="livrable",
            name="organisation",
            field=models.ForeignKey(
                to="organizations.organisation",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="livrables",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="livrable",
            name="memoire",
            field=models.ForeignKey(
                to="analysis.memoirestrategique",
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="livrables",
                null=True,
                blank=True,
            ),
        ),
        migrations.RunPython(rattacher_organisations_existantes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="livrable",
            name="organisation",
            field=models.ForeignKey(
                to="organizations.organisation",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="livrables",
            ),
        ),
        migrations.AlterField(
            model_name="livrable",
            name="fiche_projet",
            field=models.ForeignKey(
                to="organizations.ficheprojet",
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="livrables",
                null=True,
                blank=True,
            ),
        ),
    ]