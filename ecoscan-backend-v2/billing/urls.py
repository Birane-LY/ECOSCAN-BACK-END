# billing/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import AbonnementViewSet, FactureViewSet, PaiementViewSet, PlanViewSet, RelanceCallbackView, RelanceViewSet
from .webhooks import webhook_paiement

router = DefaultRouter()
router.register(r'plans', PlanViewSet, basename='plan')
router.register(r'abonnements', AbonnementViewSet, basename='abonnement')
router.register(r'factures', FactureViewSet, basename='facture')
router.register(r'paiements', PaiementViewSet, basename='paiement')
router.register(r'relances', RelanceViewSet, basename='relance')

urlpatterns = [
    # /billing/webhooks/paydunya/, /billing/webhooks/manual/... — le fournisseur
    # est dans l'URL pour que chaque prestataire ait son propre point d'entrée,
    # sans avoir à deviner lequel a envoyé la requête depuis le seul payload.
    path('webhooks/<str:fournisseur>/', webhook_paiement, name='webhook_paiement'),
    # Callback du workflow n8n confirmant l'envoi effectif d'une relance
    # (secret partagé, voir permissions.EstAppelDeConfianceN8N).
    path('relances/<uuid:relance_id>/callback/', RelanceCallbackView.as_view(), name='relance_callback'),
    path('', include(router.urls)),
]