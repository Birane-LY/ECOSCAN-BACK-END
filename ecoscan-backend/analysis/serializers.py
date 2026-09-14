from django.utils import timezone
from rest_framework import serializers
from .models import (
    Recommandation, Action, Decision, Livrable, ResultatMetrique,
    ObservationOperationnelle, Anomalie, Hypothese, MemoireStrategique, DocumentEntreprise,
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


class ActionSerializer(serializers.ModelSerializer):
    """Sérialiseur pour le pilotage opérationnel des plans d'actions terrain."""

    class Meta:
        model = Action
        fields = (
            "id", "recommandation", "titre", "description", "statut",
            "responsable", "date_echeance", "date_realisation",
        )
        read_only_fields = ("id", "statut", "date_realisation")

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

    class Meta:
        model = Decision
        fields = ("id", "recommandation", "resultat", "commentaire", "date_decision", "decideur")
        read_only_fields = ("id", "date_decision", "decideur")


class LivrableSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la traçabilité des livrables techniques et rapports d'audit."""

    class Meta:
        model = Livrable
        fields = (
            "id", "fiche_projet", "nom", "type", "statut",
            "url_fichier", "version", "date_generation",
        )
        read_only_fields = ("id", "statut", "version", "date_generation")


class ResultatMetriqueSerializer(serializers.ModelSerializer):
    """Sérialiseur pour l'Audit Trail immuable des résultats de métriques d'analyse.

    Garantit qu'une métrique persistée ne peut être falsifiée ou réécrite rétroactivement.
    """

    class Meta:
        model = ResultatMetrique
        fields = (
            "id", "organisation", "compteur", "code_metrique", "version_metrique",
            "valeur", "unite", "periode_debut", "periode_fin", "baseline_type",
            "baseline_valeur", "baseline_nombre_observations", "completude",
            "statut_qualite", "confiance", "sources", "limites", "date_calcul",
        )
        # RIGUEUR ANALYTIQUE : Un résultat de calcul persisté est immuable
        read_only_fields = (
            "id", "organisation", "date_calcul", "version_metrique",
            "completude", "statut_qualite", "confiance",
        )

    def validate(self, attrs):
        """Quality Gate 3 : Applique les validations de cohérence temporelle chronologique."""
        debut = attrs.get("periode_debut")
        fin = attrs.get("periode_fin")

        if debut and fin and debut >= fin:
            raise serializers.ValidationError(
                {"periode_debut": "Cohérence temporelle invalide : la date de début doit être strictement antérieure à la date de fin."}
            )
        return attrs


class ObservationOperationnelleSerializer(serializers.ModelSerializer):
    """Le contenu (texte, date, créneau) est libre à la saisie, mais `valide` est
    verrouillé : seule une action dédiée (voir ObservationOperationnelleViewSet)
    doit pouvoir faire passer une observation à `valide=True`, jamais un simple
    PATCH — sinon n'importe quel auteur pourrait auto-valider sa propre note."""

    class Meta:
        model = ObservationOperationnelle
        fields = ("id", "organisation", "auteur", "texte", "date_observation", "creneau", "valide", "date_creation")
        read_only_fields = ("id", "auteur", "valide", "date_creation")


class AnomalieSerializer(serializers.ModelSerializer):
    class Meta:
        model = Anomalie
        fields = (
            "id", "organisation", "resultat_metrique", "type", "severite",
            "valeur_observee", "valeur_attendue", "ecart_pourcentage", "statut", "date_detection",
        )
        read_only_fields = fields  # créées uniquement par anomaly_service, jamais via l'API


class HypotheseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hypothese
        fields = ("id", "anomalie", "texte", "preuves", "confiance", "statut", "genere_par_ia", "date_creation")
        # `statut` et `confiance` ne changent que via confirmer/rejeter (voir
        # HypotheseViewSet) — jamais par PATCH direct, pour garder une trace
        # explicite de QUI a confirmé une hypothèse et QUAND (dans le journal
        # d'audit), pas juste un champ modifié silencieusement.
        read_only_fields = ("id", "texte", "preuves", "genere_par_ia", "date_creation", "statut", "confiance")


class MemoireStrategiqueSerializer(serializers.ModelSerializer):
    class Meta:
        model = MemoireStrategique
        fields = (
            "id", "organisation", "anomalie", "action", "titre", "signal_initial",
            "hypothese_texte", "action_texte", "impact_attendu_fcfa", "impact_mesure_fcfa",
            "taux_realisation", "statut", "sources", "indexee_rag", "date_creation",
        )
        read_only_fields = fields  # créées uniquement par memory_service


class DocumentEntrepriseSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentEntreprise
        fields = (
            "id", "organisation", "depose_par", "titre", "type", "contenu_texte",
            "valide", "valide_par", "date_validation", "indexe_rag", "date_depot",
        )
        read_only_fields = ("id", "depose_par", "valide", "valide_par", "date_validation", "indexe_rag", "date_depot")