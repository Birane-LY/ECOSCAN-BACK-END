"""Commande périodique d'analyse des consommations télémétriques."""

from datetime import date
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from energy_assets.anomalies import analyser_anomalies_sites
from organizations.models import Site


class Command(BaseCommand):
    help = "Détecte les anomalies de consommation à partir de la télémétrie."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=date.fromisoformat,
            help="Journée locale à analyser au format AAAA-MM-JJ (défaut : hier).",
        )
        parser.add_argument(
            "--site",
            type=UUID,
            help="Identifiant du site à analyser (défaut : tous les sites).",
        )

    def handle(self, *args, **options):
        if options["site"] and not Site.objects.filter(pk=options["site"]).exists():
            raise CommandError("Le site demandé est introuvable.")

        resultats = analyser_anomalies_sites(
            jour=options["date"],
            site_id=options["site"],
        )
        compteurs = {
            statut: sum(resultat["status"] == statut for resultat in resultats)
            for statut in ("anomaly_detected", "normal", "insufficient_data")
        }
        self.stdout.write(
            "Sites analysés : {total}; anomalies : {anomalies}; normaux : {normaux}; "
            "données insuffisantes : {insuffisants}".format(
                total=len(resultats),
                anomalies=compteurs["anomaly_detected"],
                normaux=compteurs["normal"],
                insuffisants=compteurs["insufficient_data"],
            )
        )
