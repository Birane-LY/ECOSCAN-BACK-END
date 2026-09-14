from rest_framework import serializers

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
)


class FichierSourceSerializer(serializers.ModelSerializer):
    """Sérialiseur pour le téléversement et le suivi des fichiers sources."""

    class Meta:
        model = FichierSource
        fields = (
            "id",
            "organisation",
            "nom",
            "fichier",
            "chemin_stockage",
            "mime_type",
            "taille_octets",
            "hash",
            "date_depot",
            "depose_par",
        )
        read_only_fields = (
            "id",
            "organisation",
            "nom",
            "mime_type",
            "taille_octets",
            "hash",
            "date_depot",
            "depose_par",
        )


class SourceDonneeSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la configuration des canaux d'acquisition des données."""

    class Meta:
        model = SourceDonnee
        fields = (
            "id",
            "organisation",
            "nom",
            "type",
            "origine",
            "frequence",
            "statut_synchronisation",
            "derniere_synchronisation",
        )
        read_only_fields = ("id", "derniere_synchronisation")


class ImportDonneesSerializer(serializers.ModelSerializer):
    """Sérialiseur pour le pilotage et le suivi des processus d'importation.

    Inclut les champs du pipeline OCR/classification (ocr_statut, scores, données
    extraites, rapport d'analyse) : sans eux, la réponse de l'action `lancer` ne
    contient aucun des résultats que le pipeline vient de calculer.
    """

    class Meta:
        model = ImportDonnees
        fields = (
            "id",
            "fichier_source",
            "organisation",
            "source_donnee",
            "lance_par",
            "nom_fichier",
            "format",
            "type_donnees",
            "nombre_lignes",
            "nombre_erreurs",
            "score_qualite",
            "statut",
            "date_import",
            "ocr_statut",
            "ocr_erreur",
            "score_lisibilite",
            "score_pertinence",
            "donnees_extraites",
            "rapport_analyse",
            "date_traitement",
        )
        # `statut` et tous les champs du pipeline ne doivent être modifiés que par
        # ImportDonneesViewSet.lancer() / annuler_import() — jamais par un PATCH direct
        # d'un client, qui pourrait sinon forger un import "TERMINE" sans OCR réel.
        read_only_fields = (
            "id",
            "date_import",
            "statut",
            "score_qualite",
            "ocr_statut",
            "ocr_erreur",
            "score_lisibilite",
            "score_pertinence",
            "donnees_extraites",
            "rapport_analyse",
            "date_traitement",
        )


class FacteurEmissionSerializer(serializers.ModelSerializer):
    """Sérialiseur pour les coefficients réglementaires de conversion carbone."""

    class Meta:
        model = FacteurEmission
        fields = (
            "id",
            "nom",
            "type_energie",
            "valeur",
            "unite",
            "source_reglementaire",
            "version",
        )
        read_only_fields = ("id",)


class DonneeEnergetiqueSerializer(serializers.ModelSerializer):
    """Sérialiseur pour l'enregistrement et la validation des index de consommation."""

    class Meta:
        model = DonneeEnergetique
        fields = (
            "id",
            "compteur",
            "source_donnee",
            "facteur_emission",
            "valeur",
            "unite",
            "periode_debut",
            "periode_fin",
            "statut_validation",
            "source",
        )
        read_only_fields = ("id",)

    def validate(self, data):
        """Validation temporelle et logique de la donnée de consommation."""
        periode_debut = data.get("periode_debut") or (self.instance.periode_debut if self.instance else None)
        periode_fin = data.get("periode_fin") or (self.instance.periode_fin if self.instance else None)
        compteur = data.get("compteur") or (self.instance.compteur if self.instance else None)
        source_donnee = data.get("source_donnee") or (self.instance.source_donnee if self.instance else None)

        if periode_debut and periode_fin and periode_fin < periode_debut:
            raise serializers.ValidationError(
                {"periode_fin": "La date de fin de la période de consommation ne peut pas être antérieure à sa date de début."}
            )

        if compteur and source_donnee:
            if compteur.site.organisation != source_donnee.organisation:
                raise serializers.ValidationError(
                    "Incohérence détectée : Le compteur et la source de données configurés doivent appartenir à la même organisation."
                )

        return data


class HistoriquePerformanceSerializer(serializers.ModelSerializer):
    """Sérialiseur pour les consolidations historiques de performance d'un audit."""

    class Meta:
        model = HistoriquePerformance
        fields = ("id", "fiche_projet", "periode", "consommation", "emissions", "economie", "unite")
        read_only_fields = ("id",)


class SyntheseFinanciereSerializer(serializers.ModelSerializer):
    """Sérialiseur pour les indicateurs de rentabilité financière et ROI."""

    class Meta:
        model = SyntheseFinanciere
        fields = (
            "id",
            "fiche_projet",
            "cout_energie",
            "economie_estimee",
            "economie_realisee",
            "retour_investissement",
            "date_calcul",
        )
        read_only_fields = ("id", "date_calcul")


class ObjectifSerializer(serializers.ModelSerializer):
    """Sérialiseur pour le suivi des objectifs énergétiques de l'entreprise."""

    class Meta:
        model = Objectif
        fields = (
            "id",
            "organisation",
            "nom",
            "description",
            "type",
            "valeur_cible",
            "unite",
            "date_debut",
            "date_fin",
            "progression_actuelle",
            "prevision",
            "statut",
        )
        read_only_fields = ("id",)

    def validate(self, data):
        """Validation temporelle des horizons d'objectifs."""
        date_debut = data.get("date_debut") or (self.instance.date_debut if self.instance else None)
        date_fin = data.get("date_fin") or (self.instance.date_fin if self.instance else None)

        if date_debut and date_fin and date_fin < date_debut:
            raise serializers.ValidationError(
                {"date_fin": "La date d'échéance de l'objectif ne peut pas être antérieure à sa date de lancement."}
            )
        return data


class IndicateurSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la consultation et le suivi des KPIs énergétiques."""

    class Meta:
        model = Indicateur
        fields = ("id", "objectif", "nom", "type", "valeur", "unite", "periode", "methode_calcul", "date_calcul")
        read_only_fields = ("id", "date_calcul")


class IndicateurObjectifSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la consolidation de performance globale face à un objectif."""

    class Meta:
        model = IndicateurObjectif
        fields = (
            "id",
            "objectif",
            "indicateurs",
            "nom",
            "unite",
            "valeur_initiale",
            "valeur_cible",
            "valeur_actuelle",
            "progression",
        )
        read_only_fields = ("id",)

    def validate(self, data):
        """Vérifie la cohérence de l'indicateur d'objectif par rapport aux sous-indicateurs liés."""
        objectif = data.get("objectif") or (self.instance.objectif if self.instance else None)
        list_indicateurs = data.get("indicateurs") or (list(self.instance.indicateurs.all()) if self.instance else [])

        if objectif and list_indicateurs:
            for indicateur in list_indicateurs:
                if indicateur.objectif != objectif:
                    raise serializers.ValidationError(
                        {"indicateurs": f"L'indicateur '{indicateur.nom}' découle d'un autre objectif. Liaison impossible."}
                    )
        return data