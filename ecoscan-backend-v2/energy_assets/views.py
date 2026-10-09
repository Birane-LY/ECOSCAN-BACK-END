from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, mixins, permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import EstAbonnementActif
from billing.services import BillingAccessService
from organizations.models import Site

from .models import (
    ActionVirtuelle,
    Capteur,
    CommandeEquipement,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    ProfilFonctionnement,
    Zone,
)
from .serializers import (
    ActionVirtuelleSerializer,
    CapteurSerializer,
    CommandeEquipementSerializer,
    CurrentLoadSerializer,
    EquipementSerializer,
    EtatEquipementSerializer,
    MesureCapteurSerializer,
    MonitoringSummarySerializer,
    ProfilFonctionnementSerializer,
    TelemetryHistorySerializer,
    ZoneSerializer,
)
from .monitoring import obtenir_indicateurs_site
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


class ActionVirtuelleViewSet(
    OrganisationScopedViewSet,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Expose l'historique et la création de simulations énergétiques virtuelles."""

    organisation_lookup = "equipement__site__organisation"
    queryset = ActionVirtuelle.objects.select_related(
        "equipement__site__organisation",
        "recommandation__objectif__organisation",
        "cree_par",
    )
    serializer_class = ActionVirtuelleSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        recommandation_id = self.request.query_params.get("recommandation")
        if not recommandation_id:
            return queryset
        try:
            UUID(str(recommandation_id))
        except ValueError:
            return queryset.none()
        return queryset.filter(recommandation_id=recommandation_id)


class EtatEquipementViewSet(OrganisationScopedViewSet, viewsets.ReadOnlyModelViewSet):
    """Expose en lecture seule l'état courant des équipements."""

    organisation_lookup = "equipement__site__organisation"
    queryset = EtatEquipement.objects.select_related(
        "equipement__site__organisation"
    ).order_by("equipement__nom")
    serializer_class = EtatEquipementSerializer


class SiteMonitoringAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]

    def get_site(self, request, site_id):
        organisations = BillingAccessService().organisations_avec_acces(request.user)
        return get_object_or_404(
            Site.objects.select_related("organisation"),
            pk=site_id,
            organisation__in=organisations,
        )


class SiteMonitoringSummaryView(SiteMonitoringAPIView):
    def get(self, request, site_id):
        site = self.get_site(request, site_id)
        summary, _ = obtenir_indicateurs_site(site)
        return Response(MonitoringSummarySerializer(summary).data)


class SiteCurrentLoadView(SiteMonitoringAPIView):
    def get(self, request, site_id):
        site = self.get_site(request, site_id)
        summary, _ = obtenir_indicateurs_site(site)
        current_load = {
            key: summary[key]
            for key in (
                "site_id",
                "site_name",
                "current_power_kw",
                "latest_measurement_at",
                "equipment",
            )
        }
        return Response(CurrentLoadSerializer(current_load).data)


class SiteTopConsumersView(SiteMonitoringAPIView):
    def get(self, request, site_id):
        site = self.get_site(request, site_id)
        _, consumers = obtenir_indicateurs_site(site)
        return Response(consumers[:10])


class EquipmentTelemetryHistoryView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    serializer_class = TelemetryHistorySerializer

    def get_queryset(self):
        organisations = BillingAccessService().organisations_avec_acces(
            self.request.user
        )
        equipement = get_object_or_404(
            Equipement.objects.filter(site__organisation__in=organisations),
            pk=self.kwargs["equipement_id"],
        )
        return (
            MesureCapteur.objects.filter(capteur__equipement=equipement)
            .select_related("capteur")
            .order_by("-date_mesure", "-date_reception")
        )
