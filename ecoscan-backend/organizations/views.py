from rest_framework import viewsets, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from .models import (
    Organisation,
    UtilisateurOrganisation,
    Site,
    FicheProjet,
    Activite,
    Compteur,
)
from .serializers import (
    OrganisationSerializer,
    UtilisateurOrganisationSerializer,
    SiteSerializer,
    FicheProjetSerializer,
    ActiviteSerializer,
    CompteurSerializer,
)


class EstMembreDeLOrganisation(permissions.BasePermission):
    """Permission globale appliquant le principe de cloisonnement strict par entreprise.

    L'accès est accordé uniquement si l'utilisateur est explicitement affilié à la structure.
    Le Super Admin ne peut pas contourner cette règle sur les données métiers des clients.
    """

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and request.user.actif

    def has_object_permission(self, request, view, obj):
        # Détermination dynamique de l'organisation selon l'objet ciblé
        if isinstance(obj, Organisation):
            organisation = obj
        elif hasattr(obj, "organisation"):
            organisation = obj.organisation
        elif isinstance(obj, Activite):
            organisation = obj.fiche_projet.organisation
        elif isinstance(obj, Compteur):
            organisation = obj.site.organisation
        else:
            return False

        # Le Super Admin doit lui aussi être affilié à l'organisation 
        # pour pouvoir consulter ou interagir avec ses objets métiers (Privacy by Design).
        return UtilisateurOrganisation.objects.filter(
            organisation=organisation,
            utilisateur=request.user
        ).exists()


class OrganisationScopedViewSet(viewsets.ModelViewSet):
    """Classe de base abstraite appliquant l'aveuglement par défaut.

    Tous les rôles (y compris Super Admin) ne voient en base de données 
    que les lignes des organisations auxquelles ils sont rattachés.
    """
    permission_classes = [EstMembreDeLOrganisation]
    organisation_field = "organisation"

    def get_queryset(self):
        queryset = self.queryset
        user = self.request.user

        # Récupération de la liste des organisations de l'utilisateur connecté
        organisations = Organisation.objects.filter(membres__utilisateur=user)

        # Application dynamique du filtre selon la profondeur de la relation dans le modèle
        if self.organisation_field == "organisation":
            return queryset.filter(organisation__in=organisations)
            
        return queryset.filter(**{f"{self.organisation_field}__organisation__in": organisations})


class OrganisationViewSet(viewsets.ModelViewSet):
    """Contrôleur gérant le cycle de vie des organisations clientes.

    - Le Super Admin liste et modifie UNIQUEMENT le statut/défaut de paiement (Automatisable par n8n).
    - La suppression physique est formellement interdite.
    """
    queryset = Organisation.objects.all().order_by("nom")
    serializer_class = OrganisationSerializer
    permission_classes = [EstMembreDeLOrganisation]

    def get_queryset(self):
        # Le Super Admin conserve le droit de lister toutes les structures de la plateforme
        if self.request.user.role == "SUPER_ADMIN":
            return self.queryset
        return self.queryset.filter(membres__utilisateur=self.request.user).distinct()

    def update(self, request, *args, **kwargs):
        """Restreint les modifications d'accès aux seules contraintes financières (Abonnement)."""
        acteur = request.user
        instance = self.get_object()

        # L'action est autorisée pour le SUPER_ADMIN connecté ou via un script authentifié (n8n)
        if acteur.role == "SUPER_ADMIN" or request.auth: 
            nouveau_statut = request.data.get("statut")
            nouveau_defaut = request.data.get("defaut_paiement")

            # 1. Traitement du flag de paiement envoyé par n8n ou le Super Admin
            if nouveau_defaut is not None:
                instance.defaut_paiement = nouveau_defaut

            # 2. Traitement du changement de statut (Suspension / Réactivation)
            if nouveau_statut and instance.statut != nouveau_statut:
                
                # Tentative de Suspension : Impossible si le client est à jour
                if nouveau_statut == Organisation.Statut.SUSPENDUE:
                    if not instance.defaut_paiement and not nouveau_defaut:
                        raise PermissionDenied(
                            "Action refusée. L'organisation est à jour dans ses paiements."
                        )
                
                # Tentative de Réactivation : Impossible si le défaut n'est pas résolu
                elif nouveau_statut == Organisation.Statut.ACTIVE and instance.statut == Organisation.Statut.SUSPENDUE:
                    if instance.defaut_paiement:
                        raise PermissionDenied(
                            "Action refusée. Le défaut de paiement doit d'abord être régularisé."
                        )

                instance.statut = nouveau_statut
            
            instance.save()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
                
        raise PermissionDenied("Vous n'avez pas l'autorisation de modifier les données de ce client.")

    def perform_destroy(self, instance):
        """Interdit la suppression définitive d'une organisation en production."""
        raise PermissionDenied(
            "La suppression d'une organisation est interdite pour préserver l'historique des données. "
            "Veuillez suspendre ou archiver son accès."
        )


class UtilisateurOrganisationViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les affiliations et liaisons des membres à leurs organisations."""
    queryset = UtilisateurOrganisation.objects.select_related("organisation", "utilisateur")
    serializer_class = UtilisateurOrganisationSerializer


class SiteViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les sites physiques de manière hermétique par organisation."""
    queryset = Site.objects.select_related("organisation").order_by("nom")
    serializer_class = SiteSerializer


class FicheProjetViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les fiches projets et les audits de l'organisation."""
    queryset = FicheProjet.objects.select_related("organisation").order_by("nom")
    serializer_class = FicheProjetSerializer


class ActiviteViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les activités opérationnelles découlant des fiches projets."""
    queryset = Activite.objects.select_related("fiche_projet__organisation").order_by("nom")
    serializer_class = ActiviteSerializer
    organisation_field = "fiche_projet"


class CompteurViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les équipements de mesure énergétique rattachés aux sites."""
    queryset = Compteur.objects.select_related("site__organisation").prefetch_related("activites").order_by("reference")
    serializer_class = CompteurSerializer
    organisation_field = "site"
