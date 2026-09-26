"""
Client Django -> service FastAPI EcoScan AI/RAG.

Miroir côté Django du `django_client.py` côté FastAPI (qui va dans l'autre sens :
FastAPI -> Django pour le contexte métier temps réel). Les deux services
s'authentifient par jeton partagé et dégradent gracieusement si l'autre est
indisponible.

Politique d'indexation : on n'envoie à /internal/documents QUE du contenu déjà
validé par un humain ou par les règles métier déterministes — jamais du texte
OCR brut, jamais une hypothèse IA non confirmée, jamais une donnée
REVUE_REQUISE. C'est la responsabilité de l'appelant (memory_service,
DocumentEntrepriseViewSet.valider).
"""

import json
import logging
import urllib.error
import urllib.request
import uuid
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

AI_SERVICE_URL = getattr(settings, "AI_SERVICE_URL", "http://localhost:8001").rstrip("/")
AI_SERVICE_INTERNAL_TOKEN = getattr(settings, "AI_SERVICE_INTERNAL_TOKEN", "")
# La chaîne de secours FastAPI peut enchaîner plusieurs modèles (30 s chacun) :
# un délai de 15 s coupait la requête avant la réponse. 60 s par défaut.
AI_SERVICE_TIMEOUT = getattr(settings, "AI_SERVICE_TIMEOUT", 60.0)


_HEADERS = {"X-Internal-Service-Token": AI_SERVICE_INTERNAL_TOKEN}

if not AI_SERVICE_INTERNAL_TOKEN:
    logger.warning(
        "AI_SERVICE_INTERNAL_TOKEN n'est pas configuré : les appels au service IA "
        "sont désactivés jusqu'à sa configuration côté Django et FastAPI."
    )


def _decrire_erreur(exc: Exception) -> str:
    """Message lisible : pour une erreur HTTP, on remonte le code ET le détail
    renvoyé par FastAPI (401 = jeton, 500 = jeton non configuré, 422 = payload...)."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        return f"Service IA en erreur (HTTP {exc.code}) {detail}".strip()
    return f"Service IA indisponible : {exc}"


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=AI_SERVICE_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _appeler(url: str, payload: dict, contexte: str) -> dict:
    if not AI_SERVICE_INTERNAL_TOKEN:
        message = "AI_SERVICE_INTERNAL_TOKEN n'est pas configuré."
        logger.warning("%s : %s", contexte, message)
        return {"_error": message}

    try:
        return _post_json(url, payload)
    except (OSError, ValueError) as exc:  # URLError/HTTPError/timeout/JSON invalide
        message = _decrire_erreur(exc)
        logger.warning("%s : %s", contexte, message)
        return {"_error": message}


def indexer_document(
    organisation_id, document_id, texte: str, source: str, import_id=None, metadata: Optional[dict] = None
) -> dict:
    """Pousse un contenu déjà validé vers l'index RAG. Ne lève jamais d'exception :
    une panne du service IA ne doit jamais faire échouer l'action métier."""
    payload = {
        "event_id": str(uuid.uuid4()),
        "organisation_id": str(organisation_id),
        "document_id": str(document_id),
        "import_id": str(import_id) if import_id else None,
        "source": source,
        "texte": texte,
        "metadata": metadata or {},
    }
    return _appeler(f"{AI_SERVICE_URL}/internal/documents", payload, f"Indexation du document {document_id}")


def demander_hypothese(question: str, organisation_id) -> dict:
    """Interroge le service IA pour formuler une hypothèse de cause probable.

    Le texte retourné est une explication à faire valider par un humain, pas un
    objet structuré fiable (voir hypothesis.py : confiance=None)."""
    payload = {
        "question": question,
        "organisation_id": str(organisation_id),
        "limit": 5,
        "include_live_data": False,
    }
    return _appeler(f"{AI_SERVICE_URL}/internal/query", payload, "Demande d'hypothèse")


def interroger_assistant(
    question: str, organisation_id, include_live_data: bool = True, historique: Optional[list] = None
) -> dict:
    """Question libre depuis l'assistant conversationnel. `historique` (derniers
    échanges) permet à l'IA de comprendre « confirme cette action » ou « montre
    les sources » : sans lui, chaque message repartait de zéro."""
    payload = {
        "question": question,
        "organisation_id": str(organisation_id),
        "limit": 5,
        "include_live_data": include_live_data,
        "history": historique or [],
    }
    return _appeler(f"{AI_SERVICE_URL}/internal/query", payload, "Question assistant")
