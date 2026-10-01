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
    PointSuiviEnergetique,
    ReleveRituelEnergetique,
    RechargeRituelWoyofal,
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


class PointSuiviEnergetiqueSerializer(serializers.ModelSerializer):
    site_nom = serializers.CharField(source="site.nom", read_only=True, default="")
    compteur_reference = serializers.CharField(source="compteur.reference", read_only=True, default="")

    class Meta:
        model = PointSuiviEnergetique
        fields = (
            "id", "organisation", "site", "site_nom", "compteur",
            "compteur_reference", "nom", "mode_mesure", "date_creation",
        )
        read_only_fields = ("id", "date_creation")

    def validate(self, attrs):
        request = self.context.get("request")
        organisation = attrs.get("organisation", getattr(self.instance, "organisation", None))
        site = attrs.get("site", getattr(self.instance, "site", None))
        compteur = attrs.get("compteur", getattr(self.instance, "compteur", None))
        if request is None or organisation is None:
            raise serializers.ValidationError({"organisation": "Une organisation est obligatoire."})
        if not organisation.membres.filter(utilisateur=request.user).exists():
            raise serializers.ValidationError({"organisation": "Cette organisation ne vous est pas rattachée."})
        if site and site.organisation_id != organisation.id:
            raise serializers.ValidationError({"site": "Ce site n’appartient pas à l’organisation sélectionnée."})
        if compteur and (site is None or compteur.site_id != site.id):
            raise serializers.ValidationError({"compteur": "Un compteur nécessite le site auquel il est rattaché."})
        return attrs


class ReleveRituelEnergetiqueSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReleveRituelEnergetique
        fields = (
            "id", "client_id", "point_suivi", "date_releve",
            "creneau", "valeur_kwh", "note", "observe_le",
        )
        read_only_fields = ("id",)

    def validate_point_suivi(self, point_suivi):
        request = self.context.get("request")
        if request is None or not point_suivi.organisation.membres.filter(utilisateur=request.user).exists():
            raise serializers.ValidationError("Point de suivi hors de votre organisation.")
        return point_suivi

    def validate(self, attrs):
        if self.instance:
            raise serializers.ValidationError("Un relevé enregistré ne peut pas être modifié.")

        date_releve = attrs.get("date_releve")
        creneau = attrs.get("creneau")
        aujourdhui = timezone.localdate()
        if date_releve != aujourdhui:
            raise serializers.ValidationError({
                "date_releve": "Les relevés ne peuvent être saisis que pour la journée en cours."
            })

        maintenant = timezone.localtime()
        minutes_actuelles = maintenant.hour * 60 + maintenant.minute
        creneaux = list(ReleveRituelEnergetique.Creneau.values)
        index = creneaux.index(creneau)
        heure_debut = int(creneau[:2]) * 60 + int(creneau[3:])
        heure_fin = (
            int(creneaux[index + 1][:2]) * 60 + int(creneaux[index + 1][3:])
            if index + 1 < len(creneaux)
            else 24 * 60
        )
        if minutes_actuelles < heure_debut or minutes_actuelles >= heure_fin:
            raise serializers.ValidationError({
                "creneau": "L’heure de saisie de ce créneau est dépassée ou n’est pas encore arrivée."
            })
        return attrs


class RechargeRituelWoyofalSerializer(serializers.ModelSerializer):
    class Meta:
        model = RechargeRituelWoyofal
        fields = (
            "id", "client_id", "point_suivi", "montant_fcfa",
            "kwh_credites", "effectuee_le", "note",
        )
        read_only_fields = ("id",)

    def validate_point_suivi(self, point_suivi):
        request = self.context.get("request")
        if request is None or not point_suivi.organisation.membres.filter(utilisateur=request.user).exists():
            raise serializers.ValidationError("Point de suivi hors de votre organisation.")
        if point_suivi.mode_mesure != PointSuiviEnergetique.ModeMesure.SOLDE_WOYOFAL:
            raise serializers.ValidationError("Les recharges ne concernent que les points Woyofal.")
        return point_suivi
