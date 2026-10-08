from django.contrib import admin
from django.urls import path, include
from analysis.internal_views import AIContextView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('accounts.urls')),
    path("api/internal/organisations/<str:organisation_id>/ai-context/", AIContextView.as_view()),
    path(
        "api/internal/energy-assets/",
        include("energy_assets.internal_urls"),
    ),
    path('api/organisations/', include('organizations.urls')), 
    path("api/energies/", include("energy.urls", namespace="energy")),
    path("api/energy-assets/", include("energy_assets.urls")),
    path('api/analyses/', include('analysis.urls')),
    path('api/audits/', include('audit.urls')),
    path('api/billing/', include('billing.urls')),
    path('api/paiements/', include(('billing.urls', 'billing_legacy'), namespace='billing_legacy')),
]