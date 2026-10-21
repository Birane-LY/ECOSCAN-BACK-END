from uuid import UUID

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, mixins, permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from analysis.models import Anomalie
from analysis.serializers import AnomalieSerializer
from billing.permissions import EstAbonnementActif
from billing.services import BillingAccessService
from organizations.models import Organisation, Site

from .models import (
    ActionVirtuelle,
    Capteur,
    CapteurPresence,
    CommandeEquipement,
    DecisionAutonome,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    MesurePresence,
    PilotageAutonome,
    ProfilFonctionnement,
    Zone,
)
from .serializers import (
    ActionVirtuelleSerializer,
    CapteurSerializer,
    CapteurPresenceSerializer,
    CommandeEquipementSerializer,
    CurrentLoadSerializer,
    EquipementSerializer,
    EtatEquipementSerializer,
    MesureCapteurSerializer,
    MesurePresenceSerializer,
    MonitoringSummarySerializer,
    PilotageAutonomeSerializer,
    ProfilFonctionnementSerializer,
    TelemetryHistorySerializer,
    ZoneSerializer,
)
from .monitoring import obtenir_indicateurs_site
from .anomalies import code_metrique_site
from .occupancy_anomalies import code_metrique_occupation_zone
from .services import synchroniser_etat_equipement
from .autopilot import (
    equipement_pilotable_en_simulation,
    evaluer_pilotage_autonome,
)
from .occupancy import diagnostic_occupation_site


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


class CapteurPresenceViewSet(OrganisationScopedViewSet, viewsets.ModelViewSet):
    """Expose la gestion des capteurs d'occupation des zones accessibles."""

    organisation_lookup = "zone__site__organisation"
    queryset = CapteurPresence.objects.select_related(
        "zone__site__organisation"
    ).order_by("identifiant")
    serializer_class = CapteurPresenceSerializer


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


class MesurePresenceViewSet(
    OrganisationScopedViewSet,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Expose l'historique et l'ingestion des mesures de présence."""

    organisation_lookup = "capteur__zone__site__organisation"
    queryset = MesurePresence.objects.select_related(
        "capteur__zone__site__organisation"
    ).order_by("-date_mesure")
    serializer_class = MesurePresenceSerializer

    def perform_create(self, serializer):
        capteur = serializer.validated_data["capteur"]
        with transaction.atomic():
            serializer.save()
            capteur.derniere_communication = timezone.now()
            capteur.save(update_fields=("derniere_communication",))


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


class PublicDemoView(APIView):
    """Expose uniquement des indicateurs agrégés des organisations de démonstration."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        organisations = Organisation.objects.filter(
            details_demande__ecoscan_demo_scenario__in=(
                "multisite",
                "hotel",
                "usine",
                "restaurant",
            ),
            statut=Organisation.Statut.ACTIVE,
        ).order_by("nom")
        scenarios = []

        for organisation in organisations:
            key = organisation.details_demande["ecoscan_demo_scenario"]
            sites = []
            for site in Site.objects.filter(organisation=organisation).order_by("nom"):
                summary, _ = obtenir_indicateurs_site(site)
                equipment = [
                    {
                        "name": item["name"],
                        "zone": item["zone_name"],
                        "power_kw": item["power_kw"],
                        "reported_state": item["reported_state"],
                        "online": item["is_online"],
                        "telemetry_mode": item["telemetry_mode"],
                    }
                    for item in summary["equipment"]
                ]
                anomalies = Anomalie.objects.filter(
                    organisation=organisation,
                ).filter(
                    Q(resultat_metrique__code_metrique=code_metrique_site(site))
                    | Q(
                        resultat_metrique__code_metrique__in=[
                            code_metrique_occupation_zone(zone)
                            for zone in site.zones.all()
                        ]
                    )
                ).prefetch_related("recommandations").order_by("-date_detection")
                anomaly_data = [
                    {
                        "type": item.type,
                        "severity": item.severite,
                        "status": item.statut,
                        "observed": item.valeur_observee,
                        "expected": item.valeur_attendue,
                        "deviation_percent": item.ecart_pourcentage,
                        "recommendations": [
                            {
                                "title": recommendation.titre,
                                "description": recommendation.description,
                                "priority": recommendation.priorite,
                                "status": recommendation.statut,
                                "estimated_saving": recommendation.economie_estimee,
                                "unit": recommendation.unite,
                            }
                            for recommendation in item.recommandations.all()
                        ],
                    }
                    for item in anomalies
                ]
                sensor_stats = list(
                    Capteur.objects.filter(equipement__site=site)
                    .values("mode", "statut")
                    .annotate(total=Count("id"))
                    .order_by("mode", "statut")
                )
                presence_sensor_stats = list(
                    CapteurPresence.objects.filter(zone__site=site)
                    .values("mode", "statut")
                    .annotate(total=Count("id"))
                    .order_by("mode", "statut")
                )
                policy = PilotageAutonome.objects.filter(site=site).first()
                decisions = (
                    DecisionAutonome.objects.filter(site=site)
                    .values("statut")
                    .annotate(total=Count("id"))
                    .order_by("statut")
                )
                commands = (
                    CommandeEquipement.objects.filter(equipement__site=site)
                    .values("statut")
                    .annotate(total=Count("id"))
                    .order_by("statut")
                )
                sites.append(
                    {
                        "name": site.nom,
                        "summary": {
                            "site_name": site.nom,
                            "telemetry_mode": summary["telemetry_mode"],
                            "current_power_kw": summary["current_power_kw"],
                            "today_energy_kwh": summary["today_energy_kwh"],
                            "yesterday_energy_kwh": summary["yesterday_energy_kwh"],
                            "month_energy_kwh": summary["month_energy_kwh"],
                            "energy_coverage": summary["energy_coverage"],
                            "monitored_equipment_count": summary[
                                "monitored_equipment_count"
                            ],
                            "active_equipment_count": summary[
                                "active_equipment_count"
                            ],
                            "offline_equipment_count": summary[
                                "offline_equipment_count"
                            ],
                            "latest_measurement_at": summary[
                                "latest_measurement_at"
                            ],
                        },
                        "equipment": equipment,
                        "anomalies": anomaly_data,
                        "occupancy": diagnostic_occupation_site(site),
                        "sensors": sensor_stats,
                        "presence_sensors": presence_sensor_stats,
                        "autopilot": {
                            "active": policy.actif if policy else False,
                            "emergency_stop": policy.arret_urgence if policy else False,
                            "schedule_enabled": policy.horaires_actifs if policy else False,
                            "decisions": list(decisions),
                        },
                        "commands": list(commands),
                    }
                )
            scenarios.append(
                {
                    "key": key,
                    "sector": organisation.secteur,
                    "sites": sites,
                }
            )

        return Response(
            {
                "data_mode": "SIMULATED",
                "read_only": True,
                "scenarios": scenarios,
            }
        )


class SiteAutopilotView(SiteMonitoringAPIView):
    """Expose la configuration et le journal du pilote d'autonomie simulé."""

    def _configuration(self, site):
        configuration, _ = PilotageAutonome.objects.get_or_create(site=site)
        return configuration

    @staticmethod
    def _decisions(site):
        return [
            {
                "id": str(item.id),
                "strategie": item.strategie,
                "statut": item.statut,
                "action": item.action,
                "raison": item.raison,
                "equipement_id": (
                    str(item.equipement_id) if item.equipement_id else None
                ),
                "equipement_nom": item.equipement.nom if item.equipement else None,
                "puissance_avant_kw": (
                    str(item.puissance_avant_kw)
                    if item.puissance_avant_kw is not None
                    else None
                ),
                "puissance_apres_kw": (
                    str(item.puissance_apres_kw)
                    if item.puissance_apres_kw is not None
                    else None
                ),
                "temperature_simulee_c": (
                    str(item.temperature_simulee_c)
                    if item.temperature_simulee_c is not None
                    else None
                ),
                "simulation": item.simulation,
                "date_creation": item.date_creation,
            }
            for item in DecisionAutonome.objects.filter(site=site)
            .select_related("equipement")
            .order_by("-date_creation")[:30]
        ]

    def get(self, request, site_id):
        site = self.get_site(request, site_id)
        configuration = self._configuration(site)
        equipements = Equipement.objects.filter(
            site=site,
            monitoring_active=True,
        ).order_by("nom")
        return Response(
            {
                "mode": "SIMULATION",
                "configuration": PilotageAutonomeSerializer(configuration).data,
                "equipements": [
                    {
                        "id": str(equipement.id),
                        "nom": equipement.nom,
                        "categorie": equipement.categorie,
                        "criticite": equipement.criticite,
                        "puissance_nominale_kw": str(
                            equipement.puissance_nominale_kw
                        ),
                        "autorisation_possible": equipement.criticite
                        in (
                            Equipement.Criticite.BASSE,
                            Equipement.Criticite.MOYENNE,
                        )
                        and equipement_pilotable_en_simulation(equipement),
                    }
                    for equipement in equipements
                ],
                "decisions": self._decisions(site),
            }
        )

    def patch(self, request, site_id):
        site = self.get_site(request, site_id)
        configuration = self._configuration(site)
        if request.data == {"arret_urgence": True}:
            configuration.arret_urgence = True
            configuration.actif = False
            configuration.save(
                update_fields=("arret_urgence", "actif", "date_maj")
            )
            return Response(
                {
                    "mode": "SIMULATION",
                    "configuration": PilotageAutonomeSerializer(
                        configuration
                    ).data,
                    "decisions": self._decisions(site),
                }
            )
        serializer = PilotageAutonomeSerializer(
            configuration,
            data=request.data,
            partial=True,
            context={"site": site},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "mode": "SIMULATION",
                "configuration": serializer.data,
                "decisions": self._decisions(site),
            }
        )

    def post(self, request, site_id):
        site = self.get_site(request, site_id)
        configuration = self._configuration(site)
        decision = evaluer_pilotage_autonome(site.id, force=True)
        return Response(
            {
                "mode": "SIMULATION",
                "decision": self._decisions(site)[0]
                if decision is not None
                else None,
                "configuration": PilotageAutonomeSerializer(configuration).data,
            }
        )


class SiteMonitoringSummaryView(SiteMonitoringAPIView):
    def get(self, request, site_id):
        site = self.get_site(request, site_id)
        summary, _ = obtenir_indicateurs_site(site)
        return Response(MonitoringSummarySerializer(summary).data)


class SiteOccupancyDiagnosticsView(SiteMonitoringAPIView):
    """Expose un diagnostic en lecture seule de l'énergie en zone inoccupée."""

    def get(self, request, site_id):
        site = self.get_site(request, site_id)
        return Response(diagnostic_occupation_site(site))


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


class SiteAnomaliesView(generics.ListAPIView):
    """Expose les anomalies de consommation du site accessible demandé."""

    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    serializer_class = AnomalieSerializer

    def get_queryset(self):
        organisations = BillingAccessService().organisations_avec_acces(
            self.request.user
        )
        site = get_object_or_404(
            Site.objects.filter(organisation__in=organisations),
            pk=self.kwargs["site_id"],
        )
        codes_occupation = [
            code_metrique_occupation_zone(zone)
            for zone in site.zones.all()
        ]
        return Anomalie.objects.filter(
            organisation=site.organisation,
        ).filter(
            Q(resultat_metrique__code_metrique=code_metrique_site(site))
            | Q(resultat_metrique__code_metrique__in=codes_occupation)
        ).select_related("resultat_metrique").prefetch_related(
            "recommandations",
            "hypotheses",
        )
