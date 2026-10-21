import argparse
import logging
import os
import time as time_module
from datetime import datetime, time, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


def _parse_heure(value):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "L'heure doit être au format HH:MM (24 heures)."
        ) from exc


def prochaine_execution(maintenant, heure):
    heure_locale = timezone.localtime(maintenant)
    prochaine = timezone.make_aware(
        datetime.combine(heure_locale.date(), heure),
        timezone.get_current_timezone(),
    )
    if prochaine <= heure_locale:
        prochaine = timezone.make_aware(
            datetime.combine(heure_locale.date() + timedelta(days=1), heure),
            timezone.get_current_timezone(),
        )
    return prochaine


class Command(BaseCommand):
    help = "Planifie quotidiennement les analyses énergétiques et diagnostics EcoScan."

    def add_arguments(self, parser):
        parser.add_argument(
            "--heure",
            type=_parse_heure,
            default=_parse_heure(os.environ.get("ECOSCAN_TACHES_HEURE", "02:15")),
            help="Heure locale d'exécution au format HH:MM (défaut : 02:15).",
        )

    def handle(self, *args, **options):
        heure = options["heure"]
        prochaine = prochaine_execution(timezone.now(), heure)
        self.stdout.write(
            f"Prochaine analyse énergétique : "
            f"{timezone.localtime(prochaine).isoformat()}."
        )

        while True:
            attente = (prochaine - timezone.now()).total_seconds()
            if attente > 0:
                time_module.sleep(min(attente, 60))
                continue

            self.stdout.write("Démarrage des analyses énergétiques quotidiennes.")
            try:
                call_command("executer_taches_energetiques_quotidiennes")
            except Exception:
                logger.exception("Échec des analyses énergétiques quotidiennes.")

            prochaine = prochaine_execution(timezone.now(), heure)
            self.stdout.write(
                f"Prochaine analyse énergétique : "
                f"{timezone.localtime(prochaine).isoformat()}."
            )
