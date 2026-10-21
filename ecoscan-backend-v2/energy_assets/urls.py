from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import (
    ActionVirtuelleViewSet,
    CapteurViewSet,
    CapteurPresenceViewSet,
    CommandeEquipementViewSet,
    EquipementViewSet,
    EquipmentTelemetryHistoryView,
    EtatEquipementViewSet,
    MesureCapteurViewSet,
    ProfilFonctionnementViewSet,
    SiteCurrentLoadView,
    SiteMonitoringSummaryView,
    SiteOccupancyDiagnosticsView,
    SiteTopConsumersView,
    SiteAnomaliesView,
    SiteAutopilotView,
    PublicDemoView,
    MesurePresenceViewSet,
    ZoneViewSet,
)

app_name = "energy_assets"

router = SimpleRouter()
router.register("zones", ZoneViewSet, basename="zone")
router.register("equipements", EquipementViewSet, basename="equipement")
router.register(
    "profils-fonctionnement",
    ProfilFonctionnementViewSet,
    basename="profil-fonctionnement",
)
router.register("capteurs", CapteurViewSet, basename="capteur")
router.register(
    "capteurs-presence",
    CapteurPresenceViewSet,
    basename="capteur-presence",
)
router.register("mesures", MesureCapteurViewSet, basename="mesure")
router.register(
    "mesures-presence",
    MesurePresenceViewSet,
    basename="mesure-presence",
)
router.register(
    "commandes",
    CommandeEquipementViewSet,
    basename="commande-equipement",
)
router.register(
    "actions-virtuelles",
    ActionVirtuelleViewSet,
    basename="action-virtuelle",
)
router.register("etats-equipements", EtatEquipementViewSet, basename="etat-equipement")

urlpatterns = [
    path("demo/", PublicDemoView.as_view(), name="public-demo"),
    path(
        "sites/<uuid:site_id>/autonomie/",
        SiteAutopilotView.as_view(),
        name="site-autonomie",
    ),
    path(
        "sites/<uuid:site_id>/monitoring/summary/",
        SiteMonitoringSummaryView.as_view(),
        name="site-monitoring-summary",
    ),
    path(
        "sites/<uuid:site_id>/occupancy-diagnostics/",
        SiteOccupancyDiagnosticsView.as_view(),
        name="site-occupancy-diagnostics",
    ),
    path(
        "sites/<uuid:site_id>/monitoring/current-load/",
        SiteCurrentLoadView.as_view(),
        name="site-current-load",
    ),
    path(
        "sites/<uuid:site_id>/monitoring/top-consumers/",
        SiteTopConsumersView.as_view(),
        name="site-top-consumers",
    ),
    path(
        "equipements/<uuid:equipement_id>/telemetry/",
        EquipmentTelemetryHistoryView.as_view(),
        name="equipement-telemetry",
    ),
    path(
        "sites/<uuid:site_id>/anomalies/",
        SiteAnomaliesView.as_view(),
        name="site-anomalies",
    ),
    path("", include(router.urls)),
]
