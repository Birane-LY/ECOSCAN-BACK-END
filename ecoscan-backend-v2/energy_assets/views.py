from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, permissions, viewsets

from billing.permissions import EstAbonnementActif
from billing.services import BillingAccessService

from .models import (
    Capteur,
    CommandeEquipement,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    ProfilFonctionnement,
    Zone,
)
from .serializers import (
    CapteurSerializer,
    CommandeEquipementSerializer,
    EquipementSerializer,
    EtatEquipementSerializer,
    MesureCapteurSerializer,
    ProfilFonctionnementSerializer,
    ZoneSerializer,
)
from .services import synchroniser_etat_equipement


class OrganisationScopedViewSet:
    """Restreint les objets aux organisations accessibles par l'utilisateur."""

    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    organisation_lookup = "site__organisation"

    def get_queryset(self):
        queryset = self.queryset
        if getattr(self.request.user, "role", None) == "SUPER_ADMIN":
            return queryset.none()
        organisations = BillingAccessService().organisations_avec_acces(self.request.user)
        return queryset.filter(**{f"{self.organisation_lookup}__in": organisations})


class ZoneViewSet(OrganisationScopedViewSet, viewsets.ModelViewSet):
    """Expose les opérations de gestion des zones d'un site."""

    queryset = Zone.objects.select_related("site__organisation").order_by("site__nom", "nom")
    serializer_class = ZoneSerializer


class EquipementViewSet(OrganisationScopedViewSet, viewsets.ModelViewSet):
    """Expose les équipements et initialise leur état de monitoring."""

    queryset = (
        Equipement.objects.select_related("site__organisation", "zone", "etat")
        .prefetch_related("capteurs")
        .order_by("site__nom", "nom")
    )
    serializer_class = EquipementSerializer

    def perform_create(self, serializer):
        with transaction.atomic():
            equipement = serializer.save()
            EtatEquipement.objects.create(equipement=equipement)


class ProfilFonctionnementViewSet(OrganisationScopedViewSet, viewsets.ModelViewSet):
    """Expose les créneaux de fonctionnement des équipements."""

    organisation_lookup = "equipement__site__organisation"
    queryset = ProfilFonctionnement.objects.select_related(
        "equipement__site__organisation"
    ).order_by("jour_semaine", "heure_debut")
    serializer_class = ProfilFonctionnementSerializer


class CapteurViewSet(OrganisationScopedViewSet, viewsets.ModelViewSet):
    """Expose les capteurs associés aux équipements."""

    organisation_lookup = "equipement__site__organisation"
    queryset = Capteur.objects.select_related(
        "equipement__site__organisation"
    ).order_by("identifiant")
    serializer_class = CapteurSerializer


class MesureCapteurViewSet(
    OrganisationScopedViewSet,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Expose la consultation et l'ingestion des mesures des capteurs."""

    organisation_lookup = "capteur__equipement__site__organisation"
    queryset = MesureCapteur.objects.select_related(
        "capteur__equipement__site__organisation"
    ).order_by("-date_mesure")
    serializer_class = MesureCapteurSerializer

    def perform_create(self, serializer):
        capteur = serializer.validated_data["capteur"]
        with transaction.atomic():
            mesure = serializer.save()
            capteur.derniere_communication = timezone.now()
            capteur.save(update_fields=("derniere_communication",))
            synchroniser_etat_equipement(mesure)


class CommandeEquipementViewSet(
    OrganisationScopedViewSet,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Expose les demandes de commande sans exécuter de commande matérielle."""

    organisation_lookup = "equipement__site__organisation"
    queryset = CommandeEquipement.objects.select_related(
        "equipement__site__organisation",
        "demande_par",
    ).order_by("-date_creation")
    serializer_class = CommandeEquipementSerializer


class EtatEquipementViewSet(OrganisationScopedViewSet, viewsets.ReadOnlyModelViewSet):
    """Expose en lecture seule l'état courant des équipements."""

    organisation_lookup = "equipement__site__organisation"
    queryset = EtatEquipement.objects.select_related(
        "equipement__site__organisation"
    ).order_by("equipement__nom")
    serializer_class = EtatEquipementSerializer
