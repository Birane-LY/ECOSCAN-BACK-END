from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from organizations.models import Organisation
from .models import Action, Decision, Livrable, Recommandation, ResultatMetrique
from .serializers import (
    ActionSerializer,
    DecisionSerializer,
    LivrableSerializer,
    RecommandationSerializer,
    ResultatMetriqueSerializer,
)


class AnalyseScopedViewSet(viewsets.ModelViewSet):
    """Classe de base appliquant le filtrage multi-tenant dès la requête QuerySet.

    Le rôle SUPER_ADMIN est exclu de l'accès aux objets métier pour préserver la
    confidentialité et la souveraineté des données d'activité des clients.
    """

    permission_classes = [IsAuthenticated]
    organisation_lookup = "recommandation__objectif__organisation"

    def get_queryset(self):
        """Filtre les résultats à la source pour n'inclure que les tenants de l'utilisateur."""
        queryset = self.queryset
        user = self.request.user

        if getattr(user, "role", None) == "SUPER_ADMIN":
            return queryset.none()

        organisations = Organisation.objects.filter(membres__utilisateur=user)
        return queryset.filter(**{f"{self.organisation_lookup}__in": organisations})


class RecommandationViewSet(AnalyseScopedViewSet):
    """Pilote le cycle de vie et l'analyse décisionnelle des pistes d'économies d'énergie."""

    queryset = Recommandation.objects.select_related("objectif__organisation").order_by("-priorite", "date_echeance")
    serializer_class = RecommandationSerializer
    organisation_lookup = "objectif__organisation"

    @action(detail=True, methods=["post"])
    def generer(self, request, pk=None):
        """Déclenche la phase d'édition initiale de la recommandation d'efficacité."""
        recommandation = self.get_object()
        recommandation.generer()
        return Response(self.get_serializer(recommandation).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="marquer-comme-decidee")
    def marquer_comme_decidee(self, request, pk=None):
        """Valide formellement l'adoption stratégique de la piste d'amélioration par l'entité."""
        recommandation = self.get_object()
        recommandation.marquer_comme_decidee()
        return Response(self.get_serializer(recommandation).data, status=status.HTTP_200_OK)


class ActionViewSet(AnalyseScopedViewSet):
    """Assure le suivi opérationnel, l'avancement technique et l'affectation terrain."""

    queryset = Action.objects.select_related("recommandation__objectif__organisation", "responsable").order_by("date_echeance")
    serializer_class = ActionSerializer
    organisation_lookup = "recommandation__objectif__organisation"

    @action(detail=True, methods=["post"])
    def suivre(self, request, pk=None):
        """Bascule l'étape technique vers l'état opérationnel en cours d'exécution."""
        action_obj = self.get_object()
        action_obj.suivre()
        return Response(self.get_serializer(action_obj).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def cloturer(self, request, pk=None):
        """Enregistre la complétion définitive de l'action avec traçabilité temporelle."""
        action_obj = self.get_object()
        action_obj.cloturer()
        return Response(self.get_serializer(action_obj).data, status=status.HTTP_200_OK)


class DecisionViewSet(AnalyseScopedViewSet):
    """Consigne l'historique et l'arbitrage formel des décisions d'investissements."""

    queryset = Decision.objects.select_related("recommandation__objectif__organisation", "decideur").order_by("-date_decision")
    serializer_class = DecisionSerializer
    organisation_lookup = "recommandation__objectif__organisation"


class LivrableViewSet(AnalyseScopedViewSet):
    """Gère l'édition de rapports d'audits, de livrables et la validation des versions."""

    queryset = Livrable.objects.select_related("fiche_projet__organisation").order_by("-date_generation")
    serializer_class = LivrableSerializer
    organisation_lookup = "fiche_projet__organisation"

    @action(detail=True, methods=["post"])
    def generer(self, request, pk=None):
        """Fige la publication formelle du document technique ou de la certification."""
        livrable = self.get_object()
        livrable.generer()
        return Response(self.get_serializer(livrable).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def valider(self, request, pk=None):
        """Approuve de façon définitive la validité réglementaire du livrable d'analyse."""
        livrable = self.get_object()
        livrable.valider()
        return Response(self.get_serializer(livrable).data, status=status.HTTP_200_OK)


class ResultatMetriqueViewSet(AnalyseScopedViewSet):
    """Expose l'Audit Trail immuable des résultats de calculs, baselines et bilans carbone."""

    queryset = ResultatMetrique.objects.select_related("organisation", "compteur").order_by("-periode_fin")
    serializer_class = ResultatMetriqueSerializer
    organisation_lookup = "organisation"
