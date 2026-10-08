"""
Client Django -> service FastAPI pour l'analyse vision et la transcription audio.
Distinct de analysis/services/ai_client.py (RAG/hypothèses) — portée différente,
appelé depuis energy plutôt qu'analysis.
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
AI_SERVICE_INTERNAL_TOKEN = getattr(settings, "AI_SERVICE_INTERNAL_TOKEN", "")
AI_SERVICE_TIMEOUT = getattr(settings, "AI_SERVICE_TIMEOUT", 30.0)


def _post_multipart(
    url: str,
    field_name: str,
    filename: str,
    contenu: bytes,
    content_type: str,
    form_fields: dict | None = None,
) -> dict:
    boundary = uuid.uuid4().hex
    corps = []
    corps.append(f"--{boundary}".encode())
    corps.append(
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode()
    )
    corps.append(f"Content-Type: {content_type}".encode())
    corps.append(b"")
    corps.append(contenu)
    for nom, valeur in (form_fields or {}).items():
        corps.append(f"--{boundary}".encode())
        corps.append(f'Content-Disposition: form-data; name="{nom}"'.encode())
        corps.append(b"")
        corps.append(str(valeur).encode())
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
    if not AI_SERVICE_INTERNAL_TOKEN:
        logger.warning("AI_SERVICE_INTERNAL_TOKEN n'est pas configuré : analyse d'image indisponible.")
        return {"_error": "Le service d'analyse d'image n'est pas configuré."}

    content_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    try:
        return _post_multipart(f"{AI_SERVICE_URL}/api/v1/image/analyze", "file", filename, fichier_bytes, content_type)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Service vision indisponible lors de l'analyse de %s : %s", filename, exc)
        return {"_error": f"Service d'analyse d'image indisponible : {exc}"}


def transcrire_audio(fichier_bytes: bytes, filename: str, language: str = "fr") -> dict:
    """Envoie un enregistrement vocal au service IA et retourne sa transcription."""
    if not AI_SERVICE_INTERNAL_TOKEN:
        logger.error("AI_SERVICE_INTERNAL_TOKEN n'est pas configuré : transcription audio indisponible.")
        return {"_error": "Le service de transcription n'est pas configuré."}

    content_type = mimetypes.guess_type(filename)[0] or "audio/webm"
    try:
        return _post_multipart(
            f"{AI_SERVICE_URL}/api/v1/audio/transcribe",
            "file",
            filename,
            fichier_bytes,
            content_type,
            form_fields={"language": language},
        )
    except urllib.error.HTTPError as exc:
        try:
            contenu = json.loads(exc.read().decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            contenu = {}
        detail = contenu.get("detail", "")
        logger.warning("Le service de transcription a répondu HTTP %s.", exc.code)
        if exc.code == 401:
            return {
                "_error": (
                    "L’authentification entre Django et le service IA a échoué. "
                    "Redémarrez les deux serveurs après vérification de leurs jetons de service."
                )
            }
        if exc.code == 422:
            return {
                "_error": str(detail) or (
                    "Aucune parole n’a été reconnue. Parlez distinctement et enregistrez "
                    "un bilan plus long avant de réessayer."
                )
            }
        if exc.code == 400:
            return {"_error": str(detail) or "Le fichier audio est vide ou son format n’est pas pris en charge."}
        return {"_error": "Le service de transcription a refusé l’enregistrement. Réessayez dans quelques instants."}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.error("Service de transcription indisponible pour %s : %s", filename, exc)
        return {"_error": "Service de transcription indisponible. Réessayez dans quelques instants."}