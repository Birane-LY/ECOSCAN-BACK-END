"""Démarre l'essai gratuit uniquement après l'approbation d'une demande."""

import logging

from django.db.models.signals import post_save, pre_save

logger = logging.getLogger(__name__)

PLAN_ESSAI_PAR_DEFAUT = "standard"


def memoriser_statut_precedent(sender, instance, **kwargs):
    if instance.pk:
        instance._statut_precedent = sender.objects.filter(pk=instance.pk).values_list("statut", flat=True).first()
    else:
        instance._statut_precedent = None


def demarrer_essai_apres_approbation(sender, instance, created, **kwargs):
    from organizations.models import Organisation

    if (
        created
        or getattr(instance, "_statut_precedent", None) != Organisation.Statut.EN_ATTENTE
        or instance.statut != Organisation.Statut.ACTIVE
    ):
        return

    from .models import Plan
    from .services import SubscriptionService

    plan_id_demande = instance.details_demande.get("plan_id")
    plan = (
        Plan.objects.filter(pk=plan_id_demande, actif=True).first()
        if plan_id_demande
        else Plan.objects.filter(code=PLAN_ESSAI_PAR_DEFAUT, actif=True).first()
    )
    if plan is None:
        logger.error(
            "Impossible de démarrer l'essai gratuit pour l'organisation %s : "
            "la formule demandée est absente ou inactive.",
            instance.id,
        )
        return

    SubscriptionService().demarrer_essai_gratuit(instance, plan)


def connecter_signaux():
    from organizations.models import Organisation
    pre_save.connect(
        memoriser_statut_precedent,
        sender=Organisation,
        dispatch_uid="billing_statut_precedent",
    )
    post_save.connect(
        demarrer_essai_apres_approbation,
        sender=Organisation,
        dispatch_uid="billing_essai_gratuit_apres_approbation",
    )