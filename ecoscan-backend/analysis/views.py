import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import JournalAudit
from audit.services import enregistrer_evenement
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
    OpportuniteFinancement,
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
    OpportuniteFinancementSerializer,
    RecommandationSerializer,
    ResultatMetriqueSerializer,
)
from analysis.api.ai_client import indexer_document, interroger_assistant
from analysis.services.factures import analyser_facture_par_id
from analysis.services.progression_objectifs import calculer_progressions_organisation
from analysis.services.recommandations import (
    DonneesInvalidesError,
    EtatIncompatibleError,
    creer_recommandation_depuis_anomalie,
    lier_action_a_memoire,
    mesurer_impact_action,
)
from analysis.services.taches import lancer_en_arriere_plan
from analysis.services.memory_service import creer_memoire_depuis_hypothese
from analysis.services.registry import obtenir_definition
from analysis.services.report_service import generer_pdf_livrable

logger = logging.getLogger(__name__)

ROLES_ADMIN = ("ADMIN_ORGANISATION", "SUPER_ADMIN")
SIX_DECIMALS = Decimal('0.000001')
VALEUR_MAX = Decimal('999999999999')  # Sécurité sur max_digits=18, decimal_places=6
FOUR_DECIMALS = Decimal('0.0001')


def _nettoyer_et_convertir_decimal(valeur_brute):
    """
    Nettoie et extrait un nombre valide à partir d'une donnée brute OCR ou texte.
    Exemples gérés : "2 460", "246 kWh", "246,5", 246.0
    Ignore les numéros de SIRET/Compteur/Facture (> 10 chiffres d'affilée sans décimale).
    """
    if valeur_brute is None:
        return None
    
    val_str = str(valeur_brute).strip().replace('\xa0', '').replace(' ', '').replace(',', '.')

    # Ignorer les identifiants longs (ex: SIRET, N° Compteur >= 10 chiffres consécutifs)
    if re.search(r'\d{10,}', val_str):
        logger.warning(f"Champ OCR ignoré (ressemble à un ID/SIRET/N° Compteur) : {val_str}")
        return None

    match = re.search(r'[-+]?\d*\.?\d+', val_str)
    if not match:
        return None
        
    cleaned_str = match.group(0)
    
    try:
        dec = Decimal(cleaned_str)
        if abs(dec) > VALEUR_MAX:
            logger.warning(f"Valeur numérique rejetée car > VALEUR_MAX ({VALEUR_MAX}): {dec}")
            return None
        return dec.quantize(SIX_DECIMALS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return None


def _publier_resultat_depuis_import(import_instance):
    """Convertit une extraction d'import 'energy' validée en ResultatMetrique.

    Idempotent grâce à `fichier_source` (get_or_create). Après publication, la
    chaîne facture -> variation -> anomalie -> hypothèse est (re)jouée EN
    ARRIÈRE-PLAN.

    Returns:
        tuple[ResultatMetrique, bool]: le résultat et un booléen « vient d'être créé ».
    """
    definition = obtenir_definition("consommation_facture_periodique")
    champs = import_instance.donnees_extraites or {}
    
    # 1. Tester par ordre de précision les différentes clés du dictionnaire OCR
    candidats_consommation = [
        champs.get("consommation_kwh"),
        champs.get("consommation_totale"),
        champs.get("consommation"),
        champs.get("index_consommation"),
    ]

    valeur_decimal = None
    for candidat in candidats_consommation:
        if candidat is not None:
            valeur_decimal = _nettoyer_et_convertir_decimal(candidat)
            if valeur_decimal is not None:
                break

    periode_fin = None
    date_facture_str = champs.get("date_facture")
    if date_facture_str:
        try:
            periode_fin = timezone.make_aware(datetime.strptime(date_facture_str, "%d/%m/%Y"))
        except (ValueError, TypeError):
            periode_fin = None
    if periode_fin is None:
        periode_fin = import_instance.date_traitement or timezone.now()
    periode_debut = periode_fin - timedelta(days=30)

    limites = list(definition.limites)

    # 2. Calcul sécurisé de la confiance
    try:
        confiance = (
            Decimal(str(import_instance.score_qualite)) / Decimal("100")
            if import_instance.score_qualite is not None
            else None
        )
        if confiance is not None:
            if not confiance.is_finite():
                confiance = None
            else:
                confiance = confiance.quantize(FOUR_DECIMALS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        confiance = None

    # 3. Attribution dynamique de la complétude et du statut de qualité
    if valeur_decimal is not None and confiance is not None and confiance >= Decimal("0.75"):
        statut_qualite = ResultatMetrique.StatutQualite.FIABLE
        completude_decimal = Decimal("1.0000")
    elif valeur_decimal is not None:
        statut_qualite = ResultatMetrique.StatutQualite.ESTIME
        completude_decimal = Decimal("1.0000")
    else:
        statut_qualite = ResultatMetrique.StatutQualite.INSUFFISANT
        completude_decimal = Decimal("0.0000")

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
            "completude": completude_decimal,
            "statut_qualite": statut_qualite,
            "confiance": confiance,
            "sources": [import_instance.nom_fichier],
            "limites": limites,
        },
    )

    # Si l'enregistrement existait déjà sans valeur valide, on le met à jour
    if not cree and (resultat.valeur is None or resultat.valeur == Decimal("0")) and valeur_decimal is not None:
        resultat.valeur = valeur_decimal
        resultat.completude = completude_decimal
        resultat.statut_qualite = statut_qualite
        resultat.confiance = confiance
        resultat.save(update_fields=("valeur", "completude", "statut_qualite", "confiance"))

    if resultat.valeur is not None:
        lancer_en_arriere_plan(analyser_facture_par_id, resultat.id)
        
    return resultat, cree


class AnalyseScopedQuerySetMixin:
    """Filtrage strict multi-tenant. Le rôle SUPER_ADMIN n'a volontairement aucun
    accès aux objets métier des clients."""

    permission_classes = [IsAuthenticated]
    organisation_lookup = "recommandation__objectif__organisation"

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "role", None) == "SUPER_ADMIN":
            return self.queryset.none()
        organisations = Organisation.objects.filter(membres__utilisateur=user)
        return self.queryset.all().filter(**{f"{self.organisation_lookup}__in": organisations})

    def _exiger_perimetre(self, organisation):
        """Refuse la création d'un objet rattaché à une organisation dont
        l'utilisateur n'est pas membre."""
        if organisation is None or not Organisation.objects.filter(
            id=organisation.id, membres__utilisateur=self.request.user
        ).exists():
            raise PermissionDenied("Ressource hors de votre périmètre.")


class AnalyseScopedViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """CRUD complet scopé par tenant."""


class RecommandationViewSet(AnalyseScopedViewSet):
    """Cycle de vie des recommandations d'efficacité énergétique."""

    queryset = Recommandation.objects.select_related("objectif__organisation").order_by("-priorite", "date_echeance")
    serializer_class = RecommandationSerializer
    organisation_lookup = "objectif__organisation"

    def get_queryset(self):
        queryset = super().get_queryset()
        objectif_id = self.request.query_params.get("objectif")
        if objectif_id:
            queryset = queryset.filter(objectif_id=objectif_id)
        return queryset

    @action(detail=True, methods=["post"])
    def generer(self, request, pk=None):
        """Phase d'édition initiale. Audit : 'GENERER_RECOMMANDATION'."""
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
        """Adoption de la piste par l'organisation. Audit : 'MARQUER_DECIDEE_RECOMMANDATION'."""
        recommandation = self.get_object()
        if recommandation.statut == Recommandation.Statut.DECIDEE:
            return Response(self.get_serializer(recommandation).data, status=status.HTTP_200_OK)
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
    """Suivi opérationnel des actions techniques."""

    queryset = Action.objects.select_related("recommandation__objectif__organisation", "responsable").order_by("date_echeance")
    serializer_class = ActionSerializer
    organisation_lookup = "recommandation__objectif__organisation"

    def get_queryset(self):
        queryset = super().get_queryset()
        recommandation_id = self.request.query_params.get("recommandation")
        if recommandation_id:
            try:
                uuid.UUID(str(recommandation_id))
            except ValueError:
                return queryset.none()
            queryset = queryset.filter(recommandation_id=recommandation_id)
        return queryset

    @staticmethod
    def _organisation_de(action_obj):
        if action_obj.recommandation and action_obj.recommandation.objectif:
            return action_obj.recommandation.objectif.organisation
        return None

    def perform_create(self, serializer):
        recommandation = serializer.validated_data.get("recommandation")
        objectif = getattr(recommandation, "objectif", None)
        self._exiger_perimetre(getattr(objectif, "organisation", None))
        lier_action_a_memoire(serializer.save())

    @action(detail=True, methods=["post"], url_path="mesurer-impact")
    def mesurer_impact(self, request, pk=None):
        """Enregistre l'impact RÉEL (FCFA), saisi par une personne, d'une action
        terminée et le propage à la mémoire stratégique. Audit : 'MESURER_IMPACT_ACTION'."""
        action_obj = self.get_object()
        try:
            mesurer_impact_action(action_obj, request.data.get("economie_realisee_fcfa"))
        except DonneesInvalidesError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except EtatIncompatibleError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        enregistrer_evenement(
            action="MESURER_IMPACT_ACTION",
            ressource="Action",
            identifiant_ressource=str(action_obj.id),
            utilisateur=request.user,
            organisation=self._organisation_de(action_obj),
            details={"economie_realisee_fcfa": float(action_obj.economie_realisee_fcfa)},
            request=request,
        )
        return Response(self.get_serializer(action_obj).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def suivre(self, request, pk=None):
        """Passe l'action en exécution. Audit : 'SUIVRE_ACTION'."""
        action_obj = self.get_object()
        action_obj.suivre()

        enregistrer_evenement(
            action="SUIVRE_ACTION",
            ressource="Action",
            identifiant_ressource=str(action_obj.id),
            utilisateur=request.user,
            organisation=self._organisation_de(action_obj),
            request=request,
        )
        return Response(self.get_serializer(action_obj).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def cloturer(self, request, pk=None):
        """Enregistre l'achèvement de l'action. Audit : 'CLOTURER_ACTION'."""
        action_obj = self.get_object()
        action_obj.cloturer()

        enregistrer_evenement(
            action="CLOTURER_ACTION",
            ressource="Action",
            identifiant_ressource=str(action_obj.id),
            utilisateur=request.user,
            organisation=self._organisation_de(action_obj),
            request=request,
        )
        return Response(self.get_serializer(action_obj).data, status=status.HTTP_200_OK)


class DecisionViewSet(AnalyseScopedViewSet):
    """Arbitrages d'investissement."""

    queryset = Decision.objects.select_related("recommandation__objectif__organisation", "decideur").order_by("-date_decision")
    serializer_class = DecisionSerializer
    organisation_lookup = "recommandation__objectif__organisation"

    def perform_create(self, serializer):
        recommandation = serializer.validated_data.get("recommandation")
        objectif = getattr(recommandation, "objectif", None)
        organisation = getattr(objectif, "organisation", None)
        self._exiger_perimetre(organisation)
        decision = serializer.save(decideur=self.request.user, date_decision=timezone.now())

        enregistrer_evenement(
            action="CREER_DECISION",
            ressource="Decision",
            identifiant_ressource=str(decision.id),
            utilisateur=self.request.user,
            organisation=organisation,
            details={"resultat": decision.resultat},
            request=self.request,
        )


class LivrableViewSet(AnalyseScopedViewSet):
    """Rapports d'audit, certifications et livrables techniques."""

    queryset = Livrable.objects.select_related("organisation", "fiche_projet", "memoire").order_by("-date_generation")
    serializer_class = LivrableSerializer
    organisation_lookup = "organisation"

    @action(detail=True, methods=["post"])
    def generer(self, request, pk=None):
        """Génère le PDF puis fige la publication. Audit : 'GENERER_LIVRABLE'."""
        livrable = self.get_object()

        try:
            pdf_file = generer_pdf_livrable(livrable)
        except Exception as exc:
            logger.exception("Échec de la génération du PDF pour le livrable %s.", livrable.id)
            return Response(
                {"error": f"Échec de la génération du rapport : {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        chemin_stocke = default_storage.save(f"livrables/{livrable.id}/{pdf_file.name}", pdf_file)
        livrable.url_fichier = default_storage.url(chemin_stocke)
        livrable.save(update_fields=("url_fichier",))
        livrable.generer()

        organisation = livrable.organisation
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
        """Approuve la conformité du livrable. Audit : 'VALIDER_LIVRABLE'."""
        livrable = self.get_object()
        livrable.valider()

        organisation = livrable.organisation
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
    """Lecture seule : bilans énergétiques, baselines, indicateurs."""

    queryset = ResultatMetrique.objects.select_related("organisation", "compteur").order_by("-periode_fin")
    serializer_class = ResultatMetriqueSerializer
    organisation_lookup = "organisation"


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def integrer_extraction_energy(request):
    """Intègre une facture extraite par 'energy' sous forme de ResultatMetrique."""
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

    if import_instance.statut not in (ImportDonnees.Statut.TERMINE, ImportDonnees.Statut.REVUE_REQUISE):
        return Response(
            {
                "error": (
                    f"Import non publiable : statut actuel '{import_instance.statut}', "
                    f"'{ImportDonnees.Statut.TERMINE}' ou '{ImportDonnees.Statut.REVUE_REQUISE}' requis."
                )
            },
            status=status.HTTP_409_CONFLICT,
        )

    if import_instance.statut == ImportDonnees.Statut.REVUE_REQUISE:
        import_instance.statut = ImportDonnees.Statut.TERMINE
        import_instance.save(update_fields=("statut",))

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
    """Saisie et validation des constatations terrain."""

    queryset = ObservationOperationnelle.objects.select_related("organisation", "auteur").order_by("-date_observation")
    serializer_class = ObservationOperationnelleSerializer
    organisation_lookup = "organisation"

    def perform_create(self, serializer):
        organisation = serializer.validated_data.get("organisation")
        self._exiger_perimetre(organisation)
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
        """Marque l'observation comme vérifiée. Audit : 'VALIDER_OBSERVATION'."""
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
    """Lecture seule des dérives et anomalies détectées."""

    queryset = Anomalie.objects.select_related("organisation", "resultat_metrique").order_by("-date_detection")
    serializer_class = AnomalieSerializer
    organisation_lookup = "organisation"

    @action(detail=True, methods=["post"], url_path="creer-recommandation")
    def creer_recommandation(self, request, pk=None):
        """Geste humain : crée une recommandation à partir d'une anomalie. Audit : 'CREER_RECOMMANDATION'."""
        anomalie = self.get_object()
        try:
            recommandation = creer_recommandation_depuis_anomalie(anomalie, request.data)
        except DonneesInvalidesError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except EtatIncompatibleError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        enregistrer_evenement(
            action="CREER_RECOMMANDATION",
            ressource="Recommandation",
            identifiant_ressource=str(recommandation.id),
            utilisateur=request.user,
            organisation=anomalie.organisation,
            details={"anomalie_id": str(anomalie.id)},
            request=request,
        )
        return Response(RecommandationSerializer(recommandation).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="generer-recommandation-ia")
    def generer_recommandation_ia(self, request, pk=None):
        """Génération automatique de recommandation par IA pour cette anomalie."""
        anomalie = self.get_object()
        from analysis.services.auto_recommandation import generer_recommandation_auto
        reco = generer_recommandation_auto(anomalie)
        if reco is None:
            return Response({"error": "Impossible de générer une recommandation pour cette anomalie."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(RecommandationSerializer(reco).data, status=status.HTTP_201_CREATED)


class HypotheseViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Consultation et traitement des explications générées par IA."""

    queryset = Hypothese.objects.select_related("anomalie__organisation").order_by("-date_creation")
    serializer_class = HypotheseSerializer
    organisation_lookup = "anomalie__organisation"

    @action(detail=True, methods=["post"])
    def confirmer(self, request, pk=None):
        """Confirme une hypothèse PROPOSEE et la convertit en mémoire stratégique."""
        hypothese = self.get_object()
        if hypothese.statut != Hypothese.Statut.PROPOSEE:
            return Response(
                {"error": f"Seule une hypothèse PROPOSEE peut être confirmée (statut actuel : {hypothese.statut})."},
                status=status.HTTP_409_CONFLICT,
            )

        confiance_brute = request.data.get("confiance")
        if confiance_brute is None:
            return Response({"error": "confiance est requise (0.0 à 1.0) pour confirmer une hypothèse."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            confiance = Decimal(str(confiance_brute))
        except InvalidOperation:
            return Response({"error": "confiance doit être un nombre entre 0.0 et 1.0."}, status=status.HTTP_400_BAD_REQUEST)
        if not (Decimal("0") <= confiance <= Decimal("1")):
            return Response({"error": "confiance doit être comprise entre 0.0 et 1.0."}, status=status.HTTP_400_BAD_REQUEST)

        hypothese.statut = Hypothese.Statut.CONFIRMEE
        hypothese.confiance = confiance
        hypothese.save(update_fields=("statut", "confiance"))

        anomalie = hypothese.anomalie
        if anomalie and anomalie.statut in (Anomalie.Statut.DETECTED, Anomalie.Statut.NEEDS_CONTEXT):
            anomalie.statut = Anomalie.Statut.CONFIRMED
            anomalie.save(update_fields=("statut",))

        creer_memoire_depuis_hypothese(hypothese)

        # Génération automatique de la recommandation si non encore créée
        if anomalie:
            from analysis.services.auto_recommandation import generer_recommandation_auto
            try:
                generer_recommandation_auto(anomalie)
            except Exception:
                pass

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
        """Invalide une hypothèse générée par l'IA. Audit : 'REJETER_HYPOTHESE'."""
        hypothese = self.get_object()
        if hypothese.statut != Hypothese.Statut.PROPOSEE:
            return Response(
                {"error": f"Seule une hypothèse PROPOSEE peut être rejetée (statut actuel : {hypothese.statut})."},
                status=status.HTTP_409_CONFLICT,
            )
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


class ProgressionObjectifsView(APIView):
    """Progression DÉCLARÉE et MESURÉE de chaque objectif."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if getattr(request.user, "role", None) == "SUPER_ADMIN":
            return Response({"error": "Rôle non autorisé."}, status=status.HTTP_403_FORBIDDEN)
        resultats = []
        for organisation in Organisation.objects.filter(membres__utilisateur=request.user):
            resultats.extend(calculer_progressions_organisation(organisation))
        return Response(resultats)


class MemoireStrategiqueViewSet(AnalyseScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Lecture seule de la base de connaissances stratégiques."""

    queryset = MemoireStrategique.objects.select_related("organisation").order_by("-date_creation")
    serializer_class = MemoireStrategiqueSerializer
    organisation_lookup = "organisation"


class DocumentEntrepriseViewSet(AnalyseScopedQuerySetMixin, viewsets.ModelViewSet):
    """Gestion documentaire technique de l'entreprise."""

    queryset = DocumentEntreprise.objects.select_related("organisation", "depose_par").order_by("-date_depot")
    serializer_class = DocumentEntrepriseSerializer
    organisation_lookup = "organisation"

    def perform_create(self, serializer):
        organisation = serializer.validated_data.get("organisation")
        self._exiger_perimetre(organisation)
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
        """Approuve le document et l'indexe dans le RAG. Audit : 'VALIDER_DOCUMENT'."""
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
        else:
            logger.warning("Document %s validé mais non indexé : %s", document.id, resultat["_error"])

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


def _nettoyer_historique(brut) -> list:
    """Historique fourni par le front : 6 derniers échanges, rôles et taille bornés."""
    if not isinstance(brut, list):
        return []
    historique = []
    for item in brut[-6:]:
        if not isinstance(item, dict):
            continue
        role = "assistant" if item.get("role") == "assistant" else "user"
        contenu = str(item.get("content") or "").strip()[:1500]
        if contenu:
            historique.append({"role": role, "content": contenu})
    return historique


class AssistantQueryView(APIView):
    """Requêtes conversationnelles adressées à l'assistant IA."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        question = (request.data.get("question") or "").strip()
        if not question:
            return Response({"error": "La question ne peut pas être vide."}, status=status.HTTP_400_BAD_REQUEST)

        if getattr(request.user, "role", None) == "SUPER_ADMIN":
            return Response({"error": "L'assistant n'est pas disponible pour ce rôle."}, status=status.HTTP_403_FORBIDDEN)

        organisation = Organisation.objects.filter(membres__utilisateur=request.user).first()
        if organisation is None:
            return Response({"error": "Aucune organisation associée à ce compte."}, status=status.HTTP_409_CONFLICT)

        reponse = interroger_assistant(
            question,
            organisation.id,
            historique=_nettoyer_historique(request.data.get("historique")),
        )

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
            return Response({"error": reponse["_error"]}, status=status.HTTP_53_SERVICE_UNAVAILABLE)

        return Response(
            {"answer": reponse.get("answer", "Je n'ai pas pu formuler de réponse."), "sources": reponse.get("sources", [])},
            status=status.HTTP_200_OK,
        )


class OpportuniteFinancementViewSet(viewsets.ReadOnlyModelViewSet):
    """Consultation des opportunités et subventions."""

    serializer_class = OpportuniteFinancementSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        statut_demande = self.request.query_params.get("statut")
        if statut_demande == "A_VERIFIER":
            if getattr(self.request.user, "role", None) not in ROLES_ADMIN:
                return OpportuniteFinancement.objects.none()
            return OpportuniteFinancement.objects.filter(statut="A_VERIFIER")
        return OpportuniteFinancement.objects.filter(statut="ACTIF")

    @action(detail=True, methods=["post"], url_path="valider")
    def valider(self, request, pk=None):
        """Publie une opportunité en attente de relecture."""
        if getattr(request.user, "role", None) not in ROLES_ADMIN:
            return Response({"error": "Rôle non autorisé pour cette action."}, status=status.HTTP_403_FORBIDDEN)
        opportunite = get_object_or_404(OpportuniteFinancement, pk=pk)
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
        """Passe une opportunité à l'état expiré/rejeté."""
        if getattr(request.user, "role", None) not in ROLES_ADMIN:
            return Response({"error": "Rôle non autorisé pour cette action."}, status=status.HTTP_403_FORBIDDEN)
        opportunite = get_object_or_404(OpportuniteFinancement, pk=pk)
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
    """Webhook recevant des flux d'opportunités financières automatisés."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        attendu = getattr(settings, "N8N_INGESTION_TOKEN", "") or ""
        recu = request.headers.get("X-N8N-Ingestion-Token", "")
        if not attendu or not secrets.compare_digest(recu, attendu):
            return Response({"error": "Jeton invalide."}, status=status.HTTP_401_UNAUTHORIZED)

        items = request.data if isinstance(request.data, list) else [request.data]
        crees, maj, ignores = 0, 0, 0
        for item in items:
            if not isinstance(item, dict) or not item.get("titre") or not item.get("organisme"):
                ignores += 1
                continue
            try:
                confiance = float(item["confiance_extraction"]) if item.get("confiance_extraction") is not None else None
            except (TypeError, ValueError):
                confiance = None
            statut_calcule = (
                "ACTIF" if confiance is not None and confiance >= SEUIL_CONFIANCE_AUTO_PUBLICATION else "A_VERIFIER"
            )
            _obj, created = OpportuniteFinancement.objects.update_or_create(
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
            details={"crees": crees, "mis_a_jour": maj, "ignores": ignores},
            request=request,
        )
        return Response({"crees": crees, "mis_a_jour": maj, "ignores": ignores}, status=status.HTTP_200_OK)