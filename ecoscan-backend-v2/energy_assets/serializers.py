from rest_framework import serializers

from billing.services import BillingAccessService
from organizations.models import Organisation, Site

from .models import (
    Capteur,
    CommandeEquipement,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    ProfilFonctionnement,
    Zone,
)
from .services import convertir_mesure, demander_commande


class OrganisationAccessibleSerializer(serializers.ModelSerializer):
    """Centralise la validation des objets rattachés aux sites accessibles."""

    def _organisations_accessibles(self):
        request = self.context.get("request")
        if not request or not request.user or not request.user.is_authenticated:
            raise serializers.ValidationError("Authentification requise.")
        if getattr(request.user, "role", None) == "SUPER_ADMIN":
            return Organisation.objects.none()
        return BillingAccessService().organisations_avec_acces(request.user)

    def _valider_site_accessible(self, site):
        if not self._organisations_accessibles().filter(pk=site.organisation_id).exists():
            raise serializers.ValidationError(
                "Vous n'avez pas accès à l'organisation de ce site."
            )
        return site

    def _valider_equipement_accessible(self, equipement):
        if not self._organisations_accessibles().filter(
            pk=equipement.site.organisation_id
        ).exists():
            raise serializers.ValidationError(
                "Vous n'avez pas accès à l'organisation de cet équipement."
            )
        return equipement


class ZoneSerializer(OrganisationAccessibleSerializer):
    """Sérialiseur pour les zones d'un site."""

    class Meta:
        model = Zone
        fields = ("id", "site", "nom", "description", "active", "date_creation")
        read_only_fields = ("id", "date_creation")

    def validate_site(self, value):
        return self._valider_site_accessible(value)


class CapteurSerializer(OrganisationAccessibleSerializer):
    """Sérialiseur pour les capteurs associés à un équipement."""

    class Meta:
        model = Capteur
        fields = (
            "id",
            "equipement",
            "identifiant",
            "type",
            "mode",
            "frequence_secondes",
            "statut",
            "derniere_communication",
            "date_creation",
        )
        read_only_fields = ("id", "derniere_communication", "date_creation")

    def validate_equipement(self, value):
        return self._valider_equipement_accessible(value)


class MesureCapteurSerializer(OrganisationAccessibleSerializer):
    """Sérialiseur pour l'ingestion des mesures des capteurs."""

    class Meta:
        model = MesureCapteur
        fields = (
            "id",
            "capteur",
            "valeur",
            "unite",
            "date_mesure",
            "date_reception",
        )
        read_only_fields = ("id", "date_reception")

    def validate_capteur(self, value):
        self._valider_equipement_accessible(value.equipement)
        if value.statut != Capteur.Statut.ACTIVE:
            raise serializers.ValidationError("Ce capteur est inactif.")
        return value

    def validate(self, attrs):
        capteur = attrs.get("capteur", self.instance.capteur if self.instance else None)
        valeur = attrs.get("valeur", self.instance.valeur if self.instance else None)
        unite = attrs.get("unite", self.instance.unite if self.instance else None)
        if capteur and valeur is not None and unite is not None:
            try:
                convertir_mesure(capteur.type, valeur, unite)
            except ValueError as exc:
                raise serializers.ValidationError(str(exc)) from exc
        return attrs


class CommandeEquipementSerializer(OrganisationAccessibleSerializer):
    """Expose la demande et le suivi des commandes sans accès direct au transport."""

    class Meta:
        model = CommandeEquipement
        fields = (
            "id",
            "equipement",
            "action",
            "statut",
            "demande_par",
            "date_creation",
            "date_envoi",
            "date_finalisation",
            "detail_echec",
        )
        read_only_fields = (
            "id",
            "statut",
            "demande_par",
            "date_creation",
            "date_envoi",
            "date_finalisation",
            "detail_echec",
        )

    def validate_equipement(self, value):
        return self._valider_equipement_accessible(value)

    def create(self, validated_data):
        request = self.context["request"]
        return demander_commande(
            equipement=validated_data["equipement"],
            action=validated_data["action"],
            utilisateur=request.user,
        )


class ProfilFonctionnementSerializer(OrganisationAccessibleSerializer):
    """Sérialiseur pour les créneaux de fonctionnement d'un équipement."""

    class Meta:
        model = ProfilFonctionnement
        fields = (
            "id",
            "equipement",
            "nom",
            "jour_semaine",
            "heure_debut",
            "heure_fin",
            "actif",
        )
        read_only_fields = ("id",)

    def validate_equipement(self, value):
        return self._valider_equipement_accessible(value)

    def validate(self, attrs):
        heure_debut = attrs.get(
            "heure_debut",
            self.instance.heure_debut if self.instance else None,
        )
        heure_fin = attrs.get(
            "heure_fin",
            self.instance.heure_fin if self.instance else None,
        )
        if heure_debut and heure_fin and heure_fin <= heure_debut:
            raise serializers.ValidationError(
                {"heure_fin": "L'heure de fin doit être postérieure à l'heure de début."}
            )
        return attrs


class EtatEquipementSerializer(serializers.ModelSerializer):
    """Expose en lecture seule l'état courant d'un équipement."""

    class Meta:
        model = EtatEquipement
        fields = (
            "id",
            "equipement",
            "etat_souhaite",
            "etat_rapporte",
            "puissance_actuelle_kw",
            "energie_cumulee_kwh",
            "statut_synchronisation",
            "date_etat_souhaite",
            "date_etat_rapporte",
            "date_maj",
        )
        read_only_fields = fields


class EquipementSerializer(OrganisationAccessibleSerializer):
    """Sérialiseur pour les équipements et leur état de monitoring courant."""

    capteurs = CapteurSerializer(many=True, read_only=True)
    etat = EtatEquipementSerializer(read_only=True)

    class Meta:
        model = Equipement
        fields = (
            "id",
            "site",
            "zone",
            "nom",
            "categorie",
            "puissance_nominale_kw",
            "puissance_veille_kw",
            "quantite",
            "etat_operationnel",
            "criticite",
            "monitoring_active",
            "date_creation",
            "capteurs",
            "etat",
        )
        read_only_fields = ("id", "date_creation")

    def validate_site(self, value):
        return self._valider_site_accessible(value)

    def validate(self, attrs):
        site = attrs.get("site", self.instance.site if self.instance else None)
        zone = attrs.get("zone", self.instance.zone if self.instance else None)
        if zone and site and zone.site_id != site.id:
            raise serializers.ValidationError(
                {"zone": "La zone doit appartenir au site de l'équipement."}
            )
        return attrs
