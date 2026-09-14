from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    # Interface d'administration Django native
    path('admin/', admin.site.split_path if hasattr(admin.site, 'split_path') else admin.site.urls),
    
    # Inclusion des routes de notre application d'authentification et de membres
    path('api/', include('accounts.urls')),
    path('api/organisations/', include('organizations.urls')), 
]

