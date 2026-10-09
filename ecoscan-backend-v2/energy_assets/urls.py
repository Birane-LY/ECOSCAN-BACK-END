from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import (
    ActionVirtuelleViewSet,
    CapteurViewSet,
    CommandeEquipementViewSet,
    EquipementViewSet,
    EtatEquipementViewSet,
    MesureCapteurViewSet,
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
router.register("mesures", MesureCapteurViewSet, basename="mesure")
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
    path("", include(router.urls)),
]
