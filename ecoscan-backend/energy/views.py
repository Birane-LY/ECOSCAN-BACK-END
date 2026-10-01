import hashlib
import json
import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import mixins, permissions, status, viewsets, generics
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser

from organizations.models import Organisation, UtilisateurOrganisation, Compteur

from .models import (
    DonneeEnergetique,
    FacteurEmission,
    FichierSource,
    HistoriquePerformance,
    Indicateur,
    IndicateurObjectif,
    ImportDonnees,
    Objectif,
    SourceDonnee,
    SyntheseFinanciere,
    AchatWoyofal,
    ReleveSolde,
    PointSuiviEnergetique,
    ReleveRituelEnergetique,
    RechargeRituelWoyofal,
)
from .serializers import (
    DonneeEnergetiqueSerializer,
    FacteurEmissionSerializer,
    FichierSourceSerializer,
    HistoriquePerformanceSerializer,
    ImportDonneesSerializer,
    IndicateurObjectifSerializer,
    IndicateurSerializer,
    ObjectifSerializer,
    SourceDonneeSerializer,
    SyntheseFinanciereSerializer,
    AchatWoyofalSerializer,
    ReleveSoldeSerializer,
    PointSuiviEnergetiqueSerializer,
    ReleveRituelEnergetiqueSerializer,
    RechargeRituelWoyofalSerializer,
)
from .services.hashing import calculer_hash_fichier
from .services.services import OCRService
from .services.ai_client import analyser_image, transcrire_audio
from .services.ocr import OCRServiceError
from .services.prediction_tranches import etat_tranche, predire_kwh
from .services.autonomie import estimer_autonomie
from audit.services import enregistrer_evenement 
from billing.permissions import EstAbonnementActif
from billing.services import BillingAccessService

logger = logging.getLogger(__name__)


class OrganisationScopedQuerySetMixin:
    """Filtrage multi-tenant commun à tous les ViewSets de l'app energy."""

    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    organisation_lookup = "organisation"

    def get_queryset(self):
        queryset = self.queryset
        user = self.request.user

        if getattr(user, "role", None) == "SUPER_ADMIN":
            return queryset.none()

        organisations = BillingAccessService().organisations_avec_acces(user)
        return queryset.filter(**{f"{self.organisation_lookup}__in": organisations})

    def _organisations_de_lutilisateur(self):
        return BillingAccessService().organisations_avec_acces(self.request.user)


class FichierSourceViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Gate 1 du pipeline d'import : dépôt physique du fichier source."""

    queryset = FichierSource.objects.select_related("organisation", "depose_par").order_by("-date_depot")
    serializer_class = FichierSourceSerializer

    def perform_create(self, serializer):
        fichier = serializer.validated_data.get("fichier")
        organisation = self._organisations_de_lutilisateur().first()

        if organisation is None:
            raise PermissionDenied("Aucune organisation associée à ce compte.")

        hash_fichier = calculer_hash_fichier(fichier)
        # UniqueConstraint (hash, organisation) : redéposer la même facture provoquait
        # une IntegrityError -> erreur 500 illisible pour l'utilisateur.
        if FichierSource.objects.filter(organisation=organisation, hash=hash_fichier).exists():
            raise DRFValidationError({"fichier": ["Ce fichier a déjà été importé (même contenu). Retrouvez-le dans « Fichiers importés »."]})

        fichier_instance = serializer.save(
            organisation=organisation,
            depose_par=self.request.user,
            nom=fichier.name,
            mime_type=getattr(fichier, "content_type", "") or "",
            taille_octets=fichier.size,
            hash=hash_fichier,
        )

        enregistrer_evenement(
            action="DEPOT_FICHIER_SOURCE",
            ressource="FichierSource",
            identifiant_ressource=str(fichier_instance.id),
            utilisateur=self.request.user,
            organisation=organisation,
            details={
                "fichier_id": str(fichier_instance.id),
                "nom": fichier_instance.nom,
                "taille_octets": fichier_instance.taille_octets,
                "hash": fichier_instance.hash,
            },
            request=self.request
        )


class ImportDonneesViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Gate 2 du pipeline d'import : pilotage du traitement OCR/classification/extraction."""

    queryset = ImportDonnees.objects.select_related("fichier_source", "organisation", "source_donnee").order_by("-date_import")
    serializer_class = ImportDonneesSerializer

    def perform_create(self, serializer):
        fichier_source = serializer.validated_data.get("fichier_source")
        compteur = serializer.validated_data.get("compteur")

        est_membre = fichier_source and UtilisateurOrganisation.objects.filter(
            organisation=fichier_source.organisation, utilisateur=self.request.user
        ).exists()
        if not est_membre:
            raise PermissionDenied("Vous ne pouvez pas lancer un import pour un fichier hors de votre organisation.")

        if compteur is not None and compteur.site.organisation_id != fichier_source.organisation_id:
            raise PermissionDenied("Le compteur sélectionné n'appartient pas à la même organisation que le fichier importé.")

        nom_fichier = fichier_source.nom
        extension = nom_fichier.rsplit(".", 1)[-1].upper() if "." in nom_fichier else ""

        import_instance = serializer.save(
            lance_par=self.request.user,
            organisation=fichier_source.organisation,
            nom_fichier=nom_fichier,
            format=extension,
            type_donnees="",
        )

        enregistrer_evenement(
            action="CREATION_IMPORT",
            ressource="ImportDonnees",
            identifiant_ressource=str(import_instance.id),
            utilisateur=self.request.user,
            organisation=fichier_source.organisation,
            details={
                "import_id": str(import_instance.id),
                "fichier_source_id": str(fichier_source.id),
                "compteur_id": str(compteur.id) if compteur else None,
            },
            request=self.request
        )

    @action(detail=True, methods=["post"], url_path="lancer")
    def lancer(self, request, pk=None):
        import_instance = self.get_object()
        import_instance.lancer_import()

        enregistrer_evenement(
            action="LANCEMENT_PIPELINE_OCR",
            ressource="ImportDonnees",
            identifiant_ressource=str(import_instance.id),
            utilisateur=request.user,
            organisation=import_instance.organisation,
            details={"import_id": str(import_instance.id)},
            request=request
        )

        service = OCRService()

        try:
            texte = service.traiter_import(import_instance)
        except OCRServiceError as erreur:
            self._echouer(import_instance, str(erreur), request)
            return Response(self.get_serializer(import_instance).data, status=status.HTTP_200_OK)

        score_lisibilite = service.calculer_score_lisibilite(texte)
        classification = service.classifier_document(texte)
        score_pertinence = classification["relevance_score"]

        import_instance.ocr_statut = "TERMINE"
        import_instance.score_lisibilite = Decimal(str(score_lisibilite))
        import_instance.score_pertinence = Decimal(str(score_pertinence))
        import_instance.type_donnees = classification["type"]

        if classification["decision"] == "reject":
            signaux = ", ".join(classification["negative_signals"]) or "aucun signal énergétique reconnu"
            import_instance.statut = ImportDonnees.Statut.HORS_PERIMETRE
            import_instance.ocr_erreur = f"Document non énergétique détecté ({signaux})."
            import_instance.date_traitement = timezone.now()
            import_instance.save(update_fields=(
                "statut", "ocr_statut", "ocr_erreur", "score_lisibilite",
                "score_pertinence", "type_donnees", "date_traitement",
            ))

            enregistrer_evenement(
                action="REJET_DOCUMENT_HORS_PERIMETRE",
                ressource="ImportDonnees",
                identifiant_ressource=str(import_instance.id),
                utilisateur=request.user,
                organisation=import_instance.organisation,
                details={"import_id": str(import_instance.id), "raison": import_instance.ocr_erreur},
                request=request
            )
            return Response(self.get_serializer(import_instance).data, status=status.HTTP_200_OK)

        champs = service.extraire_champs_energetiques(texte, classification["type"])
        validation = service.valider_champs_energetiques(champs)

        import_instance.donnees_extraites = champs
        import_instance.rapport_analyse = {"classification": classification, "validation": validation}
        import_instance.nombre_lignes = 1
        score_qualite = self._calculer_score_qualite(score_lisibilite, score_pertinence, validation)
        import_instance.score_qualite = score_qualite

        if not validation["valide"]:
            import_instance.statut = ImportDonnees.Statut.INCOHERENT
            import_instance.ocr_erreur = "; ".join(e["message"] for e in validation["erreurs"])
        elif (classification.get("decision") == "human_review" or validation.get("requires_review")) and score_qualite < Decimal("80"):
            import_instance.statut = ImportDonnees.Statut.REVUE_REQUISE
        else:
            import_instance.statut = ImportDonnees.Statut.TERMINE

        import_instance.date_traitement = timezone.now()
        import_instance.save(update_fields=(
            "statut", "ocr_statut", "ocr_erreur", "score_lisibilite", "score_pertinence",
            "type_donnees", "donnees_extraites", "rapport_analyse", "nombre_lignes",
            "nombre_erreurs", "score_qualite", "date_traitement",
        ))

        if import_instance.statut == ImportDonnees.Statut.TERMINE:
            # La publication (métriques, anomalie, hypothèse IA) ne doit jamais
            # faire échouer un import déjà enregistré : on journalise et on continue.
            try:
                from analysis.views import _publier_resultat_depuis_import
                _publier_resultat_depuis_import(import_instance)
            except Exception:
                logger.exception("Publication du résultat métrique impossible pour l'import %s.", import_instance.id)

        enregistrer_evenement(
            action="TRAITEMENT_IMPORT_TERMINE",
            ressource="ImportDonnees",
            identifiant_ressource=str(import_instance.id),
            utilisateur=request.user,
            organisation=import_instance.organisation,
            details={
                "import_id": str(import_instance.id),
                "statut_final": import_instance.statut,
                "score_qualite": float(import_instance.score_qualite),
            },
            request=request
        )

        return Response(self.get_serializer(import_instance).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="valider-et-publier")
    def valider_et_publier(self, request, pk=None):
        """Action manuelle : valide un import (même s'il était en REVUE_REQUISE) et publie le résultat métrique."""
        import_instance = self.get_object()
        import_instance.statut = ImportDonnees.Statut.TERMINE
        import_instance.save(update_fields=("statut",))

        try:
            from analysis.views import _publier_resultat_depuis_import
            _publier_resultat_depuis_import(import_instance)
        except Exception:
            logger.exception("Publication du résultat métrique impossible pour l'import %s.", import_instance.id)

        enregistrer_evenement(
            action="VALIDATION_ET_PUBLICATION_IMPORT",
            ressource="ImportDonnees",
            identifiant_ressource=str(import_instance.id),
            utilisateur=request.user,
            organisation=import_instance.organisation,
            details={"import_id": str(import_instance.id)},
            request=request
        )
        return Response(self.get_serializer(import_instance).data, status=status.HTTP_200_OK)

    def _echouer(self, import_instance, message, request=None):
        import_instance.statut = ImportDonnees.Statut.ECHOUE
        import_instance.ocr_statut = "ECHEC"
        import_instance.ocr_erreur = message
        import_instance.date_traitement = timezone.now()
        import_instance.save(update_fields=("statut", "ocr_statut", "ocr_erreur", "date_traitement"))

        enregistrer_evenement(
            action="ECHEC_TRAITEMENT_IMPORT",
            ressource="ImportDonnees",
            identifiant_ressource=str(import_instance.id),
            utilisateur=request.user if request else import_instance.lance_par,
            organisation=import_instance.organisation,
            details={"import_id": str(import_instance.id), "erreur": message},
            request=request
        )

    @staticmethod
    def _calculer_score_qualite(score_lisibilite, score_pertinence, validation):
        base = ((float(score_lisibilite) + float(score_pertinence)) / 2) * 100
        base -= len(validation["erreurs"]) * 15
        base -= len(validation["warnings"]) * 5
        base = max(0.0, min(100.0, base))
        return Decimal(str(round(base, 2)))


class SourceDonneeViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Configuration des canaux d'acquisition (Woyofal, imports manuels, etc.)."""

    queryset = SourceDonnee.objects.select_related("organisation").order_by("nom")
    serializer_class = SourceDonneeSerializer

    def perform_create(self, serializer):
        organisation = serializer.validated_data.get("organisation") or self._organisations_de_lutilisateur().first()
        
        if not organisation:
            raise PermissionDenied("Aucune organisation associée à ce compte.")

        est_membre = UtilisateurOrganisation.objects.filter(
            organisation=organisation, utilisateur=self.request.user
        ).exists()
        if not est_membre:
            raise PermissionDenied("Vous ne pouvez pas créer une source pour une organisation dont vous n'êtes pas membre.")
        
        instance = serializer.save(organisation=organisation)

        enregistrer_evenement(
            action="CREATION_SOURCE_DONNEE",
            ressource="SourceDonnee",
            identifiant_ressource=str(instance.id),
            utilisateur=self.request.user,
            organisation=organisation,
            details={"source_id": str(instance.id), "nom": instance.nom},
            request=self.request
        )


class FacteurEmissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Coefficients réglementaires globaux, partagés entre toutes les organisations."""

    queryset = FacteurEmission.objects.all().order_by("nom")
    serializer_class = FacteurEmissionSerializer
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]


class DonneeEnergetiqueViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Index de consommation bruts, rattachés à un compteur d'une organisation."""

    queryset = DonneeEnergetique.objects.select_related(
        "compteur__site__organisation", "source_donnee", "facteur_emission"
    ).order_by("-periode_debut")
    serializer_class = DonneeEnergetiqueSerializer
    organisation_lookup = "compteur__site__organisation"

    def perform_create(self, serializer):
        """Refuse un relevé sur un compteur d'une autre organisation (rien ne le
        contrôlait) et le marque VALIDEE : les métriques (registry : statut_validation
        requis = VALIDEE) ignorent tout relevé EN_ATTENTE, et aucun écran ni endpoint
        ne permet de le valider — la mesure de consommation restait donc vide.
        Mettre ECOSCAN_VALIDATION_AUTO_RELEVES = False pour rétablir une validation manuelle."""
        compteur = serializer.validated_data.get("compteur")
        organisation = getattr(getattr(compteur, "site", None), "organisation", None)
        if organisation is None or not self._organisations_de_lutilisateur().filter(id=organisation.id).exists():
            raise PermissionDenied("Compteur hors de votre organisation.")
        if getattr(settings, "ECOSCAN_VALIDATION_AUTO_RELEVES", True):
            serializer.save(statut_validation=DonneeEnergetique.StatutValidation.VALIDEE)
        else:
            serializer.save()


class HistoriquePerformanceViewSet(OrganisationScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Consolidations périodiques de performance, générées par les services d'analyse."""

    queryset = HistoriquePerformance.objects.select_related("fiche_projet__organisation").order_by("-periode")
    serializer_class = HistoriquePerformanceSerializer
    organisation_lookup = "fiche_projet__organisation"


class SyntheseFinanciereViewSet(OrganisationScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    """Bilans et calculs de ROI par fiche projet."""

    queryset = SyntheseFinanciere.objects.select_related("fiche_projet__organisation").order_by("-date_calcul")
    serializer_class = SyntheseFinanciereSerializer
    organisation_lookup = "fiche_projet__organisation"


class ObjectifViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Cibles de réduction de consommation ou d'émissions d'une organisation."""

    queryset = Objectif.objects.select_related("organisation").order_by("-date_debut")
    serializer_class = ObjectifSerializer

    def perform_create(self, serializer):
        organisation = serializer.validated_data.get("organisation") or self._organisations_de_lutilisateur().first()
        if not organisation:
            raise PermissionDenied("Aucune organisation associée.")

        est_membre = UtilisateurOrganisation.objects.filter(
            organisation=organisation, utilisateur=self.request.user
        ).exists()
        if not est_membre:
            raise PermissionDenied("Vous ne pouvez pas créer un objectif pour une organisation dont vous n'êtes pas membre.")
        
        instance = serializer.save(organisation=organisation)

        enregistrer_evenement(
            action="CREATION_OBJECTIF",
            ressource="Objectif",
            identifiant_ressource=str(instance.id),
            utilisateur=self.request.user,
            organisation=organisation,
            details={"objectif_id": str(instance.id), "titre": getattr(instance, "titre", "")},
            request=self.request
        )


class IndicateurViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """KPIs rattachés à un objectif."""

    queryset = Indicateur.objects.select_related("objectif__organisation").order_by("-date_calcul")
    serializer_class = IndicateurSerializer
    organisation_lookup = "objectif__organisation"


class IndicateurObjectifViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Consolidation de la progression globale face aux jalons d'un objectif."""

    queryset = IndicateurObjectif.objects.select_related("objectif__organisation").order_by("nom")
    serializer_class = IndicateurObjectifSerializer
    organisation_lookup = "objectif__organisation"


class PointSuiviEnergetiqueViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    queryset = PointSuiviEnergetique.objects.select_related(
        "organisation", "site", "compteur"
    ).order_by("site__nom", "nom")
    serializer_class = PointSuiviEnergetiqueSerializer
    organisation_lookup = "organisation"


class ReleveRituelEnergetiqueViewSet(
    OrganisationScopedQuerySetMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = ReleveRituelEnergetique.objects.select_related(
        "point_suivi__organisation"
    ).order_by("-date_releve", "creneau")
    serializer_class = ReleveRituelEnergetiqueSerializer
    organisation_lookup = "point_suivi__organisation"

    def get_queryset(self):
        queryset = super().get_queryset()
        point_id = self.request.query_params.get("point_suivi")
        date_releve = self.request.query_params.get("date_releve")

        if point_id:
            try:
                uuid.UUID(point_id)
            except (ValueError, TypeError):
                raise DRFValidationError({"point_suivi": ["Identifiant invalide."]})
            queryset = queryset.filter(point_suivi_id=point_id)

        if date_releve:
            date_value = parse_date(date_releve)
            if date_value is None:
                raise DRFValidationError({"date_releve": ["Format attendu : AAAA-MM-JJ."]})
            queryset = queryset.filter(date_releve=date_value)

        return queryset

    def perform_create(self, serializer):
        reading = serializer.save()
        point = reading.point_suivi
        if (
            point.mode_mesure == PointSuiviEnergetique.ModeMesure.SOLDE_WOYOFAL
            and reading.creneau == ReleveRituelEnergetique.Creneau.VINGT_HEURES
        ):
            from analysis.services.taches import lancer_en_arriere_plan
            from analysis.services.woyofal_rituel import analyser_woyofal_rituel

            lancer_en_arriere_plan(
                analyser_woyofal_rituel,
                str(point.organisation_id),
                reading.date_releve.isoformat(),
            )


class RechargeRituelWoyofalViewSet(
    OrganisationScopedQuerySetMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = RechargeRituelWoyofal.objects.select_related(
        "point_suivi__organisation"
    ).order_by("-effectuee_le")
    serializer_class = RechargeRituelWoyofalSerializer
    organisation_lookup = "point_suivi__organisation"

    def perform_create(self, serializer):
        recharge = serializer.save()
        from analysis.services.taches import lancer_en_arriere_plan
        from analysis.services.woyofal_rituel import analyser_woyofal_rituel

        jour = timezone.localtime(recharge.effectuee_le).date()
        lancer_en_arriere_plan(
            analyser_woyofal_rituel,
            str(recharge.point_suivi.organisation_id),
            jour.isoformat(),
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        point_id = self.request.query_params.get("point_suivi")
        date_from = self.request.query_params.get("date_from")
        date_to = self.request.query_params.get("date_to")
        if point_id:
            try:
                uuid.UUID(point_id)
            except (ValueError, TypeError):
                raise DRFValidationError({"point_suivi": ["Identifiant invalide."]})
            queryset = queryset.filter(point_suivi_id=point_id)
        if date_from:
            parsed_date = parse_date(date_from)
            if parsed_date is None:
                raise DRFValidationError({"date_from": ["Format attendu : AAAA-MM-JJ."]})
            queryset = queryset.filter(effectuee_le__date__gte=parsed_date)
        if date_to:
            parsed_date = parse_date(date_to)
            if parsed_date is None:
                raise DRFValidationError({"date_to": ["Format attendu : AAAA-MM-JJ."]})
            queryset = queryset.filter(effectuee_le__date__lte=parsed_date)
        return queryset


def _compteur_de(request, compteur_id):
    if not compteur_id:
        return None
    try:
        return Compteur.objects.filter(
            id=compteur_id,
            site__organisation__in=BillingAccessService().organisations_avec_acces(request.user),
        ).first()
    except (ValueError, TypeError, DjangoValidationError):
        return None


class PredictionAchatView(APIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]

    def post(self, request):
        compteur = _compteur_de(request, request.data.get("compteur"))
        montant = request.data.get("montant_fcfa")
        if compteur is None or montant is None:
            return Response({"error": "compteur valide et montant_fcfa requis."}, status=400)
        try:
            montant_decimal = Decimal(str(montant))
        except InvalidOperation:
            return Response({"error": "montant_fcfa doit être un nombre."}, status=status.HTTP_400_BAD_REQUEST)
        if montant_decimal <= 0:
            return Response({"error": "montant_fcfa doit être supérieur à 0."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(predire_kwh(compteur, montant_decimal))


class EtatTrancheView(APIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]

    def get(self, request):
        compteur = _compteur_de(request, request.query_params.get("compteur"))
        if compteur is None:
            return Response({"error": "Compteur introuvable."}, status=404)
        return Response(etat_tranche(compteur))


def _client_id_valide(request):
    """client_id du mobile (UUID) : 400 clair au lieu d'une erreur 500 au filtrage."""
    brut = request.data.get("client_id")
    if not brut:
        return None
    try:
        return uuid.UUID(str(brut))
    except ValueError:
        raise DRFValidationError({"client_id": ["UUID invalide."]})


def _compteur_autorise(request):
    """Compteur du corps de requête, ou 403 s'il n'appartient pas à l'utilisateur."""
    compteur = _compteur_de(request, request.data.get("compteur"))
    if compteur is None:
        raise PermissionDenied("Compteur introuvable ou hors de votre organisation.")
    return compteur


def _declencher_analyse(compteur):
    """Après un nouveau relevé de solde : analyse (hier puis aujourd'hui) en
    arrière-plan — la réponse HTTP n'attend ni le calcul ni l'IA.

    Remplace l'ancienne « synchronisation Data Center » qui copiait kWh achetés
    ou soldes dans DonneeEnergetique.valeur : ce champ est un INDEX de compteur,
    ces chiffres n'en sont pas (le Data Center lit désormais ReleveSolde et
    AchatWoyofal directement)."""
    try:
        from analysis.services.orchestrator import analyser_compteur_apres_saisie
        from analysis.services.taches import lancer_en_arriere_plan
        lancer_en_arriere_plan(analyser_compteur_apres_saisie, compteur.id)
    except Exception:
        logger.exception("Analyse non planifiée pour le compteur %s.", getattr(compteur, "id", None))


class AchatWoyofalListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    serializer_class = AchatWoyofalSerializer

    def get_queryset(self):
        return AchatWoyofal.objects.filter(
            compteur__site__organisation__in=BillingAccessService().organisations_avec_acces(self.request.user))

    def create(self, request, *args, **kwargs):
        compteur = _compteur_autorise(request)

        client_id = _client_id_valide(request)
        if client_id:  # idempotence : ne compare que si un client_id est fourni
            existant = self.get_queryset().filter(client_id=client_id).first()
            if existant:
                return Response(self.get_serializer(existant).data, status=status.HTTP_200_OK)

        response = super().create(request, *args, **kwargs)

        if response.status_code == status.HTTP_201_CREATED:
            enregistrer_evenement(
                action="CREATION_ACHAT_WOYOFAL",
                ressource="AchatWoyofal",
                identifiant_ressource=str(response.data.get("id", "")),
                utilisateur=request.user,
                organisation=compteur.site.organisation,
                details={
                    "client_id": str(client_id) if client_id else None,
                    "montant": request.data.get("montant_fcfa"),
                    "compteur_id": str(compteur.id),
                },
                request=request
            )

        return response


class ReleveSoldeListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    serializer_class = ReleveSoldeSerializer

    def get_queryset(self):
        return ReleveSolde.objects.filter(
            compteur__site__organisation__in=BillingAccessService().organisations_avec_acces(self.request.user))

    def create(self, request, *args, **kwargs):
        compteur = _compteur_autorise(request)

        client_id = _client_id_valide(request)
        if client_id:
            existant = self.get_queryset().filter(client_id=client_id).first()
            if existant:
                return Response(self.get_serializer(existant).data, status=status.HTTP_200_OK)

        response = super().create(request, *args, **kwargs)

        if response.status_code == status.HTTP_201_CREATED:
            _declencher_analyse(compteur)

            enregistrer_evenement(
                action="CREATION_RELEVE_SOLDE",
                ressource="ReleveSolde",
                identifiant_ressource=str(response.data.get("id", "")),
                utilisateur=request.user,
                organisation=compteur.site.organisation,
                details={"client_id": str(client_id) if client_id else None, "compteur_id": str(compteur.id)},
                request=request
            )

        return response


class AutonomieView(APIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]

    def get(self, request):
        compteur = _compteur_de(request, request.query_params.get("compteur"))
        if compteur is None:
            return Response({"error": "Compteur introuvable."}, status=404)
        return Response(estimer_autonomie(compteur))


CHAMPS_UTILES_CAPTURE = (
    "numero_compteur", "ancien_index", "nouveau_index",
    "consommation_kwh", "montant_net_paye", "date_facture", "unite",
)


class CaptureImageView(APIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    parser_classes = [MultiPartParser]

    def post(self, request):
        fichier = request.FILES.get("image")
        if not fichier:
            return Response({"error": "Aucune image reçue."}, status=status.HTTP_400_BAD_REQUEST)

        contenu = fichier.read()
        resultat = analyser_image(contenu, fichier.name)

        if "_error" in resultat:
            return Response({"error": resultat["_error"]}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        champs_bruts = resultat.get("champs", {})
        champs_utiles = {cle: champs_bruts.get(cle) for cle in CHAMPS_UTILES_CAPTURE if champs_bruts.get(cle) is not None}

        organisation = BillingAccessService().organisations_avec_acces(request.user).first()
        enregistrer_evenement(
            action="CAPTURE_IMAGE_ANALYSEE",
            ressource="CaptureImage",
            utilisateur=request.user,
            organisation=organisation,
            details={"nom_fichier": fichier.name, "champs_detectes": list(champs_utiles.keys())},
            request=request
        )

        return Response({
            "champs": champs_utiles,
            "champs_non_lisibles": resultat.get("champs_non_lisibles", []),
            "description": resultat.get("description", ""),
        }, status=status.HTTP_200_OK)


class TranscrireAudioView(APIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    parser_classes = [MultiPartParser]
    extensions_autorisees = {".m4a", ".mp3", ".ogg", ".wav", ".webm"}
    taille_max_octets = 25 * 1024 * 1024

    def post(self, request):
        fichier = request.FILES.get("file")
        if not fichier:
            return Response({"error": "Aucun enregistrement audio reçu."}, status=status.HTTP_400_BAD_REQUEST)
        extension = f".{fichier.name.rsplit('.', 1)[-1].lower()}" if "." in fichier.name else ""
        if extension not in self.extensions_autorisees:
            return Response(
                {"error": "Format audio non pris en charge. Utilisez WebM, OGG, WAV, MP3 ou M4A."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if fichier.size > self.taille_max_octets:
            return Response({"error": "L’enregistrement dépasse la taille maximale de 25 Mo."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        nom_fichier = f"bilan-vocal{extension}"
        resultat = transcrire_audio(fichier.read(), nom_fichier, request.data.get("language", "fr"))
        if "_error" in resultat:
            return Response({"error": resultat["_error"]}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not resultat.get("transcription", "").strip():
            return Response(
                {"error": "Aucune parole n’a été reconnue. Réessayez ou saisissez le bilan par écrit."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        organisation = BillingAccessService().organisations_avec_acces(request.user).first()
        enregistrer_evenement(
            action="TRANSCRIPTION_BILAN_VOCAL",
            ressource="BilanEnergie",
            utilisateur=request.user,
            organisation=organisation,
            details={"langue": resultat.get("language_detected"), "moteur": resultat.get("engine_used")},
            request=request,
        )
        return Response(resultat, status=status.HTTP_200_OK)


class CreerImportDepuisCaptureView(APIView):
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]
    parser_classes = [MultiPartParser]

    def post(self, request):
        organisation = BillingAccessService().organisations_avec_acces(request.user).first()
        if organisation is None:
            return Response({"error": "Aucune organisation associée."}, status=status.HTTP_409_CONFLICT)

        fichier = request.FILES.get("image")
        try:
            champs_confirmes = json.loads(request.data.get("champs_confirmes", "{}"))
        except (TypeError, ValueError):
            return Response({"error": "champs_confirmes doit être un JSON valide."}, status=status.HTTP_400_BAD_REQUEST)
        if not fichier or not isinstance(champs_confirmes, dict) or not champs_confirmes:
            return Response({"error": "Image et champs confirmés requis."}, status=status.HTTP_400_BAD_REQUEST)

        hash_fichier = calculer_hash_fichier(fichier)
        if FichierSource.objects.filter(organisation=organisation, hash=hash_fichier).exists():
            return Response({"error": "Cette photo a déjà été importée."}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            fichier_source = FichierSource.objects.create(
                organisation=organisation, depose_par=request.user,
                nom=fichier.name, fichier=fichier,
                mime_type=getattr(fichier, "content_type", "") or "",
                taille_octets=fichier.size,
                hash=hash_fichier,
            )
            import_instance = ImportDonnees.objects.create(
                fichier_source=fichier_source, organisation=organisation, lance_par=request.user,
                nom_fichier=fichier.name, format="IMAGE", type_donnees="FACTURE_SENELEC",
                donnees_extraites=champs_confirmes,
                statut=ImportDonnees.Statut.TERMINE,
                ocr_statut="TERMINE_VIA_CAPTURE",
                date_traitement=timezone.now(),
                nombre_lignes=1,
            )

        # Hors transaction : la publication peut appeler le service IA (lent) et
        # ne doit ni bloquer la base ni annuler l'import déjà enregistré.
        try:
            from analysis.views import _publier_resultat_depuis_import
            _publier_resultat_depuis_import(import_instance)
        except Exception:
            logger.exception("Publication du résultat métrique impossible pour l'import %s.", import_instance.id)

        enregistrer_evenement(
            action="CREATION_IMPORT_DEPUIS_CAPTURE",
            ressource="ImportDonnees",
            identifiant_ressource=str(import_instance.id),
            utilisateur=request.user,
            organisation=organisation,
            details={"import_id": str(import_instance.id), "fichier_source_id": str(fichier_source.id)},
            request=request
        )

        return Response(ImportDonneesSerializer(import_instance).data, status=status.HTTP_201_CREATED)
