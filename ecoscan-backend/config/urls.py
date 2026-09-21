from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.split_path if hasattr(admin.site, 'split_path') else admin.site.urls),
    path('api/', include('accounts.urls')),
    path('api/organisations/', include('organizations.urls')), 
    path("api/energies/", include("energy.urls", namespace="energy")),
    path('api/analyses/', include('analysis.urls')),
]

