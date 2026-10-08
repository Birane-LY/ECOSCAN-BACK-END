from django.core.management.base import BaseCommand

from billing.models import Plan


class Command(BaseCommand):
    help = "Cree ou met a jour le plan standard utilise par l'essai gratuit."

    def handle(self, *args, **options):
        plan, created = Plan.objects.update_or_create(
            code="standard",
            defaults={
                "nom": "Standard",
                "description": "Plan standard EcoScan.",
                "prix_mensuel": "10000",
                "prix_annuel": "100000",
                "devise": "XOF",
                "actif": True,
            },
        )
        action = "cree" if created else "mis a jour"
        self.stdout.write(self.style.SUCCESS(f"Plan standard {action}: {plan.id}"))
