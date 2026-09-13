from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DonneeEnergetiqueViewSet,
    FacteurEmissionViewSet,
    FichierSourceViewSet,
    HistoriquePerformanceViewSet,
    ImportDonneesViewSet,
    IndicateurObjectifViewSet,
    IndicateurViewSet,
    ObjectifViewSet,
    SourceDonneeViewSet,
    SyntheseFinanciereViewSet,
)

app_name = "energy"

router = DefaultRouter()

router.register(
    r"fichiers-sources",
    FichierSourceViewSet,
    basename="fichier-source",
)

router.register(
    r"imports",
    ImportDonneesViewSet,
    basename="import-donnees",
)

router.register(
    r"sources-donnees",
    SourceDonneeViewSet,
    basename="source-donnee",
)

router.register(
    r"facteurs-emission",
    FacteurEmissionViewSet,
    basename="facteur-emission",
)

router.register(
    r"donnees-energetiques",
    DonneeEnergetiqueViewSet,
    basename="donnee-energetique",
)

router.register(
    r"historiques-performance",
    HistoriquePerformanceViewSet,
    basename="historique-performance",
)

router.register(
    r"syntheses-financieres",
    SyntheseFinanciereViewSet,
    basename="synthese-financiere",
)

router.register(
    r"objectifs",
    ObjectifViewSet,
    basename="objectif",
)

router.register(
    r"indicateurs",
    IndicateurViewSet,
    basename="indicateur",
)

router.register(
    r"indicateurs-objectifs",
    IndicateurObjectifViewSet,
    basename="indicateur-objectif",
)

urlpatterns = [
    path("", include(router.urls)),
]
