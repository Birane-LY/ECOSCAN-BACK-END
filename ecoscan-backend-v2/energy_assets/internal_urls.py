from django.urls import path

from .internal_views import (
    ProchaineCommandeInterneView,
    TransitionCommandeInterneView,
)

app_name = "energy_assets_internal"

urlpatterns = [
    path(
        "commandes/suivante/",
        ProchaineCommandeInterneView.as_view(),
        name="commande-suivante",
    ),
    path(
        "commandes/<uuid:commande_id>/confirmer/",
        TransitionCommandeInterneView.as_view(transition="confirmer"),
        name="commande-confirmer",
    ),
    path(
        "commandes/<uuid:commande_id>/echouer/",
        TransitionCommandeInterneView.as_view(transition="echouer"),
        name="commande-echouer",
    ),
]
