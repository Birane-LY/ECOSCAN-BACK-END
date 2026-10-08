"""Notifications n8n relatives aux anomalies de consommation."""

import logging

from django.conf import settings

from accounts.api.n8n_client import _post_event
from analysis.models import Anomalie

logger = logging.getLogger(__name__)


def notifier_anomalie_detectee(anomalie: Anomalie, site) -> bool:
    """Transmet à n8n une anomalie nouvellement détectée pour les admins du site."""
    organisation = site.organisation
    emails_admin = list(
        organisation.membres.filter(
            utilisateur__role="ADMIN_ORGANISATION",
            utilisateur__actif=True,
        )
        .order_by("utilisateur__email")
        .values_list("utilisateur__email", flat=True)
        .distinct()
    )
    if not emails_admin:
        logger.warning(
            "Aucun administrateur actif pour notifier l'anomalie %s.",
            anomalie.id,
        )
        return False

    backend_base_url = getattr(
        settings,
        "BACKEND_BASE_URL",
        "http://127.0.0.1:8000",
    ).rstrip("/")
    payload = {
        "anomalie": {
            "id": str(anomalie.id),
            "type": anomalie.type,
            "severite": anomalie.severite,
            "statut": anomalie.statut,
            "valeur_observee": str(anomalie.valeur_observee),
            "valeur_attendue": str(anomalie.valeur_attendue),
            "ecart_pourcentage": str(anomalie.ecart_pourcentage),
            "date_detection": anomalie.date_detection.isoformat(),
        },
        "site": {
            "id": str(site.id),
            "nom": site.nom,
        },
        "organisation": {
            "id": str(organisation.id),
            "nom": organisation.nom,
            "emails_admin": emails_admin,
        },
        "url_api_anomalies": (
            f"{backend_base_url}/api/energy-assets/sites/{site.id}/anomalies/"
        ),
    }
    return _post_event("/anomalie-detectee", payload)
