from rest_framework import serializers
from django.utils import timezone

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
        read_only_fields = (
            "id",
            "derniere_synchronisation",
        )
        extra_kwargs = {
            "organisation": {
                "required": False,
                "allow_null": True,
            }
        }


class ImportDonneesSerializer(serializers.ModelSerializer):
    """Sérialiseur pour le pilotage et le suivi des processus d'importation."""

    class Meta:
        model = ImportDonnees

        fields = (
            "id",
            "fichier_source",
            "organisation",
            "source_donnee",
            "compteur",
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

        read_only_fields = (
            "id",
            "organisation",
            "lance_par",
            "nom_fichier",
            "format",
            "type_donnees",
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

    def validate_compteur(self, value):
        if value is None:
            return value

        return value


class FacteurEmissionSerializer(serializers.ModelSerializer):
    """Sérialiseur pour les coefficients réglementaires."""

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

    class Meta:
        model = DonneeEnergetique

        fields = "__all__"

        read_only_fields = (
            "id",
            "date_creation",
            "date_modification",
        )

    def validate(self, attrs):
        """
        Empêche l'enregistrement de plusieurs relevés
        pour le même compteur, le même créneau et le
        même jour.

        Exemple :

        23/09 - matin -> autorisé
        23/09 - matin -> refusé
        24/09 - matin -> autorisé
        """

        compteur = attrs.get("compteur")

        if not compteur:
            raise serializers.ValidationError({
                "compteur": (
                    "Le compteur est obligatoire."
                )
            })


        creneau = attrs.get("creneau")

        if not creneau:
            raise serializers.ValidationError({
                "creneau": (
                    "Le créneau est obligatoire."
                )
            })


        date_releve = attrs.get(
            "date_releve"
        )


        if date_releve:
            if hasattr(
                date_releve,
                "date",
            ):
                date_locale = timezone.localtime(
                    date_releve
                ).date()
            else:
                date_locale = date_releve

        else:
            date_locale = timezone.localtime(
                timezone.now()
            ).date()

        queryset = (
            DonneeEnergetique.objects.filter(
                compteur=compteur,
                date_releve=date_locale,
                creneau=creneau,
            )
        )

        if self.instance:
            queryset = queryset.exclude(
                pk=self.instance.pk
            )

        if queryset.exists():
            raise serializers.ValidationError({
                "creneau": (
                    "Ce créneau a déjà été enregistré "
                    "aujourd'hui."
                )
            })

        attrs["date_releve"] = date_locale


        return attrs


class HistoriquePerformanceSerializer(serializers.ModelSerializer):

    class Meta:
        model = HistoriquePerformance
        fields = (
            "id",
            "fiche_projet",
            "periode",
            "consommation",
            "emissions",
            "economie",
            "unite",
        )
        read_only_fields = ("id",)


class SyntheseFinanciereSerializer(serializers.ModelSerializer):

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
        read_only_fields = (
            "id",
            "date_calcul",
        )


class ObjectifSerializer(serializers.ModelSerializer):

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
        date_debut = data.get(
            "date_debut"
        ) or (
            self.instance.date_debut
            if self.instance
            else None
        )

        date_fin = data.get(
            "date_fin"
        ) or (
            self.instance.date_fin
            if self.instance
            else None
        )

        if (
            date_debut
            and date_fin
            and date_fin < date_debut
        ):
            raise serializers.ValidationError(
                {
                    "date_fin": (
                        "La date d'échéance de "
                        "l'objectif ne peut pas être "
                        "antérieure à sa date de lancement."
                    )
                }
            )

        return data


class IndicateurSerializer(serializers.ModelSerializer):

    class Meta:
        model = Indicateur
        fields = (
            "id",
            "objectif",
            "nom",
            "type",
            "valeur",
            "unite",
            "periode",
            "methode_calcul",
            "date_calcul",
        )
        read_only_fields = (
            "id",
            "date_calcul",
        )


class IndicateurObjectifSerializer(serializers.ModelSerializer):

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
        objectif = data.get(
            "objectif"
        ) or (
            self.instance.objectif
            if self.instance
            else None
        )

        list_indicateurs = data.get(
            "indicateurs"
        ) or (
            list(
                self.instance.indicateurs.all()
            )
            if self.instance
            else []
        )

        if objectif and list_indicateurs:
            for indicateur in list_indicateurs:
                if indicateur.objectif != objectif:
                    raise serializers.ValidationError(
                        {
                            "indicateurs": (
                                f"L'indicateur "
                                f"'{indicateur.nom}' "
                                "découle d'un autre "
                                "objectif. Liaison impossible."
                            )
                        }
                    )

        return data


class AchatWoyofalSerializer(serializers.ModelSerializer):

    class Meta:
        model = AchatWoyofal
        fields = (
            "id",
            "client_id",
            "compteur",
            "montant_fcfa",
            "kwh_credites",
            "kwh_predits",
            "date_achat",
            "source",
        )
        read_only_fields = ("id",)

    def validate_compteur(self, compteur):
        user = self.context["request"].user

        if not compteur.site.organisation.membres.filter(
            utilisateur=user
        ).exists():
            raise serializers.ValidationError(
                "Compteur hors de votre organisation."
            )

        return compteur


class ReleveSoldeSerializer(serializers.ModelSerializer):

    class Meta:
        model = ReleveSolde
        fields = (
            "id",
            "client_id",
            "compteur",
            "kwh_restants",
            "date_releve",
        )
        read_only_fields = ("id",)

    def validate_compteur(self, compteur):
        user = self.context["request"].user

        if not compteur.site.organisation.membres.filter(
            utilisateur=user
        ).exists():
            raise serializers.ValidationError(
                "Compteur hors de votre organisation."
            )

        return compteur
