import logging
import os
import smtplib

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
import httpx

logger = logging.getLogger(__name__)


def envoyer_email_activation(utilisateur, sujet, introduction):
    uid = urlsafe_base64_encode(force_bytes(utilisateur.pk))
    token = default_token_generator.make_token(utilisateur)
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3001").rstrip("/")
    lien_activation = f"{frontend_url}/?uid={uid}&token={token}"
    message = (
        f"Bonjour {utilisateur.nom},\n\n{introduction}\n"
        "Définissez votre mot de passe avec ce lien sécurisé :\n"
        f"{lien_activation}\n\nL'équipe EcoScan."
    )

    provider = os.getenv("EMAIL_PROVIDER", "brevo").strip().lower()
    if provider == "smtp":
        if settings.EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend":
            raise RuntimeError(
                "Le backend Django SMTP doit être activé avec EMAIL_BACKEND."
            )
        if not settings.EMAIL_HOST or not settings.DEFAULT_FROM_EMAIL:
            raise RuntimeError(
                "EMAIL_HOST et DEFAULT_FROM_EMAIL doivent être configurés pour SMTP."
            )
        if settings.DEFAULT_FROM_EMAIL == "noreply@monapp.com":
            raise RuntimeError("DEFAULT_FROM_EMAIL doit contenir l'adresse expéditrice EcoScan.")

        try:
            send_mail(
                sujet,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [utilisateur.email],
                fail_silently=False,
            )
        except (OSError, smtplib.SMTPException) as exc:
            logger.exception("Échec de l'envoi de l'e-mail via SMTP à %s", utilisateur.email)
            raise RuntimeError("Le serveur SMTP n’a pas pu accepter l’e-mail d’activation.") from exc
        return

    if provider != "brevo":
        raise RuntimeError("EMAIL_PROVIDER doit être configuré sur 'smtp' ou 'brevo'.")

    api_key = os.getenv("BREVO_API_KEY", "")
    sender_email = os.getenv("BREVO_SENDER_EMAIL", "")
    sender_name = os.getenv("BREVO_SENDER_NAME", "EcoScan")
    if not api_key or not sender_email:
        raise RuntimeError("BREVO_API_KEY et BREVO_SENDER_EMAIL doivent être configurés.")

    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            json={
                "sender": {"name": sender_name, "email": sender_email},
                "to": [{"email": utilisateur.email, "name": utilisateur.nom}],
                "subject": sujet,
                "textContent": message,
            },
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("Échec de l'envoi de l'e-mail via Brevo à %s", utilisateur.email)
        raise RuntimeError("Brevo n’a pas pu accepter l’e-mail d’activation.") from exc
