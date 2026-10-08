from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import (
    CapteurViewSet,
    EquipementViewSet,
    EtatEquipementViewSet,
    ProfilFonctionnementViewSet,
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
router.register("etats-equipements", EtatEquipementViewSet, basename="etat-equipement")

urlpatterns = [
    path("", include(router.urls)),
]
