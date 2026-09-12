from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .views import (
    OrganisationViewSet,
    UtilisateurOrganisationViewSet,
    SiteViewSet,
    FicheProjetViewSet,
    ActiviteViewSet,
    CompteurViewSet,
)

router = SimpleRouter()
router.register(r'structures', OrganisationViewSet, basename='organisation')
router.register(r'affiliations', UtilisateurOrganisationViewSet, basename='affiliation')
router.register(r'sites', SiteViewSet, basename='site')
router.register(r'projets', FicheProjetViewSet, basename='projet')
router.register(r'activites', ActiviteViewSet, basename='activite')
router.register(r'compteurs', CompteurViewSet, basename='compteur')

urlpatterns = [
    path('', include(router.urls)),
]
