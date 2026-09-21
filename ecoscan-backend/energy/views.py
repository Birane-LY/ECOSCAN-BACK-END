import hashlib
import json
import logging
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status, viewsets, generics
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
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
    ReleveSoldeSerializer
)
from .services.hashing import calculer_hash_fichier
from .services.services import OCRService
from .services.ai_client import analyser_image
from .services.ocr import OCRServiceError
from .services.prediction_tranches import etat_tranche, predire_kwh
from .services.autonomie import estimer_autonomie

logger = logging.getLogger(__name__)


class OrganisationScopedQuerySetMixin:
    """Filtrage multi-tenant commun à tous les ViewSets de l'app energy.

    Le rôle SUPER_ADMIN est exclu des données métiers des organisations, comme
    partout ailleurs dans le projet (accounts, organizations, analysis) : il
    n'a pas vocation à consulter les données énergétiques des clients.
    """

    permission_classes = [permissions.IsAuthenticated]
    organisation_lookup = "organisation"

    def get_queryset(self):
        queryset = self.queryset
        user = self.request.user

        if getattr(user, "role", None) == "SUPER_ADMIN":
            return queryset.none()

        organisations = Organisation.objects.filter(membres__utilisateur=user)
        return queryset.filter(**{f"{self.organisation_lookup}__in": organisations})

    def _organisations_de_lutilisateur(self):
        return Organisation.objects.filter(membres__utilisateur=self.request.user)


class FichierSourceViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Gate 1 du pipeline d'import : dépôt physique du fichier source.

    L'organisation, le nom, le type MIME, la taille et le hash sont calculés
    ici plutôt que confiés au client — ce sont des faits sur le fichier reçu,
    pas des déclarations qu'on peut laisser forger côté front.
    """

    queryset = FichierSource.objects.select_related("organisation", "depose_par").order_by("-date_depot")
    serializer_class = FichierSourceSerializer

    def perform_create(self, serializer):
        fichier = serializer.validated_data.get("fichier")
        organisation = self._organisations_de_lutilisateur().first()

        if organisation is None:
            raise PermissionDenied("Aucune organisation associée à ce compte.")

        contenu = fichier.read()
        fichier.seek(0)

        serializer.save(
            organisation=organisation,
            depose_par=self.request.user,
            nom=fichier.name,
            mime_type=getattr(fichier, "content_type", "") or "",
            taille_octets=fichier.size,
            hash=calculer_hash_fichier(fichier),
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

        serializer.save(
            lance_par=self.request.user,
            organisation=fichier_source.organisation,
            nom_fichier=nom_fichier,
            format=extension,
            type_donnees="",
        )

    @action(detail=True, methods=["post"], url_path="lancer")
    def lancer(self, request, pk=None):
        """Exécute la chaîne OCR -> classification -> extraction -> validation, et persiste le résultat."""
        import_instance = self.get_object()
        import_instance.lancer_import()  # statut -> EN_COURS

        service = OCRService()

        try:
            texte = service.traiter_import(import_instance)
        except OCRServiceError as erreur:
            self._echouer(import_instance, str(erreur))
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
            return Response(self.get_serializer(import_instance).data, status=status.HTTP_200_OK)

        champs = service.extraire_champs_energetiques(texte, classification["type"])
        validation = service.valider_champs_energetiques(champs)

        import_instance.donnees_extraites = champs
        import_instance.rapport_analyse = {"classification": classification, "validation": validation}
        # Un document énergétique traité (facture, relevé) correspond à un seul
        # enregistrement de consommation extrait, pas à un nombre de lignes de fichier.
        import_instance.nombre_lignes = 1
        import_instance.nombre_erreurs = len(validation["erreurs"])
        import_instance.score_qualite = self._calculer_score_qualite(score_lisibilite, score_pertinence, validation)

        if not validation["valide"]:
            import_instance.statut = ImportDonnees.Statut.INCOHERENT
            import_instance.ocr_erreur = "; ".join(e["message"] for e in validation["erreurs"])
        elif classification["decision"] == "human_review" or validation["requires_review"]:
            import_instance.statut = ImportDonnees.Statut.REVUE_REQUISE
        else:
            import_instance.statut = ImportDonnees.Statut.TERMINE

        import_instance.date_traitement = timezone.now()
        import_instance.save(update_fields=(
            "statut", "ocr_statut", "ocr_erreur", "score_lisibilite", "score_pertinence",
            "type_donnees", "donnees_extraites", "rapport_analyse", "nombre_lignes",
            "nombre_erreurs", "score_qualite", "date_traitement",
        ))

        # Ferme la boucle avec l'app analysis : un import TERMINE doit produire
        # le ResultatMetrique que la vue "Analyses" lit — sinon l'upload réussit
        # mais rien n'apparaît jamais côté analyses. Uniquement dans la branche
        # de succès (jamais pour reject/incohérent/révision).
        if import_instance.statut == ImportDonnees.Statut.TERMINE:
            from analysis.views import _publier_resultat_depuis_import
            _publier_resultat_depuis_import(import_instance)

        return Response(self.get_serializer(import_instance).data, status=status.HTTP_200_OK)

    @staticmethod
    def _echouer(import_instance, message):
        import_instance.statut = ImportDonnees.Statut.ECHOUE
        import_instance.ocr_statut = "ECHEC"
        import_instance.ocr_erreur = message
        import_instance.date_traitement = timezone.now()
        import_instance.save(update_fields=("statut", "ocr_statut", "ocr_erreur", "date_traitement"))

    @staticmethod
    def _calculer_score_qualite(score_lisibilite, score_pertinence, validation):
        """Combine la lisibilité OCR et la pertinence énergétique, pénalisées par les
        erreurs et avertissements de validation des champs extraits."""
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
        organisation = serializer.validated_data.get("organisation")
        est_membre = organisation and UtilisateurOrganisation.objects.filter(
            organisation=organisation, utilisateur=self.request.user
        ).exists()
        if not est_membre:
            raise PermissionDenied("Vous ne pouvez pas créer une source pour une organisation dont vous n'êtes pas membre.")
        serializer.save()


class FacteurEmissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Coefficients réglementaires globaux, partagés entre toutes les organisations."""

    queryset = FacteurEmission.objects.all().order_by("nom")
    serializer_class = FacteurEmissionSerializer
    permission_classes = [permissions.IsAuthenticated]


class DonneeEnergetiqueViewSet(OrganisationScopedQuerySetMixin, viewsets.ModelViewSet):
    """Index de consommation bruts, rattachés à un compteur d'une organisation."""

    queryset = DonneeEnergetique.objects.select_related(
        "compteur__site__organisation", "source_donnee", "facteur_emission"
    ).order_by("-periode_debut")
    serializer_class = DonneeEnergetiqueSerializer
    organisation_lookup = "compteur__site__organisation"


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
        organisation = serializer.validated_data.get("organisation")
        est_membre = organisation and UtilisateurOrganisation.objects.filter(
            organisation=organisation, utilisateur=self.request.user
        ).exists()
        if not est_membre:
            raise PermissionDenied("Vous ne pouvez pas créer un objectif pour une organisation dont vous n'êtes pas membre.")
        serializer.save()


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


def _compteur_de(request, compteur_id):
    if not compteur_id:
        return None
    return Compteur.objects.filter(
        id=compteur_id, site__organisation__membres__utilisateur=request.user).first()


class PredictionAchatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        compteur = _compteur_de(request, request.data.get("compteur"))
        montant = request.data.get("montant_fcfa")
        if compteur is None or montant is None:
            return Response({"error": "compteur valide et montant_fcfa requis."}, status=400)
        return Response(predire_kwh(compteur, Decimal(str(montant))))


class EtatTrancheView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        compteur = _compteur_de(request, request.query_params.get("compteur"))
        if compteur is None:
            return Response({"error": "Compteur introuvable."}, status=404)
        return Response(etat_tranche(compteur))


class AchatWoyofalListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AchatWoyofalSerializer

    def get_queryset(self):
        return AchatWoyofal.objects.filter(
            compteur__site__organisation__membres__utilisateur=self.request.user)

    def create(self, request, *args, **kwargs):
        # Idempotence : un retry de synchro offline renvoie l'achat existant
        existant = self.get_queryset().filter(client_id=request.data.get("client_id")).first()
        if existant:
            return Response(self.get_serializer(existant).data, status=200)
        return super().create(request, *args, **kwargs)

class ReleveSoldeListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ReleveSoldeSerializer

    def get_queryset(self):
        return ReleveSolde.objects.filter(
            compteur__site__organisation__membres__utilisateur=self.request.user)

    def create(self, request, *args, **kwargs):
        existant = self.get_queryset().filter(client_id=request.data.get("client_id")).first()
        if existant:
            return Response(self.get_serializer(existant).data, status=200)
        return super().create(request, *args, **kwargs)


class AutonomieView(APIView):
    permission_classes = [permissions.IsAuthenticated]

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
    """Point d'entrée pour la capture photo mobile (compteur ou facture).

    Ne persiste RIEN — retourne les champs extraits pour que l'utilisateur
    les relise et les corrige avant tout enregistrement, exactement comme
    pour l'OCR classique (aucune photo ne devient une donnée validée sans
    confirmation humaine)."""
    permission_classes = [permissions.IsAuthenticated]
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

        return Response({
            "champs": champs_utiles,
            "champs_non_lisibles": resultat.get("champs_non_lisibles", []),
            "description": resultat.get("description", ""),
        }, status=status.HTTP_200_OK)
    

class CreerImportDepuisCaptureView(APIView):
    """Transforme une capture photo de facture (déjà analysée via
    CaptureImageView) en un ImportDonnees TERMINE — après confirmation
    humaine des champs, jamais automatiquement à la volée."""
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        organisation = Organisation.objects.filter(membres__utilisateur=request.user).first()
        if organisation is None:
            return Response({"error": "Aucune organisation associée."}, status=status.HTTP_409_CONFLICT)

        fichier = request.FILES.get("image")
        champs_confirmes = json.loads(request.data.get("champs_confirmes", "{}"))
        if not fichier or not champs_confirmes:
            return Response({"error": "Image et champs confirmés requis."}, status=status.HTTP_400_BAD_REQUEST)

        contenu = fichier.read()
        fichier.seek(0)

        with transaction.atomic():
            fichier_source = FichierSource.objects.create(
                organisation=organisation, depose_par=request.user,
                nom=fichier.name, fichier=fichier,
                mime_type=getattr(fichier, "content_type", "") or "",
                taille_octets=fichier.size,
                hash=calculer_hash_fichier(fichier),
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
            from analysis.views import _publier_resultat_depuis_import
            _publier_resultat_depuis_import(import_instance)

        return Response(ImportDonneesSerializer(import_instance).data, status=status.HTTP_201_CREATED)