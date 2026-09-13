import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from organizations.models import Organisation, UtilisateurOrganisation
from .models import (
    DonneeEnergetique,
    FacteurEmission,
    FichierSource,
    HistoriquePerformance,
    ImportDonnees,
    Indicateur,
    IndicateurObjectif,
    Objectif,
    SourceDonnee,
    SyntheseFinanciere,
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
)
from energy.services.services import OCRService, OCRServiceError

logger = logging.getLogger(__name__)

# Limite de taille d'upload (15 Mo)
TAILLE_MAX_UPLOAD_OCTETS = 15 * 1024 * 1024

MIME_AUTORISES = [
    "application/pdf",
    "image/jpeg",
    "image/png",
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]


# --- PERMISSIONS & BASE VIEWSETS ( ZERO-TRUST) ---

class EstAutoriseAuxDonneesEnergy(permissions.BasePermission):
    """Permission Zero-Trust appliquant le cloisonnement multi-tenant strict."""

    def has_permission(self, request, view) -> bool:
        return bool(request.user and request.user.is_authenticated and getattr(request.user, "actif", True))

    def has_object_permission(self, request, view, obj) -> bool:
        if getattr(request.user, "role", None) == "SUPER_ADMIN":
            return False

        if hasattr(obj, "organisation"):
            organisation = obj.organisation
        elif hasattr(obj, "compteur"):
            organisation = obj.compteur.site.organisation
        elif hasattr(obj, "fiche_projet"):
            organisation = obj.fiche_projet.organisation
        elif hasattr(obj, "objectif"):
            organisation = obj.objectif.organisation
        else:
            return False

        return UtilisateurOrganisation.objects.filter(
            organisation=organisation, utilisateur=request.user
        ).exists()


class EnergieScopedViewSet(viewsets.ModelViewSet):
    """Classe de base abstraite appliquant le filtrage multi-tenant dès la requête QuerySet."""

    permission_classes = [EstAutoriseAuxDonneesEnergy]
    organisation_lookup = "organisation"

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if getattr(user, "role", None) == "SUPER_ADMIN":
            return queryset.none()

        organisations = Organisation.objects.filter(membres__utilisateur=user)
        return queryset.filter(**{f"{self.organisation_lookup}__in": organisations})


# --- GATE 1 : RÉCEPTION ET PRÉSERVATION DU FICHIER BRUT  ---

class FichierSourceViewSet(EnergieScopedViewSet):
    """GATE 1 : Enregistrement du fichier original, calcul SHA-256 et déduplication par tenant."""

    queryset = FichierSource.objects.select_related("depose_par", "organisation").order_by("-date_depot")
    serializer_class = FichierSourceSerializer
    parser_classes = [MultiPartParser, FormParser]
    organisation_lookup = "organisation"

    def perform_create(self, serializer):
        fichier_televerse = self.request.FILES.get("fichier")
        if not fichier_televerse:
            raise ValidationError({"fichier": "Aucun fichier n'a été transmis."})

        if fichier_televerse.content_type not in MIME_AUTORISES:
            raise ValidationError(
                {"fichier": f"Type MIME non autorisé : {fichier_televerse.content_type}"}
            )

        if fichier_televerse.size > TAILLE_MAX_UPLOAD_OCTETS:
            raise ValidationError(
                {
                    "fichier": (
                        f"Fichier trop volumineux ({fichier_televerse.size} octets). "
                        f"Taille maximale autorisée : {TAILLE_MAX_UPLOAD_OCTETS} octets."
                    )
                }
            )

        # Calcul SHA-256
        sha256_hash = hashlib.sha256()
        for chunk in fichier_televerse.chunks():
            sha256_hash.update(chunk)
        fichier_hash = sha256_hash.hexdigest()

        # Résolution sécurisée de l'organisation de l'utilisateur
        organisation_id = self.request.data.get("organisation")
        if organisation_id:
            organisation_user = Organisation.objects.filter(
                id=organisation_id, membres__utilisateur=self.request.user
            ).first()
        else:
            organisation_user = Organisation.objects.filter(
                membres__utilisateur=self.request.user
            ).first()

        if not organisation_user:
            raise ValidationError(
                {"organisation": "L'utilisateur n'est associé à aucune organisation valide."}
            )

        if FichierSource.objects.filter(
            hash=fichier_hash, organisation=organisation_user
        ).exists():
            raise ValidationError(
                {"fichier": "Ce document exact a déjà été téléversé au sein de votre organisation."}
            )

        with transaction.atomic():
            instance_fichier = serializer.save(
                depose_par=self.request.user,
                organisation=organisation_user,
                hash=fichier_hash,
                taille_octets=fichier_televerse.size,
                mime_type=fichier_televerse.content_type,
                nom=fichier_televerse.name,
            )

            ImportDonnees.objects.create(
                fichier_source=instance_fichier,
                organisation=organisation_user,
                lance_par=self.request.user,
                nom_fichier=fichier_televerse.name,
                format=Path(fichier_televerse.name).suffix.replace(".", "").upper(),
                type_donnees="Facture / Relevé Énergétique",
                statut=ImportDonnees.Statut.EN_ATTENTE,
            )


# --- GATE 2 : PIPELINE DE QUALIFICATION, CLASSIFICATION ET VALIDATION  ---

class ImportDonneesViewSet(EnergieScopedViewSet):
    """GATE 2 : Pipeline d'extraction OCR, classification et contrôle de cohérence métier."""

    queryset = ImportDonnees.objects.select_related(
        "organisation", "fichier_source", "lance_par", "source_donnee"
    ).order_by("-date_import")
    serializer_class = ImportDonneesSerializer

    def _recuperer_dernier_index(self, import_instance: ImportDonnees, num_compteur: Optional[str]) -> Optional[float]:
        if not num_compteur:
            return None

        derniere_donnee = DonneeEnergetique.objects.filter(
            compteur__reference=num_compteur,
            compteur__site__organisation=import_instance.organisation,
        ).order_by("-periode_fin").first()

        return float(derniere_donnee.valeur) if (derniere_donnee and derniere_donnee.valeur is not None) else None

    def _valider_champs_energetiques(
        self, ocr: OCRService, champs: Dict[str, Any], import_instance: ImportDonnees
    ) -> Dict[str, Any]:
        validation = ocr.valider_champs_energetiques(champs)
        erreurs = list(validation.get("erreurs", []))
        warnings = list(validation.get("warnings", []))

        nouveau_index = champs.get("index") or champs.get("nouveau_index")
        num_compteur = champs.get("numero_compteur") or champs.get("police")

        if nouveau_index is not None and num_compteur:
            dernier_index_base = self._recuperer_dernier_index(import_instance, num_compteur)
            if dernier_index_base is not None and float(nouveau_index) < dernier_index_base:
                erreurs.append(
                    {
                        "code": "CONTINUITE_ROMPUE",
                        "message": (
                            f"Rupture de continuité temporelle : L'index extrait ({nouveau_index}) "
                            f"est inférieur au dernier index validé en base ({dernier_index_base})."
                        ),
                    }
                )

        return {
            "valide": len(erreurs) == 0,
            "erreurs": erreurs,
            "warnings": warnings,
        }

    def _echouer(self, import_instance: ImportDonnees, message: str) -> Response:
        import_instance.statut = ImportDonnees.Statut.ECHOUE
        import_instance.ocr_statut = "ECHOUE"
        import_instance.ocr_erreur = message
        import_instance.save()

        return Response(
            {"erreur": f"Échec critique lors du traitement OCR : {message}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    @action(detail=True, methods=["post"])
    def lancer(self, request, pk=None):
        import_instance = self.get_object()

        if import_instance.statut in [
            ImportDonnees.Statut.TERMINE,
            ImportDonnees.Statut.EN_COURS,
        ]:
            raise ValidationError(
                {"import": "Cet import est déjà en cours de traitement ou a été validé."}
            )

        import_instance.lancer_import()
        ocr = OCRService()

        try:
            texte_extrait = ocr.traiter_import(import_instance)

            if not texte_extrait or not texte_extrait.strip():
                import_instance.statut = ImportDonnees.Statut.ECHOUE
                import_instance.ocr_statut = "ECHOUE"
                import_instance.ocr_erreur = "Aucun texte exploitable n'a été restitué par l'OCR."
                import_instance.save()
                return Response(
                    self.get_serializer(import_instance).data,
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            # Calculs analytiques
            score_lisibilite = ocr.calculer_score_lisibilite(texte_extrait)
            classification = ocr.classifier_document(texte_extrait)

            import_instance.score_lisibilite = score_lisibilite
            import_instance.score_pertinence = classification.get("relevance_score", 0.0)
            type_doc = classification.get("type")

            # Condition 1 : Rejet si hors périmètre
            if type_doc == "DOCUMENT_NON_ENERGETIQUE":
                import_instance.statut = ImportDonnees.Statut.HORS_PERIMETRE
                import_instance.ocr_statut = "REJETE"
                import_instance.rapport_analyse = {
                    "classification": classification,
                    "lisibilite": score_lisibilite,
                }
                import_instance.save()
                return Response(
                    {
                        "statut": ImportDonnees.Statut.HORS_PERIMETRE,
                        "message": "Le document téléversé ne contient pas de données énergétiques valides.",
                        "details": {"classification": classification},
                    },
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            # Condition 2 : Zone d'incertitude
            if type_doc == "DOCUMENT_ENERGETIQUE_A_REVOIR":
                champs_provisoires = ocr.extraire_champs_energetiques(texte_extrait, type_document=type_doc)
                import_instance.statut = ImportDonnees.Statut.REVUE_REQUISE
                import_instance.ocr_statut = "A_VALIDER"
                import_instance.donnees_extraites = champs_provisoires
                import_instance.rapport_analyse = {
                    "motif": "classification_incertaine",
                    "classification": classification,
                    "lisibilite": score_lisibilite,
                }
                import_instance.save()
                return Response(
                    self.get_serializer(import_instance).data,
                    status=status.HTTP_202_ACCEPTED,
                )

            # Condition 3 : Quality Gates métiers
            champs = ocr.extraire_champs_energetiques(texte_extrait, type_document=type_doc)
            validation = self._valider_champs_energetiques(ocr, champs, import_instance)

            if not validation["valide"]:
                codes_incoherents = {"INDEX_INCOHERENT", "CONTINUITE_ROMPUE"}
                statut_cible = (
                    ImportDonnees.Statut.INCOHERENT
                    if any(e.get("code") in codes_incoherents for e in validation["erreurs"])
                    else ImportDonnees.Statut.REVUE_REQUISE
                )
                import_instance.statut = statut_cible
                import_instance.ocr_statut = "A_VALIDER"
                import_instance.donnees_extraites = champs
                import_instance.rapport_analyse = {
                    "erreurs_validation": validation["erreurs"],
                    "avertissements": validation["warnings"],
                    "lisibilite": score_lisibilite,
                    "classification": classification,
                }
                import_instance.save()
                return Response(
                    self.get_serializer(import_instance).data,
                    status=status.HTTP_202_ACCEPTED,
                )

            # Condition 4 : Publication
            import_instance.statut = ImportDonnees.Statut.TERMINE
            import_instance.ocr_statut = "TERMINE"
            import_instance.donnees_extraites = champs
            import_instance.score_qualite = round(
                (score_lisibilite * 0.4 + import_instance.score_pertinence * 0.6) * 100, 2
            )
            import_instance.rapport_analyse = {
                "avertissements": validation["warnings"],
                "classification": classification,
            }
            import_instance.date_traitement = timezone.now()
            import_instance.save()

            return Response(
                self.get_serializer(import_instance).data,
                status=status.HTTP_200_OK,
            )

        except OCRServiceError as error:
            return self._echouer(import_instance, str(error))
        except Exception as error:
            logger.exception(
                "Erreur inattendue dans le pipeline d'import %s", getattr(import_instance, "pk", "?")
            )
            return self._echouer(import_instance, f"Erreur interne inattendue : {error}")

    @action(detail=True, methods=["post"])
    def annuler(self, request, pk=None):
        import_donnees = self.get_object()
        import_donnees.annuler_import()
        return Response(self.get_serializer(import_donnees).data, status=status.HTTP_200_OK)


# --- VIEWSETS MÉTIERS / ANALYTIQUES (GOLD LAYER) ---

class SourceDonneeViewSet(EnergieScopedViewSet):
    queryset = SourceDonnee.objects.select_related("organisation").order_by("nom")
    serializer_class = SourceDonneeSerializer


class FacteurEmissionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FacteurEmission.objects.all().order_by("nom", "-version")
    serializer_class = FacteurEmissionSerializer
    permission_classes = [permissions.IsAuthenticated]


class DonneeEnergetiqueViewSet(EnergieScopedViewSet):
    queryset = DonneeEnergetique.objects.select_related(
        "compteur__site__organisation", "source_donnee", "facteur_emission"
    )
    serializer_class = DonneeEnergetiqueSerializer
    organisation_lookup = "compteur__site__organisation"


class HistoriquePerformanceViewSet(EnergieScopedViewSet):
    queryset = HistoriquePerformance.objects.select_related(
        "fiche_projet__organisation"
    ).order_by("-periode")
    serializer_class = HistoriquePerformanceSerializer
    organisation_lookup = "fiche_projet__organisation"


class SyntheseFinanciereViewSet(EnergieScopedViewSet):
    queryset = SyntheseFinanciere.objects.select_related(
        "fiche_projet__organisation"
    ).order_by("-date_calcul")
    serializer_class = SyntheseFinanciereSerializer
    organisation_lookup = "fiche_projet__organisation"


class ObjectifViewSet(EnergieScopedViewSet):
    queryset = Objectif.objects.select_related("organisation").order_by("date_fin", "nom")
    serializer_class = ObjectifSerializer


class IndicateurViewSet(EnergieScopedViewSet):
    queryset = Indicateur.objects.select_related("objectif__organisation").order_by("-periode")
    serializer_class = IndicateurSerializer
    organisation_lookup = "objectif__organisation"


class IndicateurObjectifViewSet(EnergieScopedViewSet):
    queryset = IndicateurObjectif.objects.select_related("objectif__organisation").prefetch_related(
        "indicateurs"
    )
    serializer_class = IndicateurObjectifSerializer
    organisation_lookup = "objectif__organisation"