from django.core.management import call_command
from django.core.management.base import BaseCommand

from analysis.services.alertes_energetiques import executer_alertes_energetiques

class Command(BaseCommand):
    help = "Exécute les analyses quotidiennes des compteurs, capteurs et actions énergétiques."

    def handle(self, *args, **options):
        self.stdout.write("Analyse quotidienne des compteurs, factures et rituels…")
        call_command("analyser_compteurs")

        self.stdout.write("Analyse quotidienne des anomalies de télémétrie…")
        call_command("analyser_anomalies_telemetrie")

        self.stdout.write("Mesure des impacts énergétiques des actions terminées…")
        call_command("mesurer_impacts_telemetrie")

        self.stdout.write("Analyse des consommations en zones inoccupées…")
        call_command("analyser_occupation")

        self.stdout.write("Évaluation des alertes Woyofal, factures et objectifs…")
        alertes = executer_alertes_energetiques()
        self.stdout.write(f"Résultat des alertes énergétiques : {alertes}")

        self.stdout.write(self.style.SUCCESS("Traitements énergétiques quotidiens terminés."))
