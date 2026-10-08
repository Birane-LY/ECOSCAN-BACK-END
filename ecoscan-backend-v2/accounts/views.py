from rest_framework import status, viewsets, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.decorators import action
from django.db import transaction
import logging

from .models import Utilisateur, PreferencesUtilisateur
from audit.models import JournalAudit
from audit.services import enregistrer_evenement
from organizations.models import Organisation, UtilisateurOrganisation
from .services import envoyer_email_activation
from .serializers import (
    OnboardingAdminOrganisationSerializer,
    UtilisateurSerializer,
    InvitationCreateSerializer,
    FinaliserInscriptionSerializer,
    ConnexionSerializer,
    PreferencesUtilisateurSerializer,
    ChangerMotDePasseSerializer,
)

logger = logging.getLogger(__name__)


class OnboardingAdminOrganisationView(APIView):
    """Crée un compte en attente et envoie le lien de démarrage d'essai."""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'invitation_validation'

    def post(self, request):
        serializer = OnboardingAdminOrganisationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = serializer.save()
        organisation = Organisation.objects.get(membres__utilisateur=utilisateur)

        try:
            envoyer_email_activation(
                utilisateur,
                sujet="Activez votre essai gratuit EcoScan",
                introduction=(
                    "Votre espace EcoScan est prêt. Activez votre adresse e-mail "
                    "pour démarrer votre essai gratuit de 14 jours."
                ),
            )
        except Exception:
            logger.exception(
                "Impossible d'envoyer le lien d'activation à %s ; annulation de l'inscription.",
                utilisateur.email,
            )
            with transaction.atomic():
                organisation.delete()
                utilisateur.delete()
            return Response(
                {"detail": "L’e-mail d’activation n’a pas pu être envoyé. Veuillez réessayer."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "message": "Votre compte est créé. Activez votre adresse e-mail pour démarrer l’essai gratuit.",
                "plan": organisation.details_demande.get("plan_nom"),
                "duree_essai_jours": 14,
            },
            status=status.HTTP_201_CREATED
        )


class FinaliserInscriptionView(APIView):
    """Endpoint public permettant à un utilisateur invité d'activer définitivement son compte.
    
    Reçoit l'uid, le token de sécurité et le mot de passe choisi par l'utilisateur pour l'activer.
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'invitation_validation'

    def post(self, request):
        serializer = FinaliserInscriptionSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        data = {
            "message": "Votre compte a été configuré et activé avec succès !",
            "essai_demarre": bool(serializer.context.get("essai_demarre")),
        }
        if serializer.context.get("essai_demarre"):
            data["message"] = "Votre compte est activé et votre essai gratuit de 14 jours a démarré."
            data["duree_essai_jours"] = 14
        return Response(data, status=status.HTTP_200_OK)


class EstAdminOuDevOuSoiMeme(permissions.BasePermission):
    """Permission personnalisée contrôlant l'accès aux profils utilisateurs.
    
    - Les Super Admins et Admins d'organisation actifs ont un accès global.
    - Les utilisateurs standards peuvent uniquement accéder à leurs propres requêtes (GET/PUT/PATCH sur leur ID).
    """

    def has_permission(self, request, view):
        # L'utilisateur doit être connecté et actif pour faire la moindre action
        if not request.user or not request.user.is_authenticated or not request.user.actif:
            return False
        
        # Pour lister tous les comptes (GET /api/users/) ou inviter (POST), il faut être Admin 
        if view.action in ["list", "create"]:
            return request.user.role in [
                Utilisateur.Role.ADMIN_ORGANISATION,
                Utilisateur.Role.SUPER_ADMIN,
            ]
        # Pour les actions unitaires (GET détaillé, PUT, PATCH, DELETE sur un ID), on autorise tout le monde.
        # Le filtrage fin se fera dans `has_object_permission` et `get_queryset`.
        return True

    def has_object_permission(self, request, view, obj):
        acteur = request.user
        
        # Un utilisateur a toujours le droit de consulter ou modifier son PROPRE profil
        if acteur.id == obj.id:
            return True
              
        # Les Admins d'organisation peuvent gérer les objets de leur périmètre
        if acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
            organisations = Organisation.objects.filter(membres__utilisateur=acteur)
            return (
                acteur.actif
                and obj.role in [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
                and UtilisateurOrganisation.objects.filter(
                    organisation__in=organisations,
                    utilisateur=obj,
                ).exists()
            )

        if acteur.role == Utilisateur.Role.SUPER_ADMIN:
            return obj.role == Utilisateur.Role.SUPER_ADMIN
            
        return False


class UtilisateurViewSet(viewsets.ModelViewSet):
    """ViewSet pour l'affichage, l'invitation, la modification et la suppression des membres.
    
    - Le Super Admin gère son équipe de dev (SUPER_ADMIN).
    - L'Admin d'organisation gère les Utilisateurs et Consultants de sa structure.
    - Tout utilisateur connecté peut modifier ses propres informations personnelles.
    """
    queryset = Utilisateur.objects.all().order_by("email")
    permission_classes = [EstAdminOuDevOuSoiMeme]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        data = serializer.data
        data["activation_email_sent"] = serializer.context.get("activation_email_sent", False)
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)

    @action(detail=False, methods=['get'], url_path='me')
    def me(self, request):
        """Renvoie les données du profil de l'utilisateur actuellement connecté."""
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)
    
    def get_serializer_class(self):
        """Bascule sur le sérialiseur d'invitation lors d'une création (POST)."""
        if self.action == "create":
            return InvitationCreateSerializer
        return UtilisateurSerializer

    def get_queryset(self):
        """Filtre les données pour cloisonner l'affichage sur les écrans selon l'acteur connecté."""
        acteur = self.request.user

        # L'équipe de dev (Super Admin) ne voit que les profils de son niveau
        if acteur.role == Utilisateur.Role.SUPER_ADMIN:
            return self.queryset.filter(role=Utilisateur.Role.SUPER_ADMIN)

        # L'Admin d'organisation voit et gère uniquement les utilisateurs et consultants
        if acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
            organisations = Organisation.objects.filter(membres__utilisateur=acteur)
            return self.queryset.filter(
                role__in=[Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT],
                organisations_membres__organisation__in=organisations,
            )

        # Un utilisateur standard (or consultant) ne voit QUE son propre profil en base
        return self.queryset.filter(id=acteur.id)

    @action(detail=True, methods=["patch"], url_path="acces")
    def acces(self, request, pk=None):
        """Active ou suspend l'accès d'un compte géré par l'administrateur."""
        utilisateur = self.get_object()
        if utilisateur.id == request.user.id:
            raise PermissionDenied("Vous ne pouvez pas modifier votre propre accès.")
        if request.user.role not in (Utilisateur.Role.ADMIN_ORGANISATION, Utilisateur.Role.SUPER_ADMIN):
            raise PermissionDenied("Seul un administrateur peut gérer les accès.")
        if "actif" not in request.data or not isinstance(request.data["actif"], bool):
            return Response(
                {"actif": ["Ce champ booléen est obligatoire."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        nouvel_etat = request.data["actif"]
        if request.user.role == Utilisateur.Role.ADMIN_ORGANISATION and not request.user.actif:
            raise PermissionDenied("Votre compte doit être actif pour gérer les accès.")
        if request.user.role == Utilisateur.Role.SUPER_ADMIN:
            if utilisateur.role != Utilisateur.Role.SUPER_ADMIN:
                raise PermissionDenied("Vous ne pouvez gérer que les accès de l'équipe EcoScan.")
            if utilisateur.actif and not nouvel_etat and not Utilisateur.objects.filter(
                role=Utilisateur.Role.SUPER_ADMIN,
                actif=True,
            ).exclude(pk=utilisateur.pk).exists():
                raise PermissionDenied("Le dernier Super Administrateur actif ne peut pas être désactivé.")

        etat_precedent = utilisateur.actif
        if etat_precedent != nouvel_etat:
            utilisateur.actif = nouvel_etat
            utilisateur.save(update_fields=("actif",))
            organisation = Organisation.objects.filter(
                membres__utilisateur=utilisateur
            ).first() if request.user.role == Utilisateur.Role.ADMIN_ORGANISATION else None
            enregistrer_evenement(
                action="ACTIVER_ACCES" if nouvel_etat else "DESACTIVER_ACCES",
                ressource="Utilisateur",
                identifiant_ressource=utilisateur.pk,
                utilisateur=request.user,
                organisation=organisation,
                details={
                    "utilisateur_nom": utilisateur.nom,
                    "utilisateur_email": utilisateur.email,
                    "etat_precedent": etat_precedent,
                    "etat": nouvel_etat,
                },
                request=request,
            )
        return Response(self.get_serializer(utilisateur).data)

    def perform_destroy(self, instance):
        """Sécurise la suppression d'un compte pour empêcher les débordements de rôles."""
        acteur = self.request.user
        if instance.pk == acteur.pk:
            raise PermissionDenied("Vous ne pouvez pas supprimer votre propre compte.")

        # Sécurité pour empêcher la suppression hors périmètre
        if acteur.role == Utilisateur.Role.SUPER_ADMIN and instance.role != Utilisateur.Role.SUPER_ADMIN:
            raise PermissionDenied("Vous ne pouvez supprimer que les membres de votre équipe de développement.")

        if acteur.role == Utilisateur.Role.SUPER_ADMIN and instance.actif and not Utilisateur.objects.filter(
            role=Utilisateur.Role.SUPER_ADMIN,
            actif=True,
        ).exclude(pk=instance.pk).exists():
            raise PermissionDenied("Le dernier Super Administrateur actif ne peut pas être supprimé.")
            
        if acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
            organisations = Organisation.objects.filter(membres__utilisateur=acteur)
            roles_geres = [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
            if not acteur.actif or instance.role not in roles_geres or not UtilisateurOrganisation.objects.filter(
                organisation__in=organisations,
                utilisateur=instance,
            ).exists():
                raise PermissionDenied("Vous ne pouvez supprimer que des Utilisateurs ou des Consultants.")

        organisation = Organisation.objects.filter(
            membres__utilisateur=instance
        ).first() if acteur.role == Utilisateur.Role.ADMIN_ORGANISATION else None
        enregistrer_evenement(
            action="SUPPRIMER_UTILISATEUR",
            ressource="Utilisateur",
            identifiant_ressource=instance.pk,
            utilisateur=acteur,
            organisation=organisation,
            details={
                "utilisateur_nom": instance.nom,
                "utilisateur_email": instance.email,
                "utilisateur_role": instance.role,
            },
            request=self.request,
        )
        instance.delete()


class ConnexionView(TokenObtainPairView):
    """Vue de connexion utilisant un token enrichi avec le rôle de l'utilisateur."""
    serializer_class = ConnexionSerializer

class MesPreferencesView(APIView):
    """Get/patch des préférences du user connecté — jamais celles d'un autre,
    donc pas besoin de pk dans l'URL."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        preferences, _ = PreferencesUtilisateur.objects.get_or_create(utilisateur=request.user)
        return Response(PreferencesUtilisateurSerializer(preferences).data)

    def patch(self, request):
        preferences, _ = PreferencesUtilisateur.objects.get_or_create(utilisateur=request.user)
        serializer = PreferencesUtilisateurSerializer(preferences, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

class ChangerMotDePasseView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invitation_validation"  # même limite de fréquence que les flux sensibles existants

    def post(self, request):
        serializer = ChangerMotDePasseSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"message": "Mot de passe modifié avec succès."}, status=status.HTTP_200_OK)