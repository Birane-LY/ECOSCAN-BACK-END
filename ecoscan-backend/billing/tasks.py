"""
Tâches périodiques de facturation.

Je n'ai pas vu de configuration Celery dans ce projet — ces fonctions sont
écrites comme des fonctions Python simples, appelables aussi bien par
`@shared_task` (Celery Beat) que par une commande `manage.py` lancée par cron,
selon ce qui est déjà en place chez toi. Si Celery est configuré, il suffit
d'ajouter le décorateur ; sinon, une commande de management + cron système
fonctionne identiquement.

CADENCE RECOMMANDÉE (cron ou Celery Beat) :
    */15  *  *  *  *   expirer_periodes_de_grace + envoyer_relances_dues
    */15  *  *  *  *   expirer_essais_termines
    0     3  *  *  *   traiter_renouvellements_dus   (une fois par nuit suffit)
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

import httpx

from .models import Abonnement, Relance
from .services import RenewalService, SubscriptionService

logger = logging.getLogger(__name__)


def expirer_periodes_de_grace() -> int:
    """Suspend les abonnements dont la période de grâce est dépassée sans
    régularisation. À exécuter plusieurs fois par jour."""
    service = SubscriptionService()
    candidats = Abonnement.objects.filter(
        statut=Abonnement.Statut.GRACE_PERIOD,
        fin_periode__lt=timezone.now(),
    )
    compte = 0
    for abonnement in candidats:
        service.suspendre(abonnement)
        compte += 1
    if compte:
        logger.info("%d abonnement(s) suspendu(s) après expiration de la période de grâce.", compte)
    return compte


def expirer_essais_termines() -> int:
    """CORRECTIF (voir services.SubscriptionService.expirer_essai) : sans
    cette tâche, un essai gratuit dont la période est dépassée sans
    souscription payante reste TRIALING indéfiniment et conserve un accès
    complet et gratuit. À exécuter plusieurs fois par jour, comme
    expirer_periodes_de_grace()."""
    service = SubscriptionService()
    candidats = Abonnement.objects.filter(
        statut=Abonnement.Statut.TRIALING,
        fin_periode__lt=timezone.now(),
    )
    compte = 0
    for abonnement in candidats:
        service.expirer_essai(abonnement)
        compte += 1
    if compte:
        logger.info("%d essai(s) gratuit(s) expiré(s) sans souscription payante.", compte)
    return compte


def traiter_renouvellements_dus() -> dict:
    """CORRECTIF (voir services.RenewalService) : génère les factures des
    cycles ACTIVE arrivant à échéance, puis bascule en PAST_DUE + démarre les
    relances ceux dont le cycle est déjà expiré sans paiement. À exécuter une
    fois par nuit (les factures sont générées quelques jours à l'avance, pas
    besoin d'une cadence plus fine)."""
    service = RenewalService()
    factures_generees = service.generer_prochaines_factures()
    abonnements_impayes = service.marquer_impayes_les_cycles_expires()
    resultat = {"factures_generees": len(factures_generees), "abonnements_passes_en_impaye": abonnements_impayes}
    logger.info("Renouvellements traités : %s", resultat)
    return resultat


def envoyer_relances_dues() -> int:
    """Envoie les relances planifiées et arrivées à échéance. Verrouillage
    (select_for_update) pour éviter un double envoi si cette tâche tourne en
    parallèle sur plusieurs workers."""
    relances_dues = Relance.objects.filter(statut=Relance.Statut.PENDING, planifiee_le__lte=timezone.now())
    compte = 0
    for relance_id in relances_dues.values_list("id", flat=True):
        if _envoyer_une_relance(relance_id):
            compte += 1
    return compte


def _envoyer_une_relance(relance_id) -> bool:
    with transaction.atomic():
        relance = Relance.objects.select_for_update().get(id=relance_id)

        if relance.statut != Relance.Statut.PENDING:
            return False  # déjà traitée par un autre worker entre-temps

        if relance.facture.statut == "PAID":
            relance.statut = Relance.Statut.CANCELED
            relance.save(update_fields=("statut",))
            return False

        relance.statut = Relance.Statut.PROCESSING
        relance.save(update_fields=("statut",))

    try:
        _envoyer_notification(relance)
        # L'envoi effectif est délégué à n8n (voir _envoyer_notification) : le
        # statut PROCESSING est le statut final ici ; n8n rappelle
        # /billing/relances/<id>/callback/ pour le faire passer à SENT/FAILED
        # une fois l'envoi réellement effectué (voir views.RelanceCallbackView).
        return True
    except Exception:
        logger.exception("Échec du déclenchement de la relance %s vers n8n.", relance_id)
        Relance.objects.filter(id=relance_id).update(statut=Relance.Statut.FAILED)
        return False


def _envoyer_notification(relance: Relance) -> None:
    """Déclenche l'envoi réel via le workflow n8n dédié (voir la conception du
    workflow livrée séparément) : POST du contexte de la relance vers le
    webhook n8n. n8n choisit le canal réel (email/SMS/WhatsApp/in-app) et
    rappelle Django en asynchrone pour confirmer l'envoi — cette fonction ne
    fait que déclencher, elle n'attend pas la confirmation finale."""
    url = getattr(settings, "N8N_RELANCE_WEBHOOK_URL", "")
    secret = getattr(settings, "N8N_SHARED_SECRET", "")
    if not url:
        raise RuntimeError(
            "N8N_RELANCE_WEBHOOK_URL n'est pas configuré (settings) — impossible de déclencher l'envoi de la relance."
        )

    facture = relance.facture
    organisation = facture.organisation
    payload = {
        "relance_id": str(relance.id),
        "niveau": relance.niveau,
        "canal": relance.canal,
        "facture": {
            "id": str(facture.id),
            "numero": facture.numero,
            "montant_total": str(facture.montant_total),
            "devise": facture.devise,
            "date_echeance": facture.date_echeance.isoformat(),
            "statut": facture.statut,
        },
        "organisation": {
            "id": str(organisation.id),
            "nom": organisation.nom,
            "emails_admin": list(
                organisation.membres.filter(utilisateur__role="ADMIN_ORGANISATION")
                .values_list("utilisateur__email", flat=True)
            ),
        },
        "callback_url": f"{getattr(settings, 'BACKEND_BASE_URL', 'http://127.0.0.1:8000')}/api/paiements/relances/{relance.id}/callback/",
    }
    headers = {"X-EcoScan-Secret": secret, "Content-Type": "application/json"}

    with httpx.Client(timeout=10.0) as client:
        r = client.post(url, json=payload, headers=headers)
        r.raise_for_status()