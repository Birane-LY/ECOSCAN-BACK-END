import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.views import APIView   
from django.conf import settings
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from energy.models import ImportDonnees
from organizations.models import Organisation
from audit.models import  JournalAudit
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
    OpportuniteFinancement,
   
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
    OpportuniteFinancementSerializer
)
from analysis.api.ai_client import indexer_document
from analysis.api.ai_client import interroger_assistant
from analysis.services.registry import obtenir_definition
from analysis.services.memory_service import creer_memoire_depuis_hypothese
from audit.services import enregistrer_evenement

logger = logging.getLogger(__name__)


def _verifier_puissance_souscrite(import_instance, champs: dict) -> None:
    """Compare la puissance souscrite lue sur une facture avec celle du compteur.

    Cette fonction d'assistance permet de détecter les écarts de souscription sans
    altérer automatiquement les données. Les observations sont jointes aux limites
    du résultat métrique pour vérification humaine.

    Args:
        import_instance (ImportDonnees): L'instance d'import de données concernée.
        champs (dict): Le dictionnaire d'extractions contenant les valeurs de puissance.

    Returns:
        Optional[str]: Un message explicatif si une puissance est présente, sinon None.
    """
    puissance_facture = champs.get("puissance_souscrite") or champs.get("puissance_transfo")
    if puissance_facture is None:
        return None
    try:
        puissance_facture = Decimal(str(puissance_facture))
    except InvalidOperation:
        return None

    return (
        f"Puissance souscrite lue sur la facture : {puissance_facture} kVA — "
        f"à comparer manuellement avec la valeur enregistrée sur le compteur concerné."
    )


def _publier_resultat_depuis_import(import_instance):
    """Convertit une extraction d'import 'energy' validée en un résultat métrique.

    Assure l'idempotence des calculs grâce à la contrainte unique sur `fichier_source`.
    Calcule la période temporelle, le statut de qualité en fonction du score de
    confiance et agrège les limites métier de la définition d'analyse.

    Args:
        import_instance (ImportDonnees): L'instance d'import traitée (statut TERMINE).

    Returns:
        tuple[ResultatMetrique, bool]: L'objet ResultatMetrique créé ou récupéré, et
        un booléen indiquant si l'objet vient d'être créé.
    """
    definition = obtenir_definition("consommation_facture_periodique")
    champs = import_instance.donnees_extraites or {}
    consommation = champs.get("consommation_kwh")

    date_facture_str = champs.get("date_facture")
    periode_fin = None
    if date_facture_str:
        try:
            periode_fin = datetime.strptime(date_facture_str, "%d/%m/%Y").replace(
                tzinfo=timezone.get_current_timezone()
            )
        except (ValueError, TypeError):
            periode_fin = None
    if periode_fin is None:
        periode_fin = import_instance.date_traitement or timezone.now()
    periode_debut = periode_fin - timedelta(days=30)

    limites = list(definition.limites)

    valeur_decimal = None
    if consommation is not None:
        try:
            valeur_decimal = Decimal(str(consommation))
        except InvalidOperation:
            valeur_decimal = None

    confiance = (
        import_instance.score_qualite / Decimal("100")
        if import_instance.score_qualite is not None
        else None
    )

    if valeur_decimal is not None and confiance is not None and confiance >= Decimal("0.8"):
        statut_qualite = ResultatMetrique.StatutQualite.FIABLE
    elif valeur_decimal is not None:
        statut_qualite = ResultatMetrique.StatutQualite.ESTIME
    else:
        statut_qualite = ResultatMetrique.StatutQualite.INSUFFISANT

    puissance_facture = champs.get("puissance_souscrite") or champs.get("puissance_transfo")
    if puissance_facture is not None:
        limites.append(
            f"Puissance souscrite lue sur la facture : {puissance_facture} kVA — "
            f"à comparer manuellement avec la valeur enregistrée sur le compteur."
        )

    resultat, cree = ResultatMetrique.objects.get_or_create(
        fichier_source=import_instance.fichier_source,
        defaults={
            "organisation": import_instance.organisation,
            "compteur": import_instance.compteur,
            "code_metrique": definition.code,
            "version_metrique": definition.version,
            "valeur": valeur_decimal,
            "unite": definition.unite,
            "periode_debut": periode_debut,
            "periode_fin": periode_fin,
            "completude": Decimal("1.0") if valeur_decimal is not None else Decimal("0.0"),
            "statut_qualite": statut_qualite,
            "confiance": confiance,
            "sources": [import_instance.nom_fichier],
            "limites": limites,
        },
    )
    return resultat, cree


class AnalyseScopedQuerySetMixin:
    """Mixin pour le filtrage strict multi-tenant des données d'analyse.

    Garantit le cloisonnement des objets selon les organisations de l'utilisateur.
    Le rôle `SUPER_ADMIN` est explicitement restreint d'accès aux objets métier
    afin de préserver la souveraineté et la confidentialité des données clients.
    """

    permission_classes = [IsAuthenticated]
    organisation_lookup = "recommandation__objectif__organisation"

    def get_queryset(self):
        """Restreint le QuerySet aux seules organisations dont l'utilisateur est membre.

        Returns:
            QuerySet: Le jeu de données filtré pour le tenant courant ou vide si SUPER_ADMIN.
        """
        queryset = self.queryset
        user = self.request.user

        if getattr(user, "role", None) == "SUPER_ADMIN":
            return queryset.none()

        organisations = Organisation.objects.filter(membres__utilisateur=user)
        return queryset.filter(**{f"{self.organisation_lookup}__in": organisations})


class AnalyseScopedViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """Classe de base pour les ViewSets nécessitant un accès CRUD complet et scopé par tenant."""


class RecommandationViewSet(AnalyseScopedViewSet):
    """ViewSet pour la gestion du cycle de vie des recommandations d'efficacité énergétique."""

    queryset = Recommandation.objects.select_related("objectif__organisation").order_by("-priorite", "date_echeance")
    serializer_class = RecommandationSerializer
    organisation_lookup = "objectif__organisation"

    def get_queryset(self):
        """Permet d'ajouter un filtre optionnel par objectif via les query params."""
        queryset = super().get_queryset()
        objectif_id = self.request.query_params.get("objectif")
        if objectif_id:
            queryset = queryset.filter(objectif_id=objectif_id)
        return queryset
    
    @action(detail=True, methods=["post"])
    def generer(self, request, pk=None):
        """Déclenche la phase d'édition initiale de la recommandation.

        Action traçée dans le journal d'audit sous la clé 'GENERER_RECOMMANDATION'.
        """
        recommandation = self.get_object()
        recommandation.generer()

        enregistrer_evenement(
            action="GENERER_RECOMMANDATION",
            ressource="Recommandation",
            identifiant_ressource=str(recommandation.id),
            utilisateur=request.user,
            organisation=getattr(recommandation.objectif, "organisation", None),
            request=request,
        )

        return Response(self.get_serializer(recommandation).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="marquer-comme-decidee")
    def marquer_comme_decidee(self, request, pk=None):
        """Valide l'adoption stratégique de la piste d'amélioration par l'organisation.

        Action traçée dans le journal d'audit sous la clé 'MARQUER_DECIDEE_RECOMMANDATION'.
        """
        recommandation = self.get_object()
        recommandation.marquer_comme_decidee()

        enregistrer_evenement(
            action="MARQUER_DECIDEE_RECOMMANDATION",
            ressource="Recommandation",
            identifiant_ressource=str(recommandation.id),
            utilisateur=request.user,
            organisation=getattr(recommandation.objectif, "organisation", None),
            request=request,
        )

        return Response(self.get_serializer(recommandation).data, status=status.HTTP_200_OK)


class ActionViewSet(AnalyseScopedViewSet):
    """ViewSet pour le suivi opérationnel et l'exécution des actions techniques."""

    queryset = Action.objects.select_related("recommandation__objectif__organisation", "responsable").order_by("date_echeance")
    serializer_class = ActionSerializer
    organisation_lookup = "recommandation__objectif__organisation"

    @action(detail=True, methods=["post"])
    def suivre(self, request, pk=None):
        """Passe l'action dans l'état opérationnel d'exécution.

        Action traçée dans le journal d'audit sous la clé 'SUIVRE_ACTION'.
        """
        action_obj = self.get_object()
        action_obj.suivre()

        organisation = None
        if action_obj.recommandation and action_obj.recommandation.objectif:
            organisation = action_obj.recommandation.objectif.organisation

        enregistrer_evenement(
            action="SUIVRE_ACTION",
            ressource="Action",
            identifiant_ressource=str(action_obj.id),
            utilisateur=request.user,
            organisation=organisation,
            request=request,
        )

        return Response(self.get_serializer(action_obj).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def cloturer(self, request, pk=None):
        """Enregistre l'achèvement complet de l'action terrain.

        Action traçée dans le journal d'audit sous la clé 'CLOTURER_ACTION'.
        """
        action_obj = self.get_object()
        action_obj.cloturer()

        organisation = None
        if action_obj.recommandation and action_obj.recommandation.objectif:
            organisation = action_obj.recommandation.objectif.organisation

        enregistrer_evenement(
            action="CLOTURER_ACTION",
            ressource="Action",
            identifiant_ressource=str(action_obj.id),
            utilisateur=request.user,
            organisation=organisation,
            request=request,
        )

        return Response(self.get_serializer(action_obj).data, status=status.HTTP_200_OK)


class DecisionViewSet(AnalyseScopedViewSet):
    """ViewSet pour la formalisation et l'historisation des arbitrages d'investissement."""

    queryset = Decision.objects.select_related("recommandation__objectif__organisation", "decideur").order_by("-date_decision")
    serializer_class = DecisionSerializer
    organisation_lookup = "recommandation__objectif__organisation"

    def perform_create(self, serializer):
        """Renseigne automatiquement l'utilisateur connecté comme décideur et consigne l'audit."""
        decision = serializer.save(decideur=self.request.user, date_decision=timezone.now())

        organisation = None
        if decision.recommandation and decision.recommandation.objectif:
            organisation = decision.recommandation.objectif.organisation

        enregistrer_evenement(
            action="CREER_DECISION",
            ressource="Decision",
            identifiant_ressource=str(decision.id),
            utilisateur=self.request.user,
            organisation=organisation,
            details={"type_decision": decision.type_decision if hasattr(decision, "type_decision") else ""},
            request=self.request,
        )


class LivrableViewSet(AnalyseScopedViewSet):
    """ViewSet pour la gestion des rapports d'audit, certifications et livrables techniques."""

    queryset = Livrable.objects.select_related("fiche_projet__organisation").order_by("-date_generation")
    serializer_class = LivrableSerializer
    organisation_lookup = "fiche_projet__organisation"

    @action(detail=True, methods=["post"])
    def generer(self, request, pk=None):
        """Fige la publication formelle du livrable.

        Action traçée dans le journal d'audit sous la clé 'GENERER_LIVRABLE'.
        """
        livrable = self.get_object()
        livrable.generer()

        organisation = livrable.fiche_projet.organisation if livrable.fiche_projet else None

        enregistrer_evenement(
            action="GENERER_LIVRABLE",
            ressource="Livrable",
            identifiant_ressource=str(livrable.id),
            utilisateur=request.user,
            organisation=organisation,
            request=request,
        )

        return Response(self.get_serializer(livrable).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def valider(self, request, pk=None):
        """Approuve la conformité réglementaire et technique du livrable.

        Action traçée dans le journal d'audit sous la clé 'VALIDER_LIVRABLE'.
        """
        livrable = self.get_object()
        livrable.valider()

        organisation = livrable.fiche_projet.organisation if livrable.fiche_projet else None

        enregistrer_evenement(
            action="VALIDER_LIVRABLE",
            ressource="Livrable",
            identifiant_ressource=str(livrable.id),
            utilisateur=request.user,
            organisation=organisation,
            request=request,
        )

        return Response(self.get_serializer(livrable).data, status=status.HTTP_200_OK)


class ResultatMetriqueViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """ViewSet en lecture seule exposant les bilans énergétiques, baselines et indicateurs carbone."""

    queryset = ResultatMetrique.objects.select_related("organisation", "compteur").order_by("-periode_fin")
    serializer_class = ResultatMetriqueSerializer
    organisation_lookup = "organisation"


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def integrer_extraction_energy(request):
    """Intègre une facture extraite depuis le module 'energy' sous forme de résultat métrique.

    Exige un identifiant d'import valide et au statut TERMINE. Vérifie les droits
    d'accès de l'utilisateur sur l'organisation concernée.

    Returns:
        Response: Le ResultatMetrique créé/mis à jour (HTTP 201 ou 200), ou une erreur HTTP.
    """
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

    resultat, created = _publier_resultat_depuis_import(import_instance)

    enregistrer_evenement(
        action="INTEGRER_EXTRACTION_ENERGY",
        ressource="ResultatMetrique",
        identifiant_ressource=str(resultat.id),
        utilisateur=request.user,
        organisation=import_instance.organisation,
        details={
            "import_id": str(import_instance.id),
            "created": created,
            "code_metrique": resultat.code_metrique,
        },
        request=request,
    )

    statut_http = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return Response(ResultatMetriqueSerializer(resultat).data, status=statut_http)


class ObservationOperationnelleViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """ViewSet pour la saisie et la validation des constatations terrain."""

    queryset = ObservationOperationnelle.objects.select_related("organisation", "auteur").order_by("-date_observation")
    serializer_class = ObservationOperationnelleSerializer
    organisation_lookup = "organisation"

    def perform_create(self, serializer):
        """Associe l'auteur connecté et vérifie l'appartenance à l'organisation."""
        organisations = Organisation.objects.filter(membres__utilisateur=self.request.user)
        organisation = serializer.validated_data.get("organisation")
        if organisation not in organisations:
            raise PermissionDenied("Organisation hors de votre périmètre.")
        observation = serializer.save(auteur=self.request.user)

        enregistrer_evenement(
            action="CREER_OBSERVATION",
            ressource="ObservationOperationnelle",
            identifiant_ressource=str(observation.id),
            utilisateur=self.request.user,
            organisation=organisation,
            request=self.request,
        )

    @action(detail=True, methods=["post"])
    def valider(self, request, pk=None):
        """Marque une observation terrain comme vérifiée et exacte.

        Action traçée dans le journal d'audit sous la clé 'VALIDER_OBSERVATION'.
        """
        observation = self.get_object()
        observation.valide = True
        observation.save(update_fields=("valide",))

        enregistrer_evenement(
            action="VALIDER_OBSERVATION",
            ressource="ObservationOperationnelle",
            identifiant_ressource=str(observation.id),
            utilisateur=request.user,
            organisation=observation.organisation,
            request=request,
        )

        return Response(self.get_serializer(observation).data, status=status.HTTP_200_OK)


class AnomalieViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """ViewSet en lecture seule pour la consultation des dérives et anomalies détectées."""

    queryset = Anomalie.objects.select_related("organisation", "resultat_metrique").order_by("-date_detection")
    serializer_class = AnomalieSerializer
    organisation_lookup = "organisation"


class HypotheseViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """ViewSet pour la consultation et le traitement des explications générées par IA."""

    queryset = Hypothese.objects.select_related("anomalie__organisation").order_by("-date_creation")
    serializer_class = HypotheseSerializer
    organisation_lookup = "anomalie__organisation"

    @action(detail=True, methods=["post"])
    def confirmer(self, request, pk=None):
        """Confirme une hypothèse explicative et déclenche sa conversion en mémoire stratégique.

        Requires:
            confiance (float): Le niveau de confiance validé par l'expert (0.0 à 1.0).
        """
        hypothese = self.get_object()
        confiance = request.data.get("confiance")
        if confiance is None:
            return Response({"error": "confiance est requise (0.0 à 1.0) pour confirmer une hypothèse."}, status=status.HTTP_400_BAD_REQUEST)

        hypothese.statut = Hypothese.Statut.CONFIRMEE
        hypothese.confiance = confiance
        hypothese.save(update_fields=("statut", "confiance"))

        creer_memoire_depuis_hypothese(hypothese)

        organisation = hypothese.anomalie.organisation if hypothese.anomalie else None

        enregistrer_evenement(
            action="CONFIRMER_HYPOTHESE",
            ressource="Hypothese",
            identifiant_ressource=str(hypothese.id),
            utilisateur=request.user,
            organisation=organisation,
            details={"confiance": float(confiance)},
            request=request,
        )

        return Response(self.get_serializer(hypothese).data, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=["post"])
    def rejeter(self, request, pk=None):
        """Invalide une hypothèse générée par l'IA.

        Action traçée dans le journal d'audit sous la clé 'REJETER_HYPOTHESE'.
        """
        hypothese = self.get_object()
        hypothese.statut = Hypothese.Statut.REJETEE
        hypothese.save(update_fields=("statut",))

        organisation = hypothese.anomalie.organisation if hypothese.anomalie else None

        enregistrer_evenement(
            action="REJETER_HYPOTHESE",
            ressource="Hypothese",
            identifiant_ressource=str(hypothese.id),
            utilisateur=request.user,
            organisation=organisation,
            request=request,
        )

        return Response(self.get_serializer(hypothese).data, status=status.HTTP_200_OK)


class MemoireStrategiqueViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """ViewSet en lecture seule exposant la base de connaissances stratégiques consolidée."""

    queryset = MemoireStrategique.objects.select_related("organisation").order_by("-date_creation")
    serializer_class = MemoireStrategiqueSerializer
    organisation_lookup = "organisation"


class DocumentEntrepriseViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """ViewSet pour la gestion documentaire technique de l'entreprise."""

    queryset = DocumentEntreprise.objects.select_related("organisation", "depose_par").order_by("-date_depot")
    serializer_class = DocumentEntrepriseSerializer
    organisation_lookup = "organisation"

    def perform_create(self, serializer):
        """Enregistre le document avec l'utilisateur déposant et journalise l'action."""
        organisations = Organisation.objects.filter(membres__utilisateur=self.request.user)
        organisation = serializer.validated_data.get("organisation")
        if organisation not in organisations:
            raise PermissionDenied("Organisation hors de votre périmètre.")
        doc = serializer.save(depose_par=self.request.user)

        enregistrer_evenement(
            action="DEPOSER_DOCUMENT",
            ressource="DocumentEntreprise",
            identifiant_ressource=str(doc.id),
            utilisateur=self.request.user,
            organisation=organisation,
            request=self.request,
        )

    @action(detail=True, methods=["post"])
    def valider(self, request, pk=None):
        """Approuve le document et déclenche son indexation vectorielle dans le moteur RAG.

        Action traçée dans le journal d'audit avec l'état du flag 'indexe_rag'.
        """
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

        enregistrer_evenement(
            action="VALIDER_DOCUMENT",
            ressource="DocumentEntreprise",
            identifiant_ressource=str(document.id),
            utilisateur=request.user,
            organisation=document.organisation,
            details={"indexe_rag": document.indexe_rag},
            request=request,
        )

        return Response(self.get_serializer(document).data, status=status.HTTP_200_OK)


class AssistantQueryView(APIView):
    """API Endpoint pour les requêtes conversationnelles adressées à l'assistant IA."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Interroge l'assistant RAG avec la question utilisateur contextualisée par l'organisation.

        Returns:
            Response: La réponse générée et les sources documentaires, ou une erreur HTTP.
        """
        question = (request.data.get("question") or "").strip()
        if not question:
            return Response({"error": "La question ne peut pas être vide."}, status=status.HTTP_400_BAD_REQUEST)

        if getattr(request.user, "role", None) == "SUPER_ADMIN":
            return Response(
                {"error": "L'assistant n'est pas disponible pour ce rôle."},
                status=status.HTTP_403_FORBIDDEN,
            )

        organisation = Organisation.objects.filter(membres__utilisateur=request.user).first()
        if organisation is None:
            return Response(
                {"error": "Aucune organisation associée à ce compte."},
                status=status.HTTP_409_CONFLICT,
            )

        reponse = interroger_assistant(question, organisation.id)

        enregistrer_evenement(
            action="INTERROGER_ASSISTANT",
            ressource="Assistant",
            utilisateur=request.user,
            organisation=organisation,
            resultat=JournalAudit.Resultat.ECHEC if "_error" in reponse else JournalAudit.Resultat.SUCCES,
            details={"question_longueur": len(question)},
            request=request,
        )

        if "_error" in reponse:
            return Response({"error": reponse["_error"]}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(
            {"answer": reponse.get("answer", "Je n'ai pas pu formuler de réponse."), "sources": reponse.get("sources", [])},
            status=status.HTTP_200_OK,
        )


class OpportuniteFinancementViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet en lecture pour la consultation des opportunités et subventions d'investissements."""

    serializer_class = OpportuniteFinancementSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filtre les opportunités par statut ('ACTIF' par défaut, 'A_VERIFIER' pour les admins)."""
        statut_demande = self.request.query_params.get("statut")
        if statut_demande == "A_VERIFIER":
            if getattr(self.request.user, "role", None) not in ("ADMIN_ORGANISATION", "SUPER_ADMIN"):
                return OpportuniteFinancement.objects.none()
            return OpportuniteFinancement.objects.filter(statut="A_VERIFIER")
        return OpportuniteFinancement.objects.filter(statut="ACTIF")

    @action(detail=True, methods=["post"], url_path="valider")
    def valider(self, request, pk=None):
        """Approuve une opportunité de financement pour publication au catalogue public."""
        if getattr(request.user, "role", None) not in ("ADMIN_ORGANISATION", "SUPER_ADMIN"):
            return Response({"error": "Rôle non autorisé pour cette action."}, status=status.HTTP_403_FORBIDDEN)
        opportunite = self.get_object()
        if opportunite.statut != "A_VERIFIER":
            return Response({"error": "Cette opportunité n'est pas en attente de relecture."}, status=status.HTTP_409_CONFLICT)
        opportunite.statut = "ACTIF"
        opportunite.save(update_fields=("statut",))

        enregistrer_evenement(
            action="VALIDER_OPPORTUNITE",
            ressource="OpportuniteFinancement",
            identifiant_ressource=str(opportunite.id),
            utilisateur=request.user,
            request=request,
        )

        return Response(self.get_serializer(opportunite).data)

    @action(detail=True, methods=["post"], url_path="rejeter")
    def rejeter(self, request, pk=None):
        """Passe une opportunité de financement à l'état expiré/rejeté."""
        if getattr(request.user, "role", None) not in ("ADMIN_ORGANISATION", "SUPER_ADMIN"):
            return Response({"error": "Rôle non autorisé pour cette action."}, status=status.HTTP_403_FORBIDDEN)
        opportunite = self.get_object()
        opportunite.statut = "EXPIRE"
        opportunite.save(update_fields=("statut",))

        enregistrer_evenement(
            action="REJETER_OPPORTUNITE",
            ressource="OpportuniteFinancement",
            identifiant_ressource=str(opportunite.id),
            utilisateur=request.user,
            request=request,
        )

        return Response(self.get_serializer(opportunite).data)


SEUIL_CONFIANCE_AUTO_PUBLICATION = 0.75


class OpportuniteFinancementIngestionView(APIView):
    """Endpoint Webhook recevant des flux d'opportunités financières automatisés (ex: n8n)."""

    permission_classes = [AllowAny]

    def post(self, request):
        """Traite l'ingestion par lots et applique le seuil de confiance pour l'auto-publication.

        Headers:
            X-N8N-Ingestion-Token: Jeton de sécurité pour valider la provenance de la requête.

        Returns:
            Response: Un bilan du nombre d'éléments créés et mis à jour.
        """
        token = request.headers.get("X-N8N-Ingestion-Token")
        if token != getattr(settings, "N8N_INGESTION_TOKEN", None):
            return Response({"error": "Jeton invalide."}, status=status.HTTP_401_UNAUTHORIZED)

        items = request.data if isinstance(request.data, list) else [request.data]
        crees, maj = 0, 0
        for item in items:
            confiance = item.get("confiance_extraction")
            statut_calcule = (
                "ACTIF" if confiance is not None and float(confiance) >= SEUIL_CONFIANCE_AUTO_PUBLICATION
                else "A_VERIFIER"
            )
            obj, created = OpportuniteFinancement.objects.update_or_create(
                titre=item.get("titre"), organisme=item.get("organisme"),
                defaults={
                    "description": item.get("description", ""),
                    "montant_max": item.get("montant_max"),
                    "devise": item.get("devise", "FCFA"),
                    "taux_financement_pct": item.get("taux_financement_pct"),
                    "criteres_eligibilite": item.get("criteres_eligibilite", ""),
                    "secteur": item.get("secteur", ""),
                    "date_limite": item.get("date_limite"),
                    "url_source": item.get("url_source", ""),
                    "statut": statut_calcule,
                },
            )
            crees += 1 if created else 0
            maj += 0 if created else 1

        enregistrer_evenement(
            action="INGESTION_N8N_OPPORTUNITES",
            ressource="OpportuniteFinancement",
            details={"crees": crees, "mis_a_jour": maj},
            request=request,
        )

        return Response({"crees": crees, "mis_a_jour": maj}, status=status.HTTP_200_OK)