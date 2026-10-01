"""
Lance la chaîne d'analyse automatisable pour toutes les organisations (compteurs à index,
compteurs prépayés Woyofal, factures) puis rattrape les hypothèses manquantes.

    python manage.py analyser_compteurs              # aujourd'hui et hier
    python manage.py analyser_compteurs --jours 7    # rattrapage sur 7 jours

À planifier chaque nuit (cron ou Celery beat). Avant cette commande, rien
n'appelait analyser_compteur : aucune anomalie n'était jamais produite à partir
des relevés fréquents.
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from organizations.models import Compteur, Organisation
from analysis.services.factures import recalculer_variations_factures
from analysis.services.orchestrator import analyser_compteur, regenerer_hypotheses_manquantes
from analysis.services.woyofal_rituel import analyser_woyofal_rituel

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Analyse les compteurs (variation vs baseline) et les factures (variation vs facture précédente)."

    def add_arguments(self, parser):
        parser.add_argument("--jours", type=int, default=2, help="Nombre de jours à analyser en remontant depuis aujourd'hui.")

    def handle(self, *args, **options):
        maintenant = timezone.now()
        for organisation in Organisation.objects.all():
            nb_factures = recalculer_variations_factures(organisation)
            self.stdout.write(f"[{organisation}] anomalies sur factures : {nb_factures}")

            for compteur in Compteur.objects.filter(site__organisation=organisation):
                for decalage in range(options["jours"]):
                    jour = maintenant - timedelta(days=decalage)
                    try:
                        res = analyser_compteur(organisation, compteur, jour)
                    except Exception as exc:  # index incohérent, etc. : on continue
                        logger.exception("Analyse impossible pour %s.", compteur)
                        self.stderr.write(f"  {compteur} {jour:%Y-%m-%d} : erreur {type(exc).__name__}")
                        continue
                    self.stdout.write(f"  {compteur} {jour:%Y-%m-%d} : {res['statut']}")

            for decalage in range(options["jours"]):
                jour = timezone.localdate(maintenant) - timedelta(days=decalage)
                try:
                    res = analyser_woyofal_rituel(str(organisation.id), jour.isoformat())
                except Exception as exc:
                    logger.exception("Analyse Woyofal rituelle impossible pour %s.", organisation)
                    self.stderr.write(f"  Woyofal rituel {jour:%Y-%m-%d} : erreur {type(exc).__name__}")
                    continue
                self.stdout.write(f"  Woyofal rituel {jour:%Y-%m-%d} : {res['statut']} — {res.get('detail', '')}")

            # Rattrape les hypothèses des anomalies détectées pendant une panne du service IA
            self.stdout.write(f"[{organisation}] hypothèses générées en rattrapage : {regenerer_hypotheses_manquantes(organisation)}")
