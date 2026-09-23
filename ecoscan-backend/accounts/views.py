from rest_framework import status, viewsets, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.decorators import action

from .models import Utilisateur, PreferencesUtilisateur
from .serializers import (
    OnboardingAdminOrganisationSerializer,
    UtilisateurSerializer,
    InvitationCreateSerializer,
    FinaliserInscriptionSerializer,
    ConnexionSerializer,
    PreferencesUtilisateurSerializer,
    ChangerMotDePasseSerializer,
)


class OnboardingAdminOrganisationView(APIView):
    """Endpoint public permettant la demande d'inscription autonome d'un Admin d'organisation.
    
    Le compte créé reste inactif (actif=False) jusqu'à sa validation manuelle 
    par l'équipe de développement (Super Admin).
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'invitation_validation'

    def post(self, request):
        serializer = OnboardingAdminOrganisationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"message": "Votre demande d'onboarding a été enregistrée. Elle est en attente de validation."},
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
        return Response(
            {"message": "Votre compte a été configuré et activé avec succès !"},
            status=status.HTTP_200_OK
        )


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
            return request.user.role == Utilisateur.Role.ADMIN_ORGANISATION
            
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
            return obj.role in [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
            
        return False


class UtilisateurViewSet(viewsets.ModelViewSet):
    """ViewSet pour l'affichage, l'invitation, la modification et la suppression des membres.
    
    - Le Super Admin gère son équipe de dev (SUPER_ADMIN).
    - L'Admin d'organisation gère les Utilisateurs et Consultants de sa structure.
    - Tout utilisateur connecté peut modifier ses propres informations personnelles.
    """
    queryset = Utilisateur.objects.all().order_by("email")
    permission_classes = [EstAdminOuDevOuSoiMeme]

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
            return self.queryset.filter(
                role__in=[Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
            )

        # Un utilisateur standard (or consultant) ne voit QUE son propre profil en base
        return self.queryset.filter(id=acteur.id)

    def perform_destroy(self, instance):
        """Sécurise la suppression d'un compte pour empêcher les débordements de rôles."""
        acteur = self.request.user
        
        # Sécurité pour empêcher la suppression hors périmètre
        if acteur.role == Utilisateur.Role.SUPER_ADMIN and instance.role != Utilisateur.Role.SUPER_ADMIN:
            raise PermissionDenied("Vous ne pouvez supprimer que les membres de votre équipe de développement.")
            
        if acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
            roles_geres = [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
            if instance.role not in roles_geres:
                raise PermissionDenied("Vous ne pouvez supprimer que des Utilisateurs ou des Consultants.")
                
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