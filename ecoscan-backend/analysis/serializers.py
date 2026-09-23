from django.utils import timezone
from rest_framework import serializers
from .models import (
    Recommandation, Action, Decision, Livrable, ResultatMetrique,
    ObservationOperationnelle, Anomalie, Hypothese, MemoireStrategique, DocumentEntreprise, OpportuniteFinancement
)
from accounts.serializers import UtilisateurSerializer
from organizations.models import UtilisateurOrganisation


def _verifier_appartenance(request, organisation, nom_champ: str):
    """Vérifie que l'utilisateur connecté est affilié à `organisation` — même
    principe que SiteSerializer.validate_organisation / FicheProjetSerializer.validate_organisation
    dans organizations/serializers.py. PAS de contournement SUPER_ADMIN ici :
    contrairement à l'app organizations, l'app analysis exclut délibérément le
    SUPER_ADMIN de tous les objets métier des tenants (voir AnalyseScopedQuerySetMixin).

    CORRECTIF : RecommandationViewSet, ActionViewSet, DecisionViewSet et
    LivrableViewSet n'ont aucun perform_create — le filtrage par organisation
    (AnalyseScopedQuerySetMixin.get_queryset) ne s'applique qu'en LECTURE.
    Sans cette vérification, n'importe quel utilisateur authentifié pouvait
    créer une Recommandation/Action/Decision/Livrable rattachée à l'Objectif,
    la Recommandation ou la FicheProjet d'une AUTRE organisation, en
    fournissant simplement son identifiant dans la requête.
    """
    if not request or not request.user:
        raise serializers.ValidationError("Authentification requise.")
    est_membre = UtilisateurOrganisation.objects.filter(
        organisation=organisation, utilisateur=request.user
    ).exists()
    if not est_membre:
        raise serializers.ValidationError(
            {nom_champ: "Vous n'avez pas l'autorisation d'agir pour l'organisation propriétaire de cette ressource."}
        )


class RecommandationSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la gestion et le suivi des pistes d'économies d'énergie."""

    class Meta:
        model = Recommandation
        fields = (
            "id", "objectif", "titre", "description", "impact_estime",
            "economie_estimee", "unite", "priorite", "statut",
            "date_echeance", "date_decision",
        )
        read_only_fields = ("id", "statut", "date_decision")

    def validate_objectif(self, value):
        _verifier_appartenance(self.context.get("request"), value.organisation, "objectif")
        return value


class ActionSerializer(serializers.ModelSerializer):
    """Sérialiseur pour le pilotage opérationnel des plans d'actions terrain."""

    class Meta:
        model = Action
        fields = (
            "id", "recommandation", "titre", "description", "statut",
            "responsable", "date_echeance", "date_realisation",
        )
        read_only_fields = ("id", "statut", "date_realisation")

    def validate_recommandation(self, value):
        _verifier_appartenance(self.context.get("request"), value.objectif.organisation, "recommandation")
        return value

    def validate(self, attrs):
        """Vérifie que la date d'échéance de l'action est cohérente."""
        date_echeance = attrs.get("date_echeance")
        if date_echeance and date_echeance < timezone.now():
            raise serializers.ValidationError(
                {"date_echeance": "La date d'échéance ne peut pas être fixée dans le passé."}
            )
        return attrs


class DecisionSerializer(serializers.ModelSerializer):
    """Sérialiseur pour l'archivage formel des arbitrages d'investissements."""

    decideur = UtilisateurSerializer(read_only=True)

    class Meta:
        model = Decision
        fields = ("id", "recommandation", "resultat", "commentaire", "date_decision", "decideur")
        read_only_fields = ("id", "date_decision", "decideur")

    def validate_recommandation(self, value):
        _verifier_appartenance(self.context.get("request"), value.objectif.organisation, "recommandation")
        return value


class LivrableSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la traçabilité des livrables techniques et rapports d'audit."""

    class Meta:
        model = Livrable
        fields = (
            "id", "fiche_projet", "nom", "type", "statut",
            "url_fichier", "version", "date_generation",
        )
        # CORRECTIF : url_fichier n'était pas en lecture seule — un client
        # pouvait poser n'importe quelle URL directement, sans jamais passer
        # par la génération réelle (voir analysis/services/report_service.py).
        read_only_fields = ("id", "statut", "version", "date_generation", "url_fichier")

    def validate_fiche_projet(self, value):
        _verifier_appartenance(self.context.get("request"), value.organisation, "fiche_projet")
        return value


class ResultatMetriqueSerializer(serializers.ModelSerializer):
    """Sérialiseur pour l'Audit Trail immuable des résultats de métriques d'analyse."""

    class Meta:
        model = ResultatMetrique
        fields = (
            "id", "organisation", "compteur", "code_metrique", "version_metrique",
            "valeur", "unite", "periode_debut", "periode_fin", "baseline_type",
            "baseline_valeur", "baseline_nombre_observations", "completude",
            "statut_qualite", "confiance", "sources", "limites", "date_calcul",
        )
        read_only_fields = (
            "id", "organisation", "date_calcul", "version_metrique",
            "completude", "statut_qualite", "confiance",
        )

    def validate(self, attrs):
        """Quality Gate : Applique les validations de cohérence temporelle chronologique."""
        debut = attrs.get("periode_debut")
        fin = attrs.get("periode_fin")

        if debut and fin and debut >= fin:
            raise serializers.ValidationError(
                {"periode_debut": "Cohérence temporelle invalide : la date de début doit être strictement antérieure à la date de fin."}
            )
        return attrs


class ObservationOperationnelleSerializer(serializers.ModelSerializer):
    """Sérialiseur pour les observations terrain journalières d'EcoScan."""

    class Meta:
        model = ObservationOperationnelle
        fields = ("id", "organisation", "auteur", "texte", "date_observation", "creneau", "valide", "date_creation")
        read_only_fields = ("id", "auteur", "valide", "date_creation")

    def validate(self, attrs):
        if not attrs.get("texte"):
            raise serializers.ValidationError({"texte": "Le texte de l'observation ne peut pas être vide."})
        return attrs


class AnomalieSerializer(serializers.ModelSerializer):
    class Meta:
        model = Anomalie
        fields = (
            "id", "organisation", "resultat_metrique", "type", "severite",
            "valeur_observee", "valeur_attendue", "ecart_pourcentage", "statut", "date_detection",
        )
        read_only_fields = fields


class HypotheseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hypothese
        fields = ("id", "anomalie", "texte", "preuves", "confiance", "statut", "genere_par_ia", "date_creation")
        read_only_fields = ("id", "texte", "preuves", "genere_par_ia", "date_creation", "statut", "confiance")


class MemoireStrategiqueSerializer(serializers.ModelSerializer):
    class Meta:
        model = MemoireStrategique
        fields = (
            "id", "organisation", "anomalie", "action", "titre", "signal_initial",
            "hypothese_texte", "action_texte", "impact_attendu_fcfa", "impact_mesure_fcfa",
            "taux_realisation", "statut", "sources", "indexee_rag", "date_creation",
        )
        read_only_fields = fields


class DocumentEntrepriseSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentEntreprise
        fields = (
            "id", "organisation", "depose_par", "titre", "type", "contenu_texte",
            "valide", "valide_par", "date_validation", "indexe_rag", "date_depot",
        )
        read_only_fields = ("id", "depose_par", "valide", "valide_par", "date_validation", "indexe_rag", "date_depot")


class OpportuniteFinancementSerializer(serializers.ModelSerializer):
    class Meta:
        model = OpportuniteFinancement
        fields = "__all__"
        read_only_fields = ("id", "date_ingestion")