from django.urls import path, include
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views import (
    UtilisateurViewSet,
    OnboardingAdminOrganisationView,
    FinaliserInscriptionView
)

router = SimpleRouter()
router.register(r'membres', UtilisateurViewSet, basename='membre')

urlpatterns = [
    path('auth/connexion/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/rafraichir/', TokenRefreshView.as_view(), name='token_refresh'),
    path('onboarding/', OnboardingAdminOrganisationView.as_view(), name='onboarding_admin'),
    path('activation/', FinaliserInscriptionView.as_view(), name='activation_compte'),
    path('', include(router.urls)),
]
