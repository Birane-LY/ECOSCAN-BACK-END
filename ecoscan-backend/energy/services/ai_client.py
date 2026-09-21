"""
Client Django -> service FastAPI, dédié à l'analyse vision (photo de compteur
ou de facture prise via l'app mobile). Distinct de analysis/services/ai_client.py
(RAG/hypothèses) — portée différente, appelé depuis energy plutôt qu'analysis.
"""

import json
import logging
import mimetypes
import urllib.error
import urllib.request
import uuid
from django.conf import settings

logger = logging.getLogger(__name__)

AI_SERVICE_URL = getattr(settings, "AI_SERVICE_URL", "http://localhost:8001")
AI_SERVICE_INTERNAL_TOKEN = getattr(settings, "AI_SERVICE_INTERNAL_TOKEN", "change-me-internal-token")
AI_SERVICE_TIMEOUT = getattr(settings, "AI_SERVICE_TIMEOUT", 30.0)


def _post_multipart(url: str, field_name: str, filename: str, contenu: bytes, content_type: str) -> dict:
    boundary = uuid.uuid4().hex
    corps = []
    corps.append(f"--{boundary}".encode())
    corps.append(
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode()
    )
    corps.append(f"Content-Type: {content_type}".encode())
    corps.append(b"")
    corps.append(contenu)
    corps.append(f"--{boundary}--".encode())
    corps.append(b"")
    body = b"\r\n".join(corps)

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "X-Internal-Service-Token": AI_SERVICE_INTERNAL_TOKEN,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=AI_SERVICE_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def analyser_image(fichier_bytes: bytes, filename: str) -> dict:
    """Envoie une photo (compteur ou facture) au service vision. Ne lève
    jamais d'exception — une panne du service IA doit dégrader gracieusement
    vers la saisie manuelle, jamais bloquer l'utilisateur sur le terrain."""
    content_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    try:
        return _post_multipart(f"{AI_SERVICE_URL}/api/v1/image/analyze", "file", filename, fichier_bytes, content_type)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Service vision indisponible lors de l'analyse de %s : %s", filename, exc)
        return {"_error": f"Service d'analyse d'image indisponible : {exc}"}