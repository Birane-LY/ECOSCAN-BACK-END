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

    objectif_nom = serializers.CharField(source="objectif.nom", read_only=True)
    objectif_valeur_cible = serializers.DecimalField(
        source="objectif.valeur_cible", max_digits=18, decimal_places=6, read_only=True
    )
    objectif_unite = serializers.CharField(source="objectif.unite", read_only=True)

    class Meta:
        model = Recommandation
        fields = (
            "id", "objectif", "objectif_nom", "objectif_valeur_cible", "objectif_unite",
            "anomalie", "titre", "description", "impact_estime",
            "economie_estimee", "unite", "priorite", "statut",
            "date_echeance", "date_decision",
        )
 
        read_only_fields = ("id", "anomalie", "statut", "date_decision")

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
            "economie_realisee_fcfa", "taux_realisation_impact", "date_mesure_impact",
        )
        read_only_fields = (
            "id", "statut", "date_realisation",
            "economie_realisee_fcfa", "taux_realisation_impact", "date_mesure_impact",
        )

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
            "id", "organisation", "fiche_projet", "memoire", "nom", "type", "statut",
            "url_fichier", "version", "date_generation",
        )
        
        read_only_fields = ("id", "statut", "version", "date_generation", "url_fichier")

    def validate(self, attrs):
        request = self.context.get("request")
        organisation = attrs.get("organisation", getattr(self.instance, "organisation", None))
        fiche_projet = attrs.get("fiche_projet", getattr(self.instance, "fiche_projet", None))
        memoire = attrs.get("memoire", getattr(self.instance, "memoire", None))

        if organisation is None and fiche_projet:
            organisation = fiche_projet.organisation
            attrs["organisation"] = organisation
        if organisation is None:
            raise serializers.ValidationError({"organisation": "Ce champ est obligatoire."})
        _verifier_appartenance(request, organisation, "organisation")
        if fiche_projet and fiche_projet.organisation_id != organisation.id:
            raise serializers.ValidationError({"fiche_projet": "Le projet doit appartenir à l'organisation du rapport."})
        if memoire and memoire.organisation_id != organisation.id:
            raise serializers.ValidationError({"memoire": "La mémoire doit appartenir à l'organisation du rapport."})
        return attrs


class ResultatMetriqueSerializer(serializers.ModelSerializer):
    """Sérialiseur pour les résultats d'analyses et décodeur de facture Senelec."""
    valeur_affichee = serializers.SerializerMethodField()
    donnees_facture = serializers.SerializerMethodField()

    class Meta:
        model = ResultatMetrique
        fields = '__all__'

    def get_valeur_affichee(self, obj):
        if obj.valeur is None:
            return None
        return f"{obj.valeur.normalize():f}"

    def get_donnees_facture(self, obj):
        """Décode la facture Senelec liée pour l'afficher en clair à la PME."""
        if not obj.fichier_source or not hasattr(obj.fichier_source, "import_donnees"):
            return None

        extraits = obj.fichier_source.import_donnees.donnees_extraites or {}
        if not extraits:
            return None

        conso = extraits.get("consommation_kwh") or (float(obj.valeur) if obj.valeur else 0)
        montant = extraits.get("montant_net_paye") or 0
        jours = extraits.get("nombre_jours") or 60
        t3 = extraits.get("consommation_tranche_3") or 0
        tva = extraits.get("montant_tva") or 0

        cout_jour = round(montant / jours) if jours else 0
        conso_jour = round(conso / jours, 1) if jours else 0
        part_t3 = round((t3 / conso * 100), 1) if conso else 0

        return {
            "numero_facture": extraits.get("numero_facture"),
            "tarif": extraits.get("tarif") or "PMP",
            "periode_debut": extraits.get("periode_debut"),
            "periode_fin": extraits.get("periode_fin"),
            "nombre_jours": jours,
            "consommation_totale": conso,
            "montant_net_paye": montant,
            "cout_par_jour": cout_jour,
            "conso_par_jour": conso_jour,
            "consommation_tranche_3": t3,
            "part_tranche_3_pct": part_t3,
            "montant_tva": tva,
            "alerte_tranche_3": part_t3 > 50,
        }

    def validate(self, attrs):
        debut = attrs.get("periode_debut")
        fin = attrs.get("periode_fin")
        if debut and fin and debut >= fin:
            raise serializers.ValidationError(
                {"periode_debut": "La date de début doit être strictement antérieure à la date de fin."}
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
    recommandations = RecommandationSerializer(many=True, read_only=True)

    class Meta:
        model = Anomalie
        fields = (
            "id", "organisation", "resultat_metrique", "type", "severite",
            "valeur_observee", "valeur_attendue", "ecart_pourcentage", "statut", "date_detection",
            "recommandations",
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
