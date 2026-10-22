"""Commande périodique d'émission de télémétrie simulée."""

import math
import time
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from energy_assets.autopilot import evaluer_pilotage_autonome
from energy_assets.models import Capteur, PilotageAutonome
from energy_assets.simulation import (
    generer_mesures_capteurs_simules,
    generer_mesures_presence_simulees,
)
from organizations.models import Organisation


FREQUENCE_ENERGIE_SIMULEE_SECONDES = 15


class Command(BaseCommand):
    help = "Émet des mesures pour les capteurs simulés actifs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Effectue un seul cycle au lieu de tourner en continu.",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=1,
            help="Délai de vérification en secondes (défaut : 1).",
        )
        parser.add_argument(
            "--organisation-id",
            type=UUID,
            action="append",
            help=(
                "Limite les relevés simulés à cette organisation (UUID). "
                "Peut être fourni plusieurs fois."
            ),
        )
        parser.add_argument(
            "--demo-scenarios",
            action="store_true",
            help="Inclut les organisations portant le marqueur de scénario EcoScan Demo.",
        )
        parser.add_argument(
            "--no-autopilot",
            action="store_true",
            help="N'évalue pas les politiques d'autonomie pendant l'émission des mesures.",
        )

    def handle(self, *args, **options):
        intervalle = options["interval"]
        if not math.isfinite(intervalle) or intervalle <= 0:
            raise CommandError("L'intervalle de vérification doit être positif.")
        organisation_ids = list(options["organisation_id"] or [])
        demo_organisation_ids = []
        if options["demo_scenarios"]:
            demo_organisation_ids = list(
                Organisation.objects.filter(
                    details_demande__ecoscan_demo_scenario__in=(
                        "multisite",
                        "hotel",
                        "usine",
                        "restaurant",
                    )
                ).values_list("id", flat=True)
            )
            organisation_ids.extend(demo_organisation_ids)
        organisation_ids = list(dict.fromkeys(organisation_ids))
        for organisation_id in organisation_ids:
            if not Organisation.objects.filter(pk=organisation_id).exists():
                raise CommandError(
                    f"L'organisation « {organisation_id} » n'existe pas."
                )

        if organisation_ids:
            capteurs_energie_ajustes = Capteur.objects.filter(
                equipement__site__organisation_id__in=organisation_ids,
                mode=Capteur.Mode.SIMULATED,
                type__iexact="ENERGY",
                frequence_secondes__gt=FREQUENCE_ENERGIE_SIMULEE_SECONDES,
            ).update(
                frequence_secondes=FREQUENCE_ENERGIE_SIMULEE_SECONDES
            )
            if capteurs_energie_ajustes:
                self.stdout.write(
                    self.style.SUCCESS(
                        "Fréquence des capteurs ENERGY simulés ciblés "
                        f"ramenée à {FREQUENCE_ENERGIE_SIMULEE_SECONDES} s "
                        f"({capteurs_energie_ajustes} capteurs)."
                    )
                )

        while True:
            if organisation_ids:
                nombre = sum(
                    generer_mesures_capteurs_simules(
                        organisation_id=organisation_id,
                    )
                    for organisation_id in organisation_ids
                )
                presences = sum(
                    generer_mesures_presence_simulees(
                        organisation_id=organisation_id,
                    )
                    for organisation_id in organisation_ids
                )
            else:
                nombre = generer_mesures_capteurs_simules()
                presences = generer_mesures_presence_simulees()
            decisions = 0
            if not options["no_autopilot"]:
                politiques = PilotageAutonome.objects.filter(
                    Q(actif=True) | Q(arret_urgence=True)
                )
                if organisation_ids:
                    politiques = politiques.filter(
                        site__organisation_id__in=organisation_ids,
                    )
                decisions = sum(
                    evaluer_pilotage_autonome(
                        politique.site_id,
                        force=options["once"],
                    )
                    is not None
                    for politique in politiques.iterator()
                )
            if options["once"]:
                self.stdout.write(
                    f"Mesures simulées générées : {nombre} ; "
                    f"mesures de présence simulées : {presences} ; "
                    f"décisions d'autonomie simulée : {decisions}"
                )
                return
            if nombre or decisions:
                self.stdout.write(
                    f"Mesures simulées générées : {nombre} ; "
                    f"mesures de présence simulées : {presences} ; "
                    f"décisions d'autonomie simulée : {decisions}"
                )
            time.sleep(intervalle)
