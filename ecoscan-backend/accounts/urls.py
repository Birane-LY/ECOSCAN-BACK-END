from django.urls import path, include
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenRefreshView
from .views import UtilisateurViewSet, OnboardingAdminOrganisationView, FinaliserInscriptionView, ConnexionView, MesPreferencesView, ChangerMotDePasseView

router = SimpleRouter()
router.register(r'membres', UtilisateurViewSet, basename='membre')

urlpatterns = [
    path('auth/connexion/', ConnexionView.as_view(), name='token_obtain_pair'),
    path('auth/rafraichir/', TokenRefreshView.as_view(), name='token_refresh'),
    path('onboarding/', OnboardingAdminOrganisationView.as_view(), name='onboarding_admin'),
    path('activation/', FinaliserInscriptionView.as_view(), name='activation_compte'),
    path('mes-preferences/', MesPreferencesView.as_view(), name='mes_preferences'),
    path('changer-mot-de-passe/', ChangerMotDePasseView.as_view(), name='changer_mot_de_passe'),
    path('', include(router.urls)),
]
