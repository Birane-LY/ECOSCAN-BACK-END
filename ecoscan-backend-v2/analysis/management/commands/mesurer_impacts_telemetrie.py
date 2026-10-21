from django.core.management.base import BaseCommand

from analysis.models import Action
from analysis.services.telemetry_impact import enregistrer_impact_telemetrie_action


class Command(BaseCommand):
    help = "Mesure les économies d'énergie des actions terminées à partir des capteurs ENERGY réels."

    def handle(self, *args, **options):
        actions = Action.objects.filter(
            statut=Action.Statut.TERMINEE,
            date_realisation__isnull=False,
            equipement__isnull=False,
            economie_energie_kwh__isnull=True,
        ).select_related("equipement__site")
        mesurees = en_attente = indisponibles = 0

        for action in actions.iterator():
            resultat = enregistrer_impact_telemetrie_action(action)
            if resultat["statut"] == "mesure":
                mesurees += 1
                self.stdout.write(f"Action {action.pk} : {resultat['economie_kwh']} kWh")
            elif resultat["statut"] == "en_attente":
                en_attente += 1
            else:
                indisponibles += 1
                self.stderr.write(f"Action {action.pk} : {resultat['raison']}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Mesurées : {mesurees} ; en attente : {en_attente} ; "
                f"indisponibles ou incomplètes : {indisponibles}."
            )
        )
