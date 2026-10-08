from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CommandeEquipement
from .permissions import JetonServiceInternePermission
from .services import (
    confirmer_commande,
    echouer_commande,
    prendre_prochaine_commande,
)


class CommandeInterneSerializer(serializers.ModelSerializer):
    equipement_id = serializers.UUIDField(read_only=True)
    equipement_nom = serializers.CharField(source="equipement.nom", read_only=True)

    class Meta:
        model = CommandeEquipement
        fields = (
            "id",
            "equipement_id",
            "equipement_nom",
            "action",
            "statut",
            "date_creation",
            "date_envoi",
            "date_finalisation",
            "detail_echec",
        )


class EchecCommandeSerializer(serializers.Serializer):
    detail = serializers.CharField(allow_blank=False, trim_whitespace=True)


class ServiceInterneAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny, JetonServiceInternePermission]

    def get_authenticate_header(self, request):
        return "X-Internal-Service-Token"


class ProchaineCommandeInterneView(ServiceInterneAPIView):
    def post(self, request):
        commande = prendre_prochaine_commande()
        if commande is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(CommandeInterneSerializer(commande).data)


class TransitionCommandeInterneView(ServiceInterneAPIView):
    transition = None

    def post(self, request, commande_id):
        commande = get_object_or_404(
            CommandeEquipement.objects.select_related("equipement"),
            pk=commande_id,
        )
        if self.transition == "confirmer":
            transition_data = None
        else:
            serializer = EchecCommandeSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            transition_data = serializer.validated_data["detail"]

        try:
            if self.transition == "confirmer":
                commande = confirmer_commande(commande)
            else:
                commande = echouer_commande(commande, transition_data)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(CommandeInterneSerializer(commande).data)
