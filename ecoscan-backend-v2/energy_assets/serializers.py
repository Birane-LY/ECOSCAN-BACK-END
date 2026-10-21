from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from analysis.models import Recommandation
from billing.services import BillingAccessService
from organizations.models import Organisation, Site

from .models import (
    ActionVirtuelle,
    Capteur,
    CapteurPresence,
    CommandeEquipement,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    MesurePresence,
    PilotageAutonome,
    ProfilFonctionnement,
    Zone,
)
from .autopilot import equipement_pilotable_en_simulation
from .services import convertir_mesure, demander_commande
from .virtual_actions import simuler_action_virtuelle


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


class CapteurPresenceSerializer(OrganisationAccessibleSerializer):
    class Meta:
        model = CapteurPresence
        fields = (
            "id",
            "zone",
            "identifiant",
            "mode",
            "occupation_simulee",
            "frequence_secondes",
            "statut",
            "derniere_communication",
            "date_creation",
        )
        read_only_fields = ("id", "derniere_communication", "date_creation")

    def validate_zone(self, value):
        if not value.active:
            raise serializers.ValidationError(
                "Un capteur de présence doit être rattaché à une zone active."
            )
        return value

    def validate(self, attrs):
        zone = attrs.get("zone", self.instance.zone if self.instance else None)
        mode = attrs.get("mode", self.instance.mode if self.instance else None)
        occupation_simulee = attrs.get(
            "occupation_simulee",
            self.instance.occupation_simulee if self.instance else None,
        )
        if zone:
            self._valider_site_accessible(zone.site)
        if mode == CapteurPresence.Mode.REAL and occupation_simulee is not None:
            raise serializers.ValidationError(
                {
                    "occupation_simulee": (
                        "Un capteur réel ne peut pas avoir d'état simulé configuré."
                    )
                }
            )
        return attrs


class MesurePresenceSerializer(OrganisationAccessibleSerializer):
    class Meta:
        model = MesurePresence
        fields = (
            "id",
            "capteur",
            "occupee",
            "date_mesure",
            "date_reception",
        )
        read_only_fields = ("id", "date_reception")

    def validate_capteur(self, value):
        if not self._organisations_accessibles().filter(
            pk=value.zone.site.organisation_id
        ).exists():
            raise serializers.ValidationError(
                "Vous n'avez pas accès à l'organisation de cette zone."
            )
        if value.statut != CapteurPresence.Statut.ACTIVE:
            raise serializers.ValidationError("Ce capteur de présence est inactif.")
        return value

    def validate_date_mesure(self, value):
        if value > timezone.now() + timedelta(minutes=5):
            raise serializers.ValidationError(
                "La date de mesure ne peut pas être dans le futur."
            )
        return value


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


class ActionVirtuelleSerializer(OrganisationAccessibleSerializer):
    """Valide et expose un scénario énergétique sans commande physique."""

    duree_heures = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal("24"),
    )

    class Meta:
        model = ActionVirtuelle
        fields = (
            "id",
            "recommandation",
            "equipement",
            "etat_cible",
            "duree_heures",
            "puissance_reference_kw",
            "puissance_scenario_kw",
            "variation_energie_kwh",
            "source_puissance_reference",
            "cree_par",
            "date_simulation",
        )
        read_only_fields = (
            "id",
            "puissance_reference_kw",
            "puissance_scenario_kw",
            "variation_energie_kwh",
            "source_puissance_reference",
            "cree_par",
            "date_simulation",
        )

    def validate_equipement(self, value):
        return self._valider_equipement_accessible(value)

    def validate_recommandation(self, value):
        organisation_id = value.objectif.organisation_id
        if not self._organisations_accessibles().filter(pk=organisation_id).exists():
            raise serializers.ValidationError(
                "Vous n'avez pas accès à l'organisation de cette recommandation."
            )
        return value

    def validate(self, attrs):
        equipement = attrs.get(
            "equipement",
            self.instance.equipement if self.instance else None,
        )
        recommandation = attrs.get(
            "recommandation",
            self.instance.recommandation if self.instance else None,
        )
        if (
            equipement
            and recommandation
            and equipement.site.organisation_id
            != recommandation.objectif.organisation_id
        ):
            raise serializers.ValidationError(
                {
                    "recommandation": (
                        "La recommandation et l'équipement doivent appartenir "
                        "à la même organisation."
                    )
                }
            )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        try:
            return simuler_action_virtuelle(
                recommandation=validated_data["recommandation"],
                equipement=validated_data["equipement"],
                etat_cible=validated_data["etat_cible"],
                duree_heures=validated_data["duree_heures"],
                utilisateur=request.user,
            )
        except ValueError as exc:
            raise serializers.ValidationError({"equipement": str(exc)}) from exc


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


class PilotageAutonomeSerializer(serializers.ModelSerializer):
    equipements_autorises = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Equipement.objects.all(),
        required=False,
    )
    zones_autorisees = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Zone.objects.all(),
        required=False,
    )

    class Meta:
        model = PilotageAutonome
        fields = (
            "site",
            "actif",
            "arret_urgence",
            "plafond_puissance_kw",
            "horaires_actifs",
            "heure_debut",
            "heure_fin",
            "confort_actif",
            "temperature_min_c",
            "temperature_max_c",
            "temperature_simulee_c",
            "strategie_automatique",
            "occupation_active",
            "equipements_autorises",
            "zones_autorisees",
            "derniere_evaluation",
            "date_maj",
        )
        read_only_fields = ("site", "derniere_evaluation", "date_maj")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        value = lambda field, default=None: attrs.get(
            field,
            getattr(instance, field, default) if instance else default,
        )
        site = self.context["site"]
        equipements = attrs.get(
            "equipements_autorises",
            list(instance.equipements_autorises.all()) if instance else [],
        )
        zones = attrs.get(
            "zones_autorisees",
            list(instance.zones_autorisees.all()) if instance else [],
        )
        invalid_equipments = [
            equipement
            for equipement in equipements
            if equipement.site_id != site.id
            or not equipement.monitoring_active
            or not equipement_pilotable_en_simulation(equipement)
            or equipement.criticite
            in (Equipement.Criticite.HAUTE, Equipement.Criticite.CRITIQUE)
        ]
        if invalid_equipments:
            raise serializers.ValidationError(
                {
                    "equipements_autorises": (
                        "Seuls les équipements surveillés, du site sélectionné et "
                        "de criticité basse ou moyenne avec des capteurs exclusivement "
                        "simulés peuvent être autorisés."
                    )
                }
            )

        invalid_zones = [
            zone for zone in zones if zone.site_id != site.id or not zone.active
        ]
        if invalid_zones:
            raise serializers.ValidationError(
                {
                    "zones_autorisees": (
                        "Seules les zones actives du site sélectionné peuvent être "
                        "autorisées."
                    )
                }
            )

        if value("occupation_active") and not zones:
            raise serializers.ValidationError(
                {
                    "zones_autorisees": (
                        "Autorisez au moins une zone pour activer la stratégie "
                        "d'occupation."
                    )
                }
            )

        heure_debut = value("heure_debut")
        heure_fin = value("heure_fin")
        if value("horaires_actifs") and (
            heure_debut is None or heure_fin is None or heure_debut == heure_fin
        ):
            raise serializers.ValidationError(
                {"heure_fin": "Configurez deux heures différentes pour la plage horaire."}
            )

        plafond = value("plafond_puissance_kw")
        if plafond is not None and plafond <= 0:
            raise serializers.ValidationError(
                {"plafond_puissance_kw": "Le plafond doit être supérieur à zéro."}
            )

        temperature_min = value("temperature_min_c")
        temperature_max = value("temperature_max_c")
        temperature_simulee = value("temperature_simulee_c")
        if value("confort_actif"):
            if (
                temperature_min is None
                or temperature_max is None
                or temperature_simulee is None
            ):
                raise serializers.ValidationError(
                    "Renseignez les deux limites de confort et la température simulée."
                )
            if temperature_min >= temperature_max:
                raise serializers.ValidationError(
                    {"temperature_max_c": "La limite maximale doit dépasser la minimale."}
                )
            if not any(
                "CLIMAT" in equipement.categorie.upper()
                for equipement in equipements
            ):
                raise serializers.ValidationError(
                    {
                        "confort_actif": (
                            "Autorisez un climatiseur non critique pour utiliser "
                            "la stratégie de confort."
                        )
                    }
                )

        if value("actif") and not value("arret_urgence"):
            if not equipements:
                raise serializers.ValidationError(
                    {
                        "equipements_autorises": (
                            "Autorisez au moins un équipement non critique."
                        )
                    }
                )
            if (
                plafond is None
                and not value("horaires_actifs")
                and not value("confort_actif")
                and not value("occupation_active")
            ):
                raise serializers.ValidationError(
                    "Configurez au moins une stratégie avant d'activer l'autonomie."
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


class ChargeEquipementSerializer(serializers.Serializer):
    equipment_id = serializers.UUIDField()
    name = serializers.CharField()
    zone_id = serializers.UUIDField(allow_null=True)
    zone_name = serializers.CharField(allow_null=True)
    power_kw = serializers.DecimalField(max_digits=12, decimal_places=3)
    reported_state = serializers.ChoiceField(choices=Equipement.Etat.choices)
    is_online = serializers.BooleanField()
    telemetry_mode = serializers.ChoiceField(
        choices=("NONE", "REAL", "SIMULATED", "MIXED")
    )


class ConsommateurPrincipalSerializer(ChargeEquipementSerializer):
    share_percent = serializers.DecimalField(max_digits=5, decimal_places=1)


class MonitoringSummarySerializer(serializers.Serializer):
    site_id = serializers.UUIDField()
    site_name = serializers.CharField()
    telemetry_mode = serializers.ChoiceField(
        choices=("NONE", "REAL", "SIMULATED", "MIXED")
    )
    current_power_kw = serializers.DecimalField(max_digits=16, decimal_places=3)
    equipment = ChargeEquipementSerializer(many=True)
    today_energy_kwh = serializers.DecimalField(
        max_digits=20,
        decimal_places=6,
        allow_null=True,
    )
    today_energy_since = serializers.DateTimeField(allow_null=True)
    yesterday_energy_kwh = serializers.DecimalField(
        max_digits=20,
        decimal_places=6,
        allow_null=True,
    )
    month_energy_kwh = serializers.DecimalField(
        max_digits=20,
        decimal_places=6,
        allow_null=True,
    )
    energy_coverage = serializers.DictField(
        child=serializers.DictField(child=serializers.IntegerField())
    )
    monitored_equipment_count = serializers.IntegerField()
    active_equipment_count = serializers.IntegerField()
    offline_equipment_count = serializers.IntegerField()
    top_consumer = ConsommateurPrincipalSerializer(allow_null=True)
    latest_measurement_at = serializers.DateTimeField(allow_null=True)


class CurrentLoadSerializer(serializers.Serializer):
    site_id = serializers.UUIDField()
    site_name = serializers.CharField()
    current_power_kw = serializers.DecimalField(max_digits=16, decimal_places=3)
    latest_measurement_at = serializers.DateTimeField(allow_null=True)
    equipment = ChargeEquipementSerializer(many=True)


class TelemetryHistorySerializer(serializers.ModelSerializer):
    telemetry_id = serializers.UUIDField(source="id", read_only=True)
    sensor_id = serializers.UUIDField(source="capteur_id", read_only=True)
    sensor_identifier = serializers.CharField(source="capteur.identifiant", read_only=True)
    sensor_type = serializers.CharField(source="capteur.type", read_only=True)
    source = serializers.CharField(source="capteur.mode", read_only=True)
    timestamp = serializers.DateTimeField(source="date_mesure", read_only=True)
    received_at = serializers.DateTimeField(source="date_reception", read_only=True)
    value = serializers.DecimalField(
        source="valeur",
        max_digits=20,
        decimal_places=6,
        read_only=True,
    )
    unit = serializers.CharField(source="unite", read_only=True)

    class Meta:
        model = MesureCapteur
        fields = (
            "telemetry_id",
            "sensor_id",
            "sensor_identifier",
            "sensor_type",
            "source",
            "timestamp",
            "received_at",
            "value",
            "unit",
        )


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
