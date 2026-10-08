from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    ActionViewSet,
    AnomalieViewSet,
    AssistantQueryView,
    DecisionViewSet,
    DocumentEntrepriseViewSet,
    HypotheseViewSet,
    LivrableViewSet,
    MemoireStrategiqueViewSet,
    ObservationOperationnelleViewSet,
    OpportuniteFinancementIngestionView,
    OpportuniteFinancementViewSet,
    ProgressionObjectifsView,
    RecommandationViewSet,
    ResultatMetriqueViewSet,
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
router.register(r'opportunites-financement', OpportuniteFinancementViewSet, basename='opportunitefinancement')

urlpatterns = [
    path('integration/energy/', views.integrer_extraction_energy, name='integrer_energy'),
    path('assistant/interroger/', AssistantQueryView.as_view(), name='assistant_interroger'),
    path('progression-objectifs/', ProgressionObjectifsView.as_view(), name='progression_objectifs'),
    path('opportunites-financement/ingestion/', OpportuniteFinancementIngestionView.as_view(), name='funding_ingestion'),
    path('', include(router.urls)),
]
