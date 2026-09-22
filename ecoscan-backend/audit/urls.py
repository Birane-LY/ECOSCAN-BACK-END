from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .views import JournalAuditViewSet

router = SimpleRouter()
router.register(r'logs', JournalAuditViewSet, basename='journal-audit')

urlpatterns = [
    path('', include(router.urls)),
]