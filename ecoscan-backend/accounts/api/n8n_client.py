"""
Client Django -> n8n pour les automatisations opérationnelles.

n8n est accédé exclusivement via un compte SUPER_ADMIN de service dédié (voir
la commande `create_n8n_service_account`) — jamais un compte humain partagé.
Ce module gère le sens Django -> n8n (notifier un événement) ; le sens inverse
(n8n -> Django, ex. PATCH sur une organisation pour lever un défaut de
paiement) passe par l'API REST classique, authentifiée avec le JWT de ce
compte de service.

Même principe de dégradation gracieuse que analysis/api/ai_client.py : une
panne de n8n ne doit JAMAIS faire échouer l'action métier qui a déclenché la
notification (créer un onboarding doit réussir même si n8n est indisponible).
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

N8N_WEBHOOK_BASE_URL = getattr(settings, "N8N_WEBHOOK_BASE_URL", "")
N8N_WEBHOOK_TOKEN = getattr(settings, "N8N_WEBHOOK_TOKEN", "change-me-n8n-token")
N8N_WEBHOOK_TIMEOUT = getattr(settings, "N8N_WEBHOOK_TIMEOUT", 10.0)


def _post_event(chemin_webhook: str, payload: dict) -> bool:
    """POST best-effort vers un webhook n8n. Ne lève jamais d'exception."""
    if not N8N_WEBHOOK_BASE_URL:
        logger.warning(
            "N8N_WEBHOOK_BASE_URL non configurée — événement '%s' non envoyé.",
            chemin_webhook,
        )
        return False

    request = urllib.request.Request(
        f"{N8N_WEBHOOK_BASE_URL}{chemin_webhook}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-N8N-Webhook-Token": N8N_WEBHOOK_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=N8N_WEBHOOK_TIMEOUT) as response:
            response.read()
        return True
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("n8n indisponible pour l'événement '%s' : %s", chemin_webhook, exc)
        return False


def notifier_onboarding_cree(utilisateur, uid: str, token: str, lien_activation: str) -> bool:
    """Signale à n8n qu'un compte admin vient d'être créé via l'onboarding
    autonome, pour qu'il envoie l'email de connexion.

    Django ne construit ni n'envoie l'email lui-même — n8n reçoit tout ce
    qu'il faut (uid, token, lien déjà construit par commodité) pour composer
    et envoyer le message de son côté.
    """
    return _post_event("/onboarding-cree", {
        "utilisateur_id": str(utilisateur.pk),
        "email": utilisateur.email,
        "nom": utilisateur.nom,
        "uid": uid,
        "token": token,
        "lien_activation": lien_activation,
    })