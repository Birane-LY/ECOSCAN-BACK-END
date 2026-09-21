"""
Point d'entrée unique pour écrire dans le journal d'audit.

Sans ce module, chaque app qui veut tracer un événement réinventerait sa propre
façon d'extraire l'IP, de gérer les erreurs d'écriture, de nommer les actions —
avec le risque que la moitié du code s'en dispense "pour plus tard". En pratique,
`enregistrer_evenement` doit être l'unique façon d'écrire dans JournalAudit dans
tout le projet (energy, analysis, organizations...).
"""

import logging
from typing import Any, Optional

from .models import JournalAudit

logger = logging.getLogger(__name__)


def extraire_adresse_ip(request) -> Optional[str]:
    """Extrait l'adresse IP réelle du client, en tenant compte d'un éventuel proxy
    inverse (X-Forwarded-For) — sans ça, en déploiement derrière un reverse proxy
    ou un load balancer, adresse_ip contiendrait systématiquement l'IP du proxy,
    pas celle du client, ce qui rend le champ inutile pour l'investigation."""
    if request is None:
        return None
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        # Le premier maillon de la liste est le client d'origine ; les suivants
        # sont les proxys intermédiaires.
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def enregistrer_evenement(
    action: str,
    ressource: str,
    *,
    utilisateur=None,
    organisation=None,
    identifiant_ressource: str = "",
    resultat: str = JournalAudit.Resultat.SUCCES,
    details: Optional[dict] = None,
    request=None,
) -> Optional[JournalAudit]:
    """Enregistre un événement d'audit. Ne lève JAMAIS d'exception vers l'appelant :
    une panne d'écriture d'audit (ex. base saturée) ne doit jamais faire échouer
    l'action métier elle-même — elle est seulement loguée côté serveur pour
    investigation, exactement comme la publication automatique energy -> analysis
    plus tôt dans ce projet.

    `request`, si fourni, sert uniquement à extraire l'adresse IP — jamais à
    déduire utilisateur/organisation implicitement, pour que l'appelant reste
    explicite sur QUI et QUOI il trace (moins d'erreurs silencieuses qu'une
    déduction automatique qui se tromperait sur un cas limite).
    """
    try:
        return JournalAudit.objects.create(
            utilisateur=utilisateur,
            organisation=organisation,
            action=action,
            ressource=ressource,
            identifiant_ressource=str(identifiant_ressource) if identifiant_ressource else "",
            resultat=resultat,
            details=details or {},
            adresse_ip=extraire_adresse_ip(request),
        )
    except Exception:
        logger.exception(
            "Échec de l'écriture d'audit (action=%s, ressource=%s, identifiant=%s) — "
            "l'action métier elle-même n'est PAS affectée par cet échec.",
            action, ressource, identifiant_ressource,
        )
        return None