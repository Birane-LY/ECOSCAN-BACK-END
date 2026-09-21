"""
Client Django -> service FastAPI EcoScan AI/RAG.

Miroir côté Django du `django_client.py` déjà écrit côté FastAPI (qui, lui, va
dans l'autre sens : FastAPI -> Django pour le contexte métier temps réel). Les
deux services s'appellent mutuellement via le même principe : jeton de service
partagé, dégradation gracieuse si l'autre service est indisponible.

Politique d'indexation (reprise du document RAG partagé plus tôt dans ce
projet) : on n'envoie à /internal/documents QUE du contenu déjà validé par un
humain ou par les règles métier déterministes — jamais du texte OCR brut, jamais
une hypothèse IA non confirmée, jamais une donnée REVUE_REQUISE. C'est la
responsabilité de l'appelant (memory_service, DocumentEntreprise.valider) de ne
transmettre à ce module QUE du contenu qui a déjà passé cette barrière.
"""

import logging
import uuid
from typing import Any, Optional

import json
import urllib.error
import urllib.request
from django.conf import settings

logger = logging.getLogger(__name__)

AI_SERVICE_URL = getattr(settings, "AI_SERVICE_URL", "http://localhost:8001")
AI_SERVICE_INTERNAL_TOKEN = getattr(settings, "AI_SERVICE_INTERNAL_TOKEN", "change-me-internal-token")
AI_SERVICE_TIMEOUT = getattr(settings, "AI_SERVICE_TIMEOUT", 15.0)

_HEADERS = {"X-Internal-Service-Token": AI_SERVICE_INTERNAL_TOKEN}

if AI_SERVICE_INTERNAL_TOKEN == "change-me-internal-token":
    logger.warning(
        "AI_SERVICE_INTERNAL_TOKEN n'est pas configuré (valeur par défaut utilisée) : "
        "le service IA va systématiquement répondre 401 tant que cette valeur ne sera "
        "pas exactement identique à INTERNAL_TOKEN côté service FastAPI."
    )


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=AI_SERVICE_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def indexer_document(
    organisation_id, document_id, texte: str, source: str, import_id=None, metadata: Optional[dict] = None
) -> dict:
    """Pousse un contenu déjà validé vers l'index RAG. Ne lève jamais d'exception :
    une panne du service IA ne doit jamais faire échouer l'action métier qui a
    déclenché l'indexation (mêmes principes que enregistrer_evenement côté audit
    et publier_import_termine côté energy->analysis)."""
    payload = {
        "event_id": str(uuid.uuid4()),
        "organisation_id": str(organisation_id),
        "document_id": str(document_id),
        "import_id": str(import_id) if import_id else None,
        "source": source,
        "texte": texte,
        "metadata": metadata or {},
    }
    try:
        return _post_json(f"{AI_SERVICE_URL}/internal/documents", payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Service IA indisponible lors de l'indexation de %s : %s", document_id, exc)
        return {"_error": f"Service IA indisponible : {exc}"}


def demander_hypothese(question: str, organisation_id) -> dict:
    """Interroge le service IA pour formuler une hypothèse de cause probable.

    Utilise /internal/query (généraliste, texte libre) plutôt qu'un endpoint
    dédié structuré : aucune garantie de format JSON strict n'existe côté LLM,
    donc le texte retourné doit être traité comme une explication à faire
    valider par un humain, pas comme un objet structuré fiable. Voir
    hypothesis_service.py pour comment ce texte est enveloppé dans une Hypothese
    avec confiance=None (jamais déduite automatiquement d'un texte libre).
    """
    payload = {
        "question": question,
        "organisation_id": str(organisation_id),
        "limit": 5,
        "include_live_data": False,
    }
    try:
        return _post_json(f"{AI_SERVICE_URL}/internal/query", payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Service IA indisponible lors de la demande d'hypothèse : %s", exc)
        return {"_error": f"Service IA indisponible : {exc}"}


def interroger_assistant(question: str, organisation_id, include_live_data: bool = True) -> dict:
    """Question libre de l'utilisateur depuis l'assistant conversationnel du front.

    Contrairement à demander_hypothese (scopé au contexte d'une anomalie précise),
    autorise l'accès aux données live de l'organisation pour répondre à des
    questions générales ("où est mon plus gros levier ?", "résume ma semaine").
    """
    payload = {
        "question": question,
        "organisation_id": str(organisation_id),
        "limit": 5,
        "include_live_data": include_live_data,
    }
    try:
        return _post_json(f"{AI_SERVICE_URL}/internal/query", payload)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Service IA indisponible lors d'une question assistant : %s", exc)
        return {"_error": f"Service IA indisponible : {exc}"}