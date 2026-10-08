from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import (
    CapteurViewSet,
    CommandeEquipementViewSet,
    EquipementViewSet,
    EtatEquipementViewSet,
    MesureCapteurViewSet,
    ProfilFonctionnementViewSet,
    SiteAnomaliesView,
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
router.register("mesures", MesureCapteurViewSet, basename="mesure")
router.register(
    "commandes",
    CommandeEquipementViewSet,
    basename="commande-equipement",
)
router.register("etats-equipements", EtatEquipementViewSet, basename="etat-equipement")

urlpatterns = [
    path(
        "sites/<uuid:site_id>/anomalies/",
        SiteAnomaliesView.as_view(),
        name="site-anomalies",
    ),
    path("", include(router.urls)),
]
