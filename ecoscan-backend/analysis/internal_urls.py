from django.urls import path

from .internal_views import AIContextView

urlpatterns = [
    path("organisations/<str:organisation_id>/ai-context/", AIContextView.as_view(), name="ai_context"),
]