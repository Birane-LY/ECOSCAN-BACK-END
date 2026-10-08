from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .views import (
    OrganisationViewSet,
    UtilisateurOrganisationViewSet,
    SiteViewSet,
    FicheProjetViewSet,
    ActiviteViewSet,
    CompteurViewSet,
    ConfigurationSecuriteView,
    OnboardingSimpleView,
    InternalAIContextView,  
)

router = SimpleRouter()
router.register(r'structures', OrganisationViewSet, basename='organisation')
router.register(r'affiliations', UtilisateurOrganisationViewSet, basename='affiliation')
router.register(r'sites', SiteViewSet, basename='site')
router.register(r'projets', FicheProjetViewSet, basename='projet')
router.register(r'activites', ActiviteViewSet, basename='activite')
router.register(r'compteurs', CompteurViewSet, basename='compteur')

urlpatterns = [
    path('configuration-securite/', ConfigurationSecuriteView.as_view(), name='configuration_securite'),
    path('onboarding-simple/', OnboardingSimpleView.as_view(), name='onboarding_simple'),
    path('internal/organisations/<str:organisation_id>/ai-context/', InternalAIContextView.as_view(), name='internal_ai_context'),
    path('', include(router.urls)),
]