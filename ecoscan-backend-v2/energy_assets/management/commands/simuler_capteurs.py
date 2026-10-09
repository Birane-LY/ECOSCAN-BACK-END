"""Commande périodique d'émission de télémétrie simulée."""

import math
import time

from django.core.management.base import BaseCommand, CommandError

from energy_assets.simulation import generer_mesures_capteurs_simules


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

    def handle(self, *args, **options):
        intervalle = options["interval"]
        if not math.isfinite(intervalle) or intervalle <= 0:
            raise CommandError("L'intervalle de vérification doit être positif.")

        while True:
            nombre = generer_mesures_capteurs_simules()
            if options["once"]:
                self.stdout.write(f"Mesures simulées générées : {nombre}")
                return
            if nombre:
                self.stdout.write(f"Mesures simulées générées : {nombre}")
            time.sleep(intervalle)
