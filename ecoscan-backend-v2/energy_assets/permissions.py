import secrets

from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission


class JetonServiceInternePermission(BasePermission):
    """Authentifie les appels internes avec le jeton de service partagé."""

    def has_permission(self, request, view):
        attendu = getattr(settings, "DJANGO_INTERNAL_TOKEN", "") or ""
        recu = request.headers.get("X-Internal-Service-Token", "")
        if not attendu or not secrets.compare_digest(recu, attendu):
            raise AuthenticationFailed("Jeton de service invalide.")
        return True
