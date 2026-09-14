# analysis/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    RecommandationViewSet,
    ActionViewSet,
    DecisionViewSet,
    LivrableViewSet,
    ResultatMetriqueViewSet,
    ObservationOperationnelleViewSet,
    AnomalieViewSet,
    HypotheseViewSet,
    MemoireStrategiqueViewSet,
    DocumentEntrepriseViewSet,
)
from . import views


router = DefaultRouter()
router.register(r'recommandations', RecommandationViewSet, basename='recommandation')
router.register(r'actions', ActionViewSet, basename='action')
router.register(r'decisions', DecisionViewSet, basename='decision')
router.register(r'livrables', LivrableViewSet, basename='livrable')
router.register(r'resultats-metriques', ResultatMetriqueViewSet, basename='resultatmetrique')
router.register(r'observations', ObservationOperationnelleViewSet, basename='observation')
router.register(r'anomalies', AnomalieViewSet, basename='anomalie')
router.register(r'hypotheses', HypotheseViewSet, basename='hypothese')
router.register(r'memoires-strategiques', MemoireStrategiqueViewSet, basename='memoirestrategique')
router.register(r'documents-entreprise', DocumentEntrepriseViewSet, basename='documententreprise')

urlpatterns = [
    # Inclut l'ensemble des routes CRUD et actions générées par le routeur
    path('integration/energy/', views.integrer_extraction_energy, name='integrer_energy'),
    path('', include(router.urls)),
]