import logging

from django.core.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from energy.models import ImportDonnees
from organizations.models import Organisation

from .models import (
    Action,
    Anomalie,
    Decision,
    DocumentEntreprise,
    Hypothese,
    Livrable,
    MemoireStrategique,
    ObservationOperationnelle,
    Recommandation,
    ResultatMetrique,
)
from .serializers import (
    ActionSerializer,
    AnomalieSerializer,
    DecisionSerializer,
    DocumentEntrepriseSerializer,
    HypotheseSerializer,
    LivrableSerializer,
    MemoireStrategiqueSerializer,
    ObservationOperationnelleSerializer,
    RecommandationSerializer,
    ResultatMetriqueSerializer,
)

logger = logging.getLogger(__name__)


class AnalyseScopedQuerySetMixin:
    """Filtrage multi-tenant, indépendant du type de ViewSet (lecture seule ou complet).

    Séparé de AnalyseScopedViewSet pour pouvoir être combiné avec
    ReadOnlyModelViewSet (ResultatMetriqueViewSet) sans hériter aussi de
    ModelViewSet — sinon update()/partial_update() restent exposées via l'ordre
    de résolution des méthodes, même en ajoutant ReadOnlyModelViewSet en second
    parent.

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


class AnalyseScopedViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """Classe de base pour les ViewSets métier nécessitant CRUD complet (recommandations,
    actions, décisions, livrables)."""


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

    def perform_create(self, serializer):
        """Renseigne le décideur et la date lors de la création d'une décision."""
        from django.utils import timezone
        serializer.save(decideur=self.request.user, date_decision=timezone.now())


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


class ResultatMetriqueViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Expose les résultats de calculs, baselines et bilans carbone."""

    queryset = ResultatMetrique.objects.select_related("organisation", "compteur").order_by("-periode_fin")
    serializer_class = ResultatMetriqueSerializer
    organisation_lookup = "organisation"


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def integrer_extraction_energy(request):
    """Integre un import 'energy' TERMINÉ comme résultat métrique."""
    import_id = request.data.get("import_id")
    if not import_id:
        return Response({"error": "import_id est requis."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        import_instance = ImportDonnees.objects.select_related("organisation", "fichier_source").get(id=import_id)
    except (ImportDonnees.DoesNotExist, ValueError, ValidationError):
        return Response({"error": f"Import {import_id} introuvable."}, status=status.HTTP_404_NOT_FOUND)

    if getattr(request.user, "role", None) == "SUPER_ADMIN":
        return Response(
            {"error": "Rôle SUPER_ADMIN non autorisé sur les objets métier des tenants."},
            status=status.HTTP_403_FORBIDDEN,
        )

    appartient = Organisation.objects.filter(
        id=import_instance.organisation_id, membres__utilisateur=request.user
    ).exists()
    if not appartient:
        return Response({"error": "Import hors de votre organisation."}, status=status.HTTP_403_FORBIDDEN)

    if import_instance.statut != ImportDonnees.Statut.TERMINE:
        return Response(
            {
                "error": (
                    f"Import non publiable : statut actuel '{import_instance.statut}', "
                    f"'{ImportDonnees.Statut.TERMINE}' requis."
                )
            },
            status=status.HTTP_409_CONFLICT,
        )

    resultat, created = ResultatMetrique.objects.get_or_create(
        organisation=import_instance.organisation,
        fichier_source=import_instance.fichier_source,
        defaults={
            "limites": f"Importé depuis l'import {import_instance.id}",
        }
    )

    statut_http = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return Response(ResultatMetriqueSerializer(resultat).data, status=statut_http)


class ObservationOperationnelleViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """Notes de terrain."""

    queryset = ObservationOperationnelle.objects.select_related("organisation", "auteur").order_by("-date_observation")
    serializer_class = ObservationOperationnelleSerializer
    organisation_lookup = "organisation"

    def perform_create(self, serializer):
        organisations = Organisation.objects.filter(membres__utilisateur=self.request.user)
        organisation = serializer.validated_data.get("organisation")
        if organisation not in organisations:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Organisation hors de votre périmètre.")
        serializer.save(auteur=self.request.user)

    @action(detail=True, methods=["post"])
    def valider(self, request, pk=None):
        """Confirme qu'une observation est fiable."""
        observation = self.get_object()
        observation.valide = True
        observation.save(update_fields=("valide",))
        return Response(self.get_serializer(observation).data, status=status.HTTP_200_OK)


class AnomalieViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Les anomalies sont créées UNIQUEMENT par anomaly_service."""

    queryset = Anomalie.objects.select_related("organisation", "resultat_metrique").order_by("-date_detection")
    serializer_class = AnomalieSerializer
    organisation_lookup = "organisation"


class HypotheseViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Hypothèses générées par l'IA (hypothesis_service)."""

    queryset = Hypothese.objects.select_related("anomalie__organisation").order_by("-date_creation")
    serializer_class = HypotheseSerializer
    organisation_lookup = "anomalie__organisation"

    @action(detail=True, methods=["post"])
    def confirmer(self, request, pk=None):
        """Confirme une hypothèse."""
        hypothese = self.get_object()
        confiance = request.data.get("confiance")
        if confiance is None:
            return Response({"error": "confiance est requise (0.0 à 1.0) pour confirmer une hypothèse."}, status=status.HTTP_400_BAD_REQUEST)

        hypothese.statut = Hypothese.Statut.CONFIRMEE
        hypothese.confiance = confiance
        hypothese.save(update_fields=("statut", "confiance"))
        return Response(self.get_serializer(hypothese).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def rejeter(self, request, pk=None):
        hypothese = self.get_object()
        hypothese.statut = Hypothese.Statut.REJETEE
        hypothese.save(update_fields=("statut",))
        return Response(self.get_serializer(hypothese).data, status=status.HTTP_200_OK)


class MemoireStrategiqueViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Créées uniquement par memory_service, à partir d'une Hypothese CONFIRMEE."""

    queryset = MemoireStrategique.objects.select_related("organisation").order_by("-date_creation")
    serializer_class = MemoireStrategiqueSerializer
    organisation_lookup = "organisation"


class DocumentEntrepriseViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """Documents propres à l'entreprise."""

    queryset = DocumentEntreprise.objects.select_related("organisation", "depose_par").order_by("-date_depot")
    serializer_class = DocumentEntrepriseSerializer
    organisation_lookup = "organisation"

    def perform_create(self, serializer):
        organisations = Organisation.objects.filter(membres__utilisateur=self.request.user)
        organisation = serializer.validated_data.get("organisation")
        if organisation not in organisations:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Organisation hors de votre périmètre.")
        serializer.save(depose_par=self.request.user)

    @action(detail=True, methods=["post"])
    def valider(self, request, pk=None):
        """Valide le document ET déclenche son indexation dans le RAG."""
        from .ai_client import indexer_document

        document = self.get_object()
        document.valider(request.user)

        resultat = indexer_document(
            organisation_id=document.organisation_id,
            document_id=document.id,
            texte=document.contenu_texte,
            source=f"document_entreprise_{document.type.lower()}",
            metadata={"titre": document.titre, "type": document.type},
        )
        if "_error" not in resultat:
            document.indexe_rag = True
            document.save(update_fields=("indexe_rag",))

        return Response(self.get_serializer(document).data, status=status.HTTP_200_OK)