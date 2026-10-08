from django.core.management.base import BaseCommand

from billing.tasks import (
    envoyer_relances_dues,
    expirer_essais_termines,
    expirer_periodes_de_grace,
    traiter_renouvellements_dus,
)


class Command(BaseCommand):
    help = "Execute les taches periodiques de facturation EcoScan."

    def add_arguments(self, parser):
        parser.add_argument(
            "--renouvellements",
            action="store_true",
            help="Traite les renouvellements et les cycles impayes.",
        )
        parser.add_argument(
            "--relances",
            action="store_true",
            help="Declenche les relances arrivees a echeance.",
        )
        parser.add_argument(
            "--expiration",
            action="store_true",
            help="Expire les essais et periodes de grace depasses.",
        )

    def handle(self, *args, **options):
        executer_tout = not any(
            options[option] for option in ("renouvellements", "relances", "expiration")
        )
        resultat = {}

        if executer_tout or options["expiration"]:
            resultat["essais_expires"] = expirer_essais_termines()
            resultat["graces_expirees"] = expirer_periodes_de_grace()
        if executer_tout or options["renouvellements"]:
            resultat.update(traiter_renouvellements_dus())
        if executer_tout or options["relances"]:
            resultat["relances_declenchees"] = envoyer_relances_dues()

        for nom, valeur in resultat.items():
            self.stdout.write(f"{nom}: {valeur}")
