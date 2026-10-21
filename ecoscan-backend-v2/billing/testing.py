from billing.models import Plan
from billing.services import SubscriptionService


def grant_test_trial(organisation):
    plan, _ = Plan.objects.get_or_create(
        code="standard",
        defaults={
            "nom": "Standard",
            "description": "Plan standard EcoScan.",
            "prix_mensuel": "10000.00",
            "prix_annuel": "100000.00",
            "devise": "XOF",
            "actif": True,
        },
    )
    return SubscriptionService().demarrer_essai_gratuit(organisation, plan)
