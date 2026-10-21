from datetime import date
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from energy_assets.occupancy_anomalies import analyser_occupation_site
from organizations.models import Site


class Command(BaseCommand):
    help = "Détecte les usages énergétiques en zones récemment confirmées inoccupées."

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
        site_id = options["site"]
        sites = Site.objects.filter(
            zones__active=True,
            zones__capteurs_presence__statut="ACTIVE",
            equipements__monitoring_active=True,
        ).distinct().order_by("id")
        if site_id:
            if not Site.objects.filter(pk=site_id).exists():
                raise CommandError("Le site demandé est introuvable.")
            sites = sites.filter(pk=site_id)

        results = [
            analyser_occupation_site(site, jour=options["date"])
            for site in sites.iterator()
        ]
        self.stdout.write(
            "Sites analysés : {total}; zones évaluées : {zones}; "
            "anomalies actives : {anomalies}; données insuffisantes : {insuffisants}".format(
                total=len(results),
                zones=sum(result["zones_analyzed"] for result in results),
                anomalies=sum(result["anomalies_detected"] for result in results),
                insuffisants=sum(
                    result["status"] == "insufficient_data" for result in results
                ),
            )
        )
