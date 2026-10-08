import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission

from organizations.models import UtilisateurOrganisation
from .services import BillingAccessService


class EstAbonnementActif(BasePermission):
    """Refuse les fonctionnalités métier sans essai ou abonnement non expiré."""

    message = "Un essai gratuit ou un abonnement actif est nécessaire pour accéder à cet espace."

    def has_permission(self, request, view):
        utilisateur = request.user
        return (
            utilisateur.is_authenticated
            and utilisateur.actif
            and BillingAccessService().organisations_avec_acces(utilisateur).exists()
        )


class EstSuperAdminEcoScan(BasePermission):
    """Les Plans sont gérés exclusivement par le SUPER_ADMIN de la plateforme,
    même si leur usage concerne les organisations — demande explicite : le
    catalogue tarifaire est une décision produit centrale, pas un objet métier
    par tenant, donc PAS soumis au filtrage multi-tenant habituel ni à
    l'exclusion habituelle du SUPER_ADMIN des objets métier."""

    message = "Seul le SUPER_ADMIN d'EcoScan peut gérer les plans tarifaires."

    def has_permission(self, request, view):
        return getattr(request.user, "role", None) == "SUPER_ADMIN"


class EstAdminDeLOrganisationAbonnee(BasePermission):
    """CORRECTIF : souscrire()/annuler() sur AbonnementViewSet n'avaient
    aucune vérification de rôle au-delà de IsAuthenticated — n'importe quel
    membre de l'organisation (CONSULTANT compris) pouvait déclencher un
    paiement ou annuler l'abonnement de toute l'organisation. Seul un
    ADMIN_ORGANISATION affilié à l'organisation de CET abonnement précis peut
    engager ou annuler une souscription."""

    message = "Seul un administrateur de l'organisation abonnée peut effectuer cette action."

    def has_object_permission(self, request, view, obj):
        if getattr(request.user, "role", None) != "ADMIN_ORGANISATION":
            return False
        return UtilisateurOrganisation.objects.filter(
            organisation=obj.organisation, utilisateur=request.user
        ).exists()


class EstAppelDeConfianceN8N(BasePermission):
    """Le callback de confirmation d'envoi d'une relance est appelé par le
    workflow n8n, pas par un utilisateur EcoScan — il n'y a donc pas de JWT à
    vérifier. La confiance vient d'un secret partagé transmis dans l'en-tête
    X-EcoScan-Secret (le même que celui envoyé à n8n dans le payload du
    déclenchement, voir tasks._envoyer_notification), comparé en temps
    constant pour éviter une attaque par mesure de temps."""

    message = "Secret d'appel invalide."

    def has_permission(self, request, view):
        secret_attendu = getattr(settings, "N8N_SHARED_SECRET", "")
        secret_recu = request.headers.get("X-EcoScan-Secret", "")
        if not secret_attendu or not secret_recu:
            return False
        return hmac.compare_digest(secret_recu, secret_attendu)