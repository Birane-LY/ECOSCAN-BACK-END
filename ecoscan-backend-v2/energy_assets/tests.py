from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from analysis.models import Recommandation
from billing.models import Abonnement, Plan
from energy.models import Objectif
from organizations.models import Organisation, Site, UtilisateurOrganisation

from .models import (
    ActionVirtuelle,
    Capteur,
    CommandeEquipement,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    ProfilFonctionnement,
    Zone,
)
from .services import (
    confirmer_commande,
    demander_commande,
    echouer_commande,
    marquer_commande_envoyee,
)
from .simulation import (
    generer_mesure_simulee,
    generer_mesures_capteurs_simules,
)


class EnergyAssetApiTests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="energy-assets@example.com",
            password="StrongPassword123!",
            nom="Gestionnaire",
            role="ADMIN_ORGANISATION",
            actif=True,
        )
        self.organisation = self.create_organisation("Organisation autorisée")
        UtilisateurOrganisation.objects.create(
            organisation=self.organisation,
            utilisateur=self.user,
        )
        self.site = self.create_site(self.organisation, "Site principal")
        plan = Plan.objects.create(
            code="energy-assets-test",
            nom="Plan test",
            prix_mensuel="10000",
        )
        maintenant = timezone.now()
        Abonnement.objects.create(
            organisation=self.organisation,
            plan=plan,
            statut=Abonnement.Statut.ACTIVE,
            debut=maintenant,
            fin_periode=maintenant + timedelta(days=30),
            fournisseur=Abonnement.Fournisseur.MANUAL,
        )
        self.client.force_authenticate(self.user)

    @staticmethod
    def create_organisation(nom):
        return Organisation.objects.create(
            nom=nom,
            secteur="Commerce",
            localisation="Dakar",
            statut=Organisation.Statut.ACTIVE,
        )

    @staticmethod
    def create_site(organisation, nom):
        return Site.objects.create(
            organisation=organisation,
            nom=nom,
            adresse="Dakar",
            pays="Sénégal",
            fuseau_horaire="Africa/Dakar",
        )

    def create_recommendation(self, organisation=None):
        maintenant = timezone.now()
        objectif = Objectif.objects.create(
            organisation=organisation or self.organisation,
            nom="Réduction des consommations",
            type="ENERGIE",
            valeur_cible="100.000000",
            unite="kWh",
            date_debut=maintenant,
            date_fin=maintenant + timedelta(days=90),
        )
        return Recommandation.objects.create(
            objectif=objectif,
            titre="Réduire la consommation hors horaires",
            description="Arrêter l'équipement lorsque le site est fermé.",
            impact_estime="100.000000",
            economie_estimee="100.000000",
            unite="kWh",
        )

    def test_site_monitoring_summary_calculates_energy_from_cumulative_readings(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Compteur général",
            categorie="COMPTEUR",
            puissance_nominale_kw="5.000",
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="ENERGY-SITE-001",
            type="ENERGY",
        )
        with timezone.override("Africa/Dakar"):
            now = timezone.make_aware(datetime(2025, 5, 15, 12))
            readings = (
                ("20.000000", datetime(2025, 4, 30, 23, 59)),
                ("22.000000", datetime(2025, 5, 1, 1)),
                ("25.000000", datetime(2025, 5, 14, 23)),
                ("26.500000", datetime(2025, 5, 15, 11)),
            )
            for value, measured_at in readings:
                MesureCapteur.objects.create(
                    capteur=sensor,
                    valeur=value,
                    unite="kWh",
                    date_mesure=timezone.make_aware(measured_at),
                )

            with patch("energy_assets.monitoring._maintenant", return_value=now):
                response = self.client.get(
                    f"/api/energy-assets/sites/{self.site.id}/monitoring/summary/"
                )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["today_energy_kwh"], "1.500000")
        self.assertEqual(response.data["yesterday_energy_kwh"], "3.000000")
        self.assertEqual(response.data["month_energy_kwh"], "6.500000")
        self.assertEqual(
            response.data["energy_coverage"],
            {
                "today": {"complete_sensors": 1, "total_sensors": 1},
                "yesterday": {"complete_sensors": 1, "total_sensors": 1},
                "month": {"complete_sensors": 1, "total_sensors": 1},
            },
        )

    def test_site_monitoring_summary_marks_energy_unavailable_without_baseline(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Compteur général",
            categorie="COMPTEUR",
            puissance_nominale_kw="5.000",
        )
        Capteur.objects.create(
            equipement=equipment,
            identifiant="ENERGY-SITE-EMPTY-001",
            type="ENERGY",
        )

        response = self.client.get(
            f"/api/energy-assets/sites/{self.site.id}/monitoring/summary/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["today_energy_kwh"])
        self.assertEqual(
            response.data["energy_coverage"]["today"],
            {"complete_sensors": 0, "total_sensors": 1},
        )

    def test_create_equipment_initializes_state_and_returns_its_sensor(self):
        zone_response = self.client.post(
            "/api/energy-assets/zones/",
            {"site": str(self.site.id), "nom": "Bureau"},
            format="json",
        )
        self.assertEqual(zone_response.status_code, status.HTTP_201_CREATED, zone_response.data)

        equipment_response = self.client.post(
            "/api/energy-assets/equipements/",
            {
                "site": str(self.site.id),
                "zone": zone_response.data["id"],
                "nom": "Climatiseur salle 1",
                "categorie": "CLIMATISATION",
                "puissance_nominale_kw": "1.500",
            },
            format="json",
        )
        self.assertEqual(
            equipment_response.status_code,
            status.HTTP_201_CREATED,
            equipment_response.data,
        )
        equipment_id = equipment_response.data["id"]
        self.assertEqual(equipment_response.data["etat"]["etat_rapporte"], "UNKNOWN")
        self.assertEqual(equipment_response.data["capteurs"], [])

        sensor_response = self.client.post(
            "/api/energy-assets/capteurs/",
            {
                "equipement": equipment_id,
                "identifiant": "SIM-CLIM-001",
                "type": "ELECTRICITY",
            },
            format="json",
        )
        self.assertEqual(sensor_response.status_code, status.HTTP_201_CREATED, sensor_response.data)

        equipment_detail = self.client.get(f"/api/energy-assets/equipements/{equipment_id}/")
        self.assertEqual(equipment_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(
            equipment_detail.data["capteurs"][0]["identifiant"],
            "SIM-CLIM-001",
        )

    def test_create_zone_rejects_site_from_another_organization(self):
        other_organisation = self.create_organisation("Organisation étrangère")
        other_site = self.create_site(other_organisation, "Site étranger")

        response = self.client.post(
            "/api/energy-assets/zones/",
            {"site": str(other_site.id), "nom": "Zone non autorisée"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("site", response.data)

    def test_equipment_list_only_includes_sites_from_accessible_organizations(self):
        allowed_equipment = Equipement.objects.create(
            site=self.site,
            nom="Équipement autorisé",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        other_organisation = self.create_organisation("Autre organisation")
        other_site = self.create_site(other_organisation, "Site étranger")
        Equipement.objects.create(
            site=other_site,
            nom="Équipement étranger",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )

        response = self.client.get("/api/energy-assets/equipements/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [str(allowed_equipment.id)],
        )

    def test_site_monitoring_summary_aggregates_only_monitored_equipment(self):
        active_equipment = Equipement.objects.create(
            site=self.site,
            nom="Ventilation",
            categorie="VENTILATION",
            puissance_nominale_kw="2.000",
        )
        active_state = EtatEquipement.objects.create(
            equipement=active_equipment,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="1.250",
            date_etat_rapporte=timezone.now(),
        )
        Capteur.objects.create(
            equipement=active_equipment,
            identifiant="LIVE-VENT-001",
            type="POWER",
            derniere_communication=timezone.now(),
        )

        offline_equipment = Equipement.objects.create(
            site=self.site,
            nom="Pompe",
            categorie="POMPE",
            puissance_nominale_kw="1.000",
        )
        EtatEquipement.objects.create(
            equipement=offline_equipment,
            etat_rapporte=Equipement.Etat.OFF,
            puissance_actuelle_kw="0.000",
        )
        Capteur.objects.create(
            equipement=offline_equipment,
            identifiant="STALE-POMPE-001",
            type="POWER",
            derniere_communication=timezone.now() - timedelta(minutes=6),
        )

        unmonitored_equipment = Equipement.objects.create(
            site=self.site,
            nom="Équipement non suivi",
            categorie="AUTRE",
            puissance_nominale_kw="3.000",
            monitoring_active=False,
        )
        EtatEquipement.objects.create(
            equipement=unmonitored_equipment,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="3.000",
        )

        response = self.client.get(
            f"/api/energy-assets/sites/{self.site.id}/monitoring/summary/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["site_id"], str(self.site.id))
        self.assertEqual(response.data["current_power_kw"], "1.250")
        self.assertEqual(response.data["monitored_equipment_count"], 2)
        self.assertEqual(response.data["active_equipment_count"], 1)
        self.assertEqual(response.data["offline_equipment_count"], 1)
        self.assertEqual(
            response.data["top_consumer"]["equipment_id"],
            str(active_equipment.id),
        )
        self.assertEqual(response.data["latest_measurement_at"], active_state.date_etat_rapporte.isoformat().replace("+00:00", "Z"))

    def test_site_current_load_returns_per_equipment_state_and_power(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Climatiseur",
            categorie="CLIMATISATION",
            puissance_nominale_kw="1.500",
        )
        EtatEquipement.objects.create(
            equipement=equipment,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="0.850",
        )
        Capteur.objects.create(
            equipement=equipment,
            identifiant="CLIM-LIVE-001",
            type="POWER",
            derniere_communication=timezone.now(),
        )

        response = self.client.get(
            f"/api/energy-assets/sites/{self.site.id}/monitoring/current-load/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["current_power_kw"], "0.850")
        self.assertEqual(len(response.data["equipment"]), 1)
        self.assertEqual(response.data["equipment"][0]["name"], "Climatiseur")
        self.assertEqual(response.data["equipment"][0]["power_kw"], "0.850")
        self.assertTrue(response.data["equipment"][0]["is_online"])

    def test_site_top_consumers_are_sorted_and_include_power_share(self):
        expected_consumers = []
        for name, power, identifier in (
            ("Pompe", "0.500", "TOP-POMPE-001"),
            ("Ventilation", "1.500", "TOP-VENT-001"),
        ):
            equipment = Equipement.objects.create(
                site=self.site,
                nom=name,
                categorie="AUTRE",
                puissance_nominale_kw="2.000",
            )
            EtatEquipement.objects.create(
                equipement=equipment,
                etat_rapporte=Equipement.Etat.ON,
                puissance_actuelle_kw=power,
            )
            Capteur.objects.create(
                equipement=equipment,
                identifiant=identifier,
                type="POWER",
                derniere_communication=timezone.now(),
            )
            expected_consumers.append((equipment, power))

        response = self.client.get(
            f"/api/energy-assets/sites/{self.site.id}/monitoring/top-consumers/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [item["name"] for item in response.data],
            ["Ventilation", "Pompe"],
        )
        self.assertEqual(response.data[0]["power_kw"], 1.5)
        self.assertEqual(response.data[0]["share_percent"], 75.0)

    def test_monitoring_endpoints_hide_sites_from_other_organizations(self):
        other_organisation = self.create_organisation("Organisation étrangère")
        other_site = self.create_site(other_organisation, "Site étranger")

        for suffix in ("summary/", "current-load/", "top-consumers/"):
            response = self.client.get(
                f"/api/energy-assets/sites/{other_site.id}/monitoring/{suffix}"
            )
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_equipment_telemetry_history_is_scoped_and_newest_first(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Compteur principal",
            categorie="COMPTEUR",
            puissance_nominale_kw="1.000",
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="HISTORY-001",
            type="TEMPERATURE",
            mode=Capteur.Mode.SIMULATED,
        )
        latest_at = timezone.now()
        latest = MesureCapteur.objects.create(
            capteur=sensor,
            valeur="22.500000",
            unite="°C",
            date_mesure=latest_at,
        )
        MesureCapteur.objects.create(
            capteur=sensor,
            valeur="21.000000",
            unite="°C",
            date_mesure=latest_at - timedelta(minutes=1),
        )

        other_organisation = self.create_organisation("Organisation étrangère")
        other_site = self.create_site(other_organisation, "Site étranger")
        other_equipment = Equipement.objects.create(
            site=other_site,
            nom="Autre compteur",
            categorie="COMPTEUR",
            puissance_nominale_kw="1.000",
        )
        other_sensor = Capteur.objects.create(
            equipement=other_equipment,
            identifiant="HISTORY-OTHER-001",
            type="TEMPERATURE",
        )
        MesureCapteur.objects.create(
            capteur=other_sensor,
            valeur="30",
            unite="°C",
            date_mesure=latest_at,
        )

        response = self.client.get(
            f"/api/energy-assets/equipements/{equipment.id}/telemetry/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            response.data["results"][0]["telemetry_id"],
            str(latest.id),
        )
        self.assertEqual(response.data["results"][0]["sensor_identifier"], "HISTORY-001")
        self.assertEqual(response.data["results"][0]["source"], Capteur.Mode.SIMULATED)

        forbidden_response = self.client.get(
            f"/api/energy-assets/equipements/{other_equipment.id}/telemetry/"
        )
        self.assertEqual(
            forbidden_response.status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_create_sensor_measurement_updates_communication_timestamp(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Compteur principal",
            categorie="COMPTEUR",
            puissance_nominale_kw="1.000",
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="REAL-COMPTEUR-001",
            type="POWER",
        )
        measured_at = timezone.now()

        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(sensor.id),
                "valeur": "0.450000",
                "unite": "kW",
                "date_mesure": measured_at.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["valeur"], "0.450000")
        self.assertEqual(response.data["unite"], "kW")
        self.assertIsNotNone(response.data["date_reception"])
        sensor.refresh_from_db()
        self.assertIsNotNone(sensor.derniere_communication)

    def test_power_measurement_converts_watts_and_synchronizes_reported_state(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Ventilation",
            categorie="VENTILATION",
            puissance_nominale_kw="2.000",
        )
        state = EtatEquipement.objects.create(
            equipement=equipment,
            etat_souhaite=Equipement.Etat.ON,
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="POWER-VENT-001",
            type="POWER",
        )

        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(sensor.id),
                "valeur": "450.000000",
                "unite": "W",
                "date_mesure": timezone.now().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        state.refresh_from_db()
        self.assertEqual(str(state.puissance_actuelle_kw), "0.450")
        self.assertEqual(state.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(
            state.statut_synchronisation,
            EtatEquipement.StatutSynchronisation.SYNCHRONIZED,
        )

    def test_zero_power_marks_equipment_off_and_out_of_sync(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Pompe",
            categorie="POMPE",
            puissance_nominale_kw="1.000",
        )
        state = EtatEquipement.objects.create(
            equipement=equipment,
            etat_souhaite=Equipement.Etat.ON,
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="POWER-POMPE-001",
            type="POWER",
        )

        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(sensor.id),
                "valeur": "0",
                "unite": "kW",
                "date_mesure": timezone.now().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        state.refresh_from_db()
        self.assertEqual(state.puissance_actuelle_kw, 0)
        self.assertEqual(state.etat_rapporte, Equipement.Etat.OFF)
        self.assertEqual(
            state.statut_synchronisation,
            EtatEquipement.StatutSynchronisation.OUT_OF_SYNC,
        )

    def test_energy_measurement_converts_watt_hours_without_changing_power_state(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Compteur",
            categorie="COMPTEUR",
            puissance_nominale_kw="1.000",
        )
        state = EtatEquipement.objects.create(
            equipement=equipment,
            etat_souhaite=Equipement.Etat.ON,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="0.300",
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="ENERGY-COMPTEUR-001",
            type="ENERGY",
        )

        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(sensor.id),
                "valeur": "2500",
                "unite": "Wh",
                "date_mesure": timezone.now().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        state.refresh_from_db()
        self.assertEqual(str(state.energie_cumulee_kwh), "2.500000")
        self.assertEqual(str(state.puissance_actuelle_kw), "0.300")
        self.assertEqual(state.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(
            state.statut_synchronisation,
            EtatEquipement.StatutSynchronisation.UNKNOWN,
        )

    def test_older_power_measurement_does_not_replace_current_state(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Moteur",
            categorie="MOTEUR",
            puissance_nominale_kw="2.000",
        )
        state = EtatEquipement.objects.create(equipement=equipment)
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="POWER-MOTEUR-001",
            type="POWER",
        )
        measured_at = timezone.now()
        for value, timestamp in (
            ("0.700", measured_at),
            ("0", measured_at - timedelta(minutes=1)),
        ):
            response = self.client.post(
                "/api/energy-assets/mesures/",
                {
                    "capteur": str(sensor.id),
                    "valeur": value,
                    "unite": "kW",
                    "date_mesure": timestamp.isoformat(),
                },
                format="json",
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_201_CREATED,
                response.data,
            )
            state.refresh_from_db()
            if value == "0.700":
                self.assertEqual(str(state.puissance_actuelle_kw), "0.700")

        state.refresh_from_db()
        self.assertEqual(str(state.puissance_actuelle_kw), "0.700")
        self.assertEqual(state.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(state.date_etat_rapporte, measured_at)

    def test_power_and_energy_measurements_keep_independent_latest_values(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Centrale technique",
            categorie="AUTRE",
            puissance_nominale_kw="3.000",
        )
        state = EtatEquipement.objects.create(
            equipement=equipment,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="0.300",
            energie_cumulee_kwh="1.000000",
        )
        power_sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="POWER-CENTRALE-001",
            type="POWER",
        )
        energy_sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="ENERGY-CENTRALE-001",
            type="ENERGY",
        )
        power_at = timezone.now() - timedelta(minutes=2)
        MesureCapteur.objects.create(
            capteur=power_sensor,
            valeur="0.300",
            unite="kW",
            date_mesure=power_at,
        )
        state.date_etat_rapporte = power_at
        state.save(update_fields=("date_etat_rapporte",))

        energy_at = timezone.now()
        for value, timestamp in (
            ("2.000000", energy_at),
            ("1.500000", energy_at - timedelta(minutes=1)),
        ):
            response = self.client.post(
                "/api/energy-assets/mesures/",
                {
                    "capteur": str(energy_sensor.id),
                    "valeur": value,
                    "unite": "kWh",
                    "date_mesure": timestamp.isoformat(),
                },
                format="json",
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_201_CREATED,
                response.data,
            )

        newer_power_at = energy_at - timedelta(seconds=30)
        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(power_sensor.id),
                "valeur": "0.750",
                "unite": "kW",
                "date_mesure": newer_power_at.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        state.refresh_from_db()
        self.assertEqual(str(state.puissance_actuelle_kw), "0.750")
        self.assertEqual(str(state.energie_cumulee_kwh), "2.000000")
        self.assertEqual(state.date_etat_rapporte, newer_power_at)

    def test_power_measurement_rejects_unknown_unit_and_negative_value(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Climatiseur",
            categorie="CLIMATISATION",
            puissance_nominale_kw="1.000",
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="POWER-CLIM-001",
            type="POWER",
        )

        for value, unit in (("1", "V"), ("-1", "kW")):
            response = self.client.post(
                "/api/energy-assets/mesures/",
                {
                    "capteur": str(sensor.id),
                    "valeur": value,
                    "unite": unit,
                    "date_mesure": timezone.now().isoformat(),
                },
                format="json",
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                response.data,
            )

        self.assertEqual(MesureCapteur.objects.count(), 0)

    def test_other_sensor_types_are_stored_without_changing_equipment_state(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Local technique",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        state = EtatEquipement.objects.create(
            equipement=equipment,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="0.250",
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="TEMP-LOCAL-001",
            type="TEMPERATURE",
        )
        reported_at = state.date_etat_rapporte

        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(sensor.id),
                "valeur": "24.5",
                "unite": "°C",
                "date_mesure": timezone.now().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        state.refresh_from_db()
        self.assertEqual(state.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(str(state.puissance_actuelle_kw), "0.250")
        self.assertEqual(state.date_etat_rapporte, reported_at)

    def test_sensor_measurements_are_scoped_to_accessible_organizations(self):
        allowed_equipment = Equipement.objects.create(
            site=self.site,
            nom="Équipement autorisé",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        allowed_sensor = Capteur.objects.create(
            equipement=allowed_equipment,
            identifiant="SIM-AUTORISE-001",
            type="ENERGY",
        )
        allowed_measurement = MesureCapteur.objects.create(
            capteur=allowed_sensor,
            valeur="1.000000",
            unite="kWh",
            date_mesure=timezone.now(),
        )

        other_organisation = self.create_organisation("Organisation étrangère")
        other_site = self.create_site(other_organisation, "Site étranger")
        other_equipment = Equipement.objects.create(
            site=other_site,
            nom="Équipement étranger",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        other_sensor = Capteur.objects.create(
            equipement=other_equipment,
            identifiant="SIM-ETRANGER-001",
            type="ENERGY",
        )
        MesureCapteur.objects.create(
            capteur=other_sensor,
            valeur="2.000000",
            unite="kWh",
            date_mesure=timezone.now(),
        )

        response = self.client.get("/api/energy-assets/mesures/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [str(allowed_measurement.id)],
        )

    def test_create_measurement_rejects_sensor_from_another_organization(self):
        other_organisation = self.create_organisation("Organisation étrangère")
        other_site = self.create_site(other_organisation, "Site étranger")
        other_equipment = Equipement.objects.create(
            site=other_site,
            nom="Équipement étranger",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        other_sensor = Capteur.objects.create(
            equipement=other_equipment,
            identifiant="REAL-ETRANGER-001",
            type="POWER",
        )

        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(other_sensor.id),
                "valeur": "1.000000",
                "unite": "kW",
                "date_mesure": timezone.now().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("capteur", response.data)

    def test_create_measurement_rejects_inactive_sensor(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Capteur hors service",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="INACTIVE-001",
            type="POWER",
            statut=Capteur.Statut.INACTIVE,
        )

        response = self.client.post(
            "/api/energy-assets/mesures/",
            {
                "capteur": str(sensor.id),
                "valeur": "1.000000",
                "unite": "kW",
                "date_mesure": timezone.now().isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("capteur", response.data)

    def test_equipment_command_is_created_pending_without_changing_reported_state(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Ventilation",
            categorie="VENTILATION",
            puissance_nominale_kw="2.000",
        )
        state = EtatEquipement.objects.create(equipement=equipment)

        response = self.client.post(
            "/api/energy-assets/commandes/",
            {
                "equipement": str(equipment.id),
                "action": CommandeEquipement.Action.ON,
                "statut": CommandeEquipement.Statut.SENT,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["statut"], CommandeEquipement.Statut.PENDING)
        self.assertIsNone(response.data["date_envoi"])
        self.assertEqual(response.data["demande_par"], self.user.id)
        state.refresh_from_db()
        self.assertEqual(state.etat_souhaite, Equipement.Etat.ON)
        self.assertEqual(state.etat_rapporte, Equipement.Etat.UNKNOWN)

    def test_command_list_and_creation_are_organization_scoped(self):
        allowed_equipment = Equipement.objects.create(
            site=self.site,
            nom="Équipement autorisé",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        allowed_command = demander_commande(
            allowed_equipment,
            CommandeEquipement.Action.ON,
            self.user,
        )

        other_organisation = self.create_organisation("Organisation étrangère")
        other_site = self.create_site(other_organisation, "Site étranger")
        other_equipment = Equipement.objects.create(
            site=other_site,
            nom="Équipement étranger",
            categorie="AUTRE",
            puissance_nominale_kw="0.500",
        )
        response = self.client.post(
            "/api/energy-assets/commandes/",
            {
                "equipement": str(other_equipment.id),
                "action": CommandeEquipement.Action.OFF,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("equipement", response.data)

        response = self.client.get("/api/energy-assets/commandes/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [str(allowed_command.id)],
        )

    def test_command_lifecycle_confirms_and_updates_reported_state(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Pompe",
            categorie="POMPE",
            puissance_nominale_kw="1.000",
        )
        state = EtatEquipement.objects.create(equipement=equipment)
        command = demander_commande(
            equipment,
            CommandeEquipement.Action.ON,
            self.user,
        )

        command = marquer_commande_envoyee(command)
        self.assertEqual(command.statut, CommandeEquipement.Statut.SENT)
        self.assertIsNotNone(command.date_envoi)

        command = confirmer_commande(command)

        self.assertEqual(command.statut, CommandeEquipement.Statut.CONFIRMED)
        self.assertIsNotNone(command.date_finalisation)
        state.refresh_from_db()
        self.assertEqual(state.etat_souhaite, Equipement.Etat.ON)
        self.assertEqual(state.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(
            state.statut_synchronisation,
            EtatEquipement.StatutSynchronisation.SYNCHRONIZED,
        )

    def test_command_failure_records_reason_and_keeps_reported_state_unchanged(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Compresseur",
            categorie="COMPRESSEUR",
            puissance_nominale_kw="1.000",
        )
        state = EtatEquipement.objects.create(equipement=equipment)
        command = demander_commande(
            equipment,
            CommandeEquipement.Action.ON,
            self.user,
        )
        marquer_commande_envoyee(command)

        command = echouer_commande(command, "Passerelle indisponible")

        self.assertEqual(command.statut, CommandeEquipement.Statut.FAILED)
        self.assertEqual(command.detail_echec, "Passerelle indisponible")
        self.assertIsNotNone(command.date_finalisation)
        state.refresh_from_db()
        self.assertEqual(state.etat_souhaite, Equipement.Etat.ON)
        self.assertEqual(state.etat_rapporte, Equipement.Etat.UNKNOWN)

    def test_invalid_command_transitions_are_rejected(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Chaudière",
            categorie="CHAUFFAGE",
            puissance_nominale_kw="1.000",
        )
        command = demander_commande(
            equipment,
            CommandeEquipement.Action.OFF,
            self.user,
        )

        with self.assertRaises(ValueError):
            confirmer_commande(command)
        with self.assertRaises(ValueError):
            echouer_commande(command, " ")

        command = marquer_commande_envoyee(command)
        command = confirmer_commande(command)
        with self.assertRaises(ValueError):
            echouer_commande(command, "Commande déjà confirmée")

    def test_virtual_action_estimates_savings_without_changing_equipment_or_queueing_command(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Climatiseur",
            categorie="CLIMATISATION",
            puissance_nominale_kw="2.000",
            puissance_veille_kw="0.100",
            quantite=2,
        )
        state = EtatEquipement.objects.create(
            equipement=equipment,
            etat_souhaite=Equipement.Etat.ON,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="2.500",
        )
        recommendation = self.create_recommendation()

        response = self.client.post(
            "/api/energy-assets/actions-virtuelles/",
            {
                "recommandation": str(recommendation.id),
                "equipement": str(equipment.id),
                "etat_cible": ActionVirtuelle.EtatCible.OFF,
                "duree_heures": "4.00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["puissance_reference_kw"], "2.500")
        self.assertEqual(response.data["puissance_scenario_kw"], "0.200")
        self.assertEqual(response.data["variation_energie_kwh"], "9.200000")
        self.assertEqual(
            response.data["source_puissance_reference"],
            ActionVirtuelle.SourcePuissance.MESURE,
        )
        state.refresh_from_db()
        self.assertEqual(state.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(state.etat_souhaite, Equipement.Etat.ON)
        self.assertEqual(state.puissance_actuelle_kw, Decimal("2.500"))
        self.assertFalse(CommandeEquipement.objects.exists())

    def test_virtual_action_requires_a_reported_equipment_state(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Pompe",
            categorie="POMPE",
            puissance_nominale_kw="1.000",
        )
        EtatEquipement.objects.create(equipement=equipment)
        recommendation = self.create_recommendation()

        response = self.client.post(
            "/api/energy-assets/actions-virtuelles/",
            {
                "recommandation": str(recommendation.id),
                "equipement": str(equipment.id),
                "etat_cible": ActionVirtuelle.EtatCible.OFF,
                "duree_heures": "1.00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("equipement", response.data)
        self.assertFalse(ActionVirtuelle.objects.exists())

    def test_virtual_action_rejects_recommendation_from_another_organization(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Ventilation",
            categorie="VENTILATION",
            puissance_nominale_kw="1.000",
        )
        EtatEquipement.objects.create(
            equipement=equipment,
            etat_rapporte=Equipement.Etat.ON,
        )
        other_organisation = self.create_organisation("Organisation étrangère")
        recommendation = self.create_recommendation(other_organisation)

        response = self.client.post(
            "/api/energy-assets/actions-virtuelles/",
            {
                "recommandation": str(recommendation.id),
                "equipement": str(equipment.id),
                "etat_cible": ActionVirtuelle.EtatCible.OFF,
                "duree_heures": "1.00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("recommandation", response.data)
        self.assertFalse(ActionVirtuelle.objects.exists())

    def test_virtual_action_duration_is_limited_to_24_hours(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Éclairage",
            categorie="ECLAIRAGE",
            puissance_nominale_kw="0.500",
        )
        EtatEquipement.objects.create(
            equipement=equipment,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="0.500",
        )
        recommendation = self.create_recommendation()

        response = self.client.post(
            "/api/energy-assets/actions-virtuelles/",
            {
                "recommandation": str(recommendation.id),
                "equipement": str(equipment.id),
                "etat_cible": ActionVirtuelle.EtatCible.OFF,
                "duree_heures": "24.01",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("duree_heures", response.data)
        self.assertFalse(ActionVirtuelle.objects.exists())

    def test_virtual_action_history_is_scoped_to_accessible_organizations(self):
        equipment = Equipement.objects.create(
            site=self.site,
            nom="Ventilation autorisée",
            categorie="VENTILATION",
            puissance_nominale_kw="1.000",
        )
        EtatEquipement.objects.create(
            equipement=equipment,
            etat_rapporte=Equipement.Etat.ON,
            puissance_actuelle_kw="1.000",
        )
        recommendation = self.create_recommendation()
        create_response = self.client.post(
            "/api/energy-assets/actions-virtuelles/",
            {
                "recommandation": str(recommendation.id),
                "equipement": str(equipment.id),
                "etat_cible": ActionVirtuelle.EtatCible.OFF,
                "duree_heures": "1.00",
            },
            format="json",
        )
        self.assertEqual(
            create_response.status_code,
            status.HTTP_201_CREATED,
            create_response.data,
        )

        other_organisation = self.create_organisation("Organisation étrangère")
        other_site = self.create_site(other_organisation, "Site étranger")
        other_equipment = Equipement.objects.create(
            site=other_site,
            nom="Ventilation étrangère",
            categorie="VENTILATION",
            puissance_nominale_kw="1.000",
        )
        other_recommendation = self.create_recommendation(other_organisation)
        foreign_action = ActionVirtuelle.objects.create(
            recommandation=other_recommendation,
            equipement=other_equipment,
            etat_cible=ActionVirtuelle.EtatCible.OFF,
            duree_heures="1.00",
            puissance_reference_kw="1.000",
            puissance_scenario_kw="0.000",
            variation_energie_kwh="1.000000",
            source_puissance_reference=ActionVirtuelle.SourcePuissance.NOMINALE,
        )

        response = self.client.get("/api/energy-assets/actions-virtuelles/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [create_response.data["id"]],
        )
        self.assertNotIn(
            str(foreign_action.id),
            [item["id"] for item in response.data["results"]],
        )


class EnergyAssetModelTests(TestCase):
    def setUp(self):
        self.organisation = Organisation.objects.create(
            nom="Organisation test",
            secteur="Commerce",
            localisation="Dakar",
        )
        self.site = Site.objects.create(
            organisation=self.organisation,
            nom="Site test",
            adresse="Dakar",
            pays="Sénégal",
            fuseau_horaire="Africa/Dakar",
        )

    def create_equipment(self, **kwargs):
        defaults = {
            "site": self.site,
            "nom": "Climatiseur",
            "categorie": "CLIMATISATION",
            "puissance_nominale_kw": "1.500",
        }
        defaults.update(kwargs)
        return Equipement.objects.create(**defaults)

    def test_equipment_can_be_linked_to_zone_sensor_profile_and_state(self):
        zone = Zone.objects.create(site=self.site, nom="Bureau")
        equipment = self.create_equipment(zone=zone)
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="SIM-CLIM-001",
            type="ELECTRICITY",
        )
        profile = ProfilFonctionnement.objects.create(
            equipement=equipment,
            nom="Horaires de bureau",
            jour_semaine=ProfilFonctionnement.JourSemaine.LUNDI,
            heure_debut="08:00",
            heure_fin="18:00",
        )
        state = EtatEquipement.objects.create(equipement=equipment)

        self.assertEqual(equipment.site, self.site)
        self.assertEqual(equipment.zone, zone)
        self.assertEqual(sensor.mode, Capteur.Mode.SIMULATED)
        self.assertEqual(sensor.frequence_secondes, 15)
        self.assertEqual(profile.equipement, equipment)
        self.assertEqual(state.etat_souhaite, Equipement.Etat.UNKNOWN)
        self.assertEqual(state.etat_rapporte, Equipement.Etat.UNKNOWN)

    def test_sensor_measurements_keep_signed_values_and_order_by_measurement_time(self):
        equipment = self.create_equipment()
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="REAL-CLIM-001",
            type="TEMPERATURE",
        )
        maintenant = timezone.now()
        mesure_recente = MesureCapteur.objects.create(
            capteur=sensor,
            valeur="21.500000",
            unite="°C",
            date_mesure=maintenant,
        )
        mesure_ancienne = MesureCapteur.objects.create(
            capteur=sensor,
            valeur="-2.500000",
            unite="°C",
            date_mesure=maintenant - timedelta(minutes=1),
        )

        mesures = list(sensor.mesures.all())

        self.assertEqual(
            [mesure.id for mesure in mesures],
            [mesure_recente.id, mesure_ancienne.id],
        )
        self.assertEqual(str(mesures[0].valeur), "21.500000")
        self.assertEqual(mesures[0].unite, "°C")
        self.assertIsNotNone(mesures[0].date_reception)
        self.assertEqual(str(mesures[1].valeur), "-2.500000")

    def test_simulated_power_sensor_generates_due_telemetry_and_updates_state(self):
        equipment = self.create_equipment()
        state = EtatEquipement.objects.create(equipement=equipment)
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="SIM-POWER-CLIM-001",
            type="POWER",
        )
        now = timezone.now()

        measurement = generer_mesure_simulee(sensor.id, maintenant=now)

        self.assertEqual(measurement.valeur, Decimal(equipment.puissance_nominale_kw))
        self.assertEqual(measurement.unite, "kW")
        self.assertEqual(measurement.date_mesure, now)
        self.assertIsNone(
            generer_mesure_simulee(
                sensor.id,
                maintenant=now + timedelta(seconds=14),
            )
        )
        prochaine_mesure = generer_mesure_simulee(
            sensor.id,
            maintenant=now + timedelta(seconds=15),
        )
        self.assertIsNotNone(prochaine_mesure)
        state.refresh_from_db()
        sensor.refresh_from_db()
        self.assertEqual(state.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(
            state.puissance_actuelle_kw,
            Decimal(equipment.puissance_nominale_kw),
        )
        self.assertEqual(sensor.derniere_communication, now + timedelta(seconds=15))

    def test_simulated_power_sensor_uses_standby_power_when_requested_off(self):
        equipment = self.create_equipment(puissance_veille_kw="0.125")
        EtatEquipement.objects.create(
            equipement=equipment,
            etat_souhaite=Equipement.Etat.OFF,
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="SIM-POWER-OFF-001",
            type="POWER",
        )

        measurement = generer_mesure_simulee(sensor.id)

        self.assertEqual(measurement.valeur, Decimal("0.125"))
        self.assertEqual(measurement.unite, "kW")

    def test_simulated_energy_sensor_integrates_power_since_previous_reading(self):
        equipment = self.create_equipment(puissance_nominale_kw="1.500")
        state = EtatEquipement.objects.create(
            equipement=equipment,
            etat_souhaite=Equipement.Etat.ON,
        )
        sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="SIM-ENERGY-CLIM-001",
            type="ENERGY",
        )
        start = timezone.now()
        MesureCapteur.objects.create(
            capteur=sensor,
            valeur="3.000000",
            unite="kWh",
            date_mesure=start,
        )

        measurement = generer_mesure_simulee(
            sensor.id,
            maintenant=start + timedelta(seconds=15),
        )

        self.assertEqual(measurement.valeur, Decimal("3.006250"))
        self.assertEqual(measurement.unite, "kWh")
        state.refresh_from_db()
        self.assertEqual(state.energie_cumulee_kwh, Decimal("3.006250"))

    def test_simulator_ignores_real_and_unsupported_sensors(self):
        equipment = self.create_equipment()
        real_sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="REAL-POWER-CLIM-001",
            type="POWER",
            mode=Capteur.Mode.REAL,
        )
        unsupported_sensor = Capteur.objects.create(
            equipement=equipment,
            identifiant="SIM-TEMP-CLIM-001",
            type="TEMPERATURE",
        )

        self.assertIsNone(generer_mesure_simulee(real_sensor.id))
        self.assertIsNone(generer_mesure_simulee(unsupported_sensor.id))
        self.assertEqual(MesureCapteur.objects.count(), 0)

    def test_simulator_management_command_can_run_one_cycle(self):
        equipment = self.create_equipment()
        Capteur.objects.create(
            equipement=equipment,
            identifiant="SIM-POWER-ONCE-001",
            type="POWER",
        )
        output = StringIO()

        call_command("simuler_capteurs", "--once", stdout=output)

        self.assertIn("Mesures simulées générées : 1", output.getvalue())
        self.assertEqual(MesureCapteur.objects.count(), 1)

    def test_simulator_rejects_non_positive_frequency_and_loop_interval(self):
        sensor = Capteur.objects.create(
            equipement=self.create_equipment(),
            identifiant="SIM-POWER-FREQ-001",
            type="POWER",
            frequence_secondes=0,
        )
        with self.assertRaisesMessage(ValueError, "fréquence positive"):
            generer_mesure_simulee(sensor.id)

        with self.assertRaisesMessage(CommandError, "intervalle"):
            call_command("simuler_capteurs", "--interval", "0")

    def test_equipment_rejects_negative_nominal_power(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.create_equipment(puissance_nominale_kw="-0.001")

    def test_profile_rejects_end_time_before_start_time(self):
        equipment = self.create_equipment()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProfilFonctionnement.objects.create(
                    equipement=equipment,
                    jour_semaine=ProfilFonctionnement.JourSemaine.LUNDI,
                    heure_debut="18:00",
                    heure_fin="08:00",
                )


@override_settings(DJANGO_INTERNAL_TOKEN="test-internal-service-token")
class InternalEquipmentCommandApiTests(APITestCase):
    token_headers = {"HTTP_X_INTERNAL_SERVICE_TOKEN": "test-internal-service-token"}

    def setUp(self):
        organisation = Organisation.objects.create(
            nom="Organisation passerelle",
            secteur="Industrie",
            localisation="Dakar",
            statut=Organisation.Statut.ACTIVE,
        )
        site = Site.objects.create(
            organisation=organisation,
            nom="Site passerelle",
            adresse="Dakar",
            pays="Sénégal",
            fuseau_horaire="Africa/Dakar",
        )
        self.equipement = Equipement.objects.create(
            site=site,
            nom="Pompe passerelle",
            categorie="POMPE",
            puissance_nominale_kw="1.000",
        )
        self.etat = EtatEquipement.objects.create(equipement=self.equipement)

    def create_command(self, action=CommandeEquipement.Action.ON):
        return CommandeEquipement.objects.create(
            equipement=self.equipement,
            action=action,
        )

    def test_internal_command_endpoints_require_the_shared_service_token(self):
        for headers in (
            {},
            {"HTTP_X_INTERNAL_SERVICE_TOKEN": "incorrect-token"},
        ):
            response = self.client.post(
                "/api/internal/energy-assets/commandes/suivante/",
                {},
                format="json",
                **headers,
            )
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_next_command_returns_oldest_pending_and_marks_it_sent(self):
        first = self.create_command()
        second = self.create_command(CommandeEquipement.Action.OFF)
        CommandeEquipement.objects.filter(pk=first.pk).update(
            date_creation=timezone.now() - timedelta(minutes=1)
        )

        response = self.client.post(
            "/api/internal/energy-assets/commandes/suivante/",
            {},
            format="json",
            **self.token_headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["id"], str(first.id))
        self.assertEqual(response.data["equipement_id"], str(self.equipement.id))
        self.assertEqual(response.data["statut"], CommandeEquipement.Statut.SENT)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.statut, CommandeEquipement.Statut.SENT)
        self.assertIsNotNone(first.date_envoi)
        self.assertEqual(second.statut, CommandeEquipement.Statut.PENDING)

        second_response = self.client.post(
            "/api/internal/energy-assets/commandes/suivante/",
            {},
            format="json",
            **self.token_headers,
        )
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.data["id"], str(second.id))

        empty_response = self.client.post(
            "/api/internal/energy-assets/commandes/suivante/",
            {},
            format="json",
            **self.token_headers,
        )
        self.assertEqual(empty_response.status_code, status.HTTP_204_NO_CONTENT)

    def test_internal_confirmation_updates_reported_state(self):
        self.etat.etat_souhaite = Equipement.Etat.ON
        self.etat.save(update_fields=("etat_souhaite",))
        command = self.create_command()
        marquer_commande_envoyee(command)

        response = self.client.post(
            f"/api/internal/energy-assets/commandes/{command.id}/confirmer/",
            {},
            format="json",
            **self.token_headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["statut"], CommandeEquipement.Statut.CONFIRMED)
        self.etat.refresh_from_db()
        self.assertEqual(self.etat.etat_rapporte, Equipement.Etat.ON)
        self.assertEqual(
            self.etat.statut_synchronisation,
            EtatEquipement.StatutSynchronisation.SYNCHRONIZED,
        )

    def test_internal_failure_requires_a_reason_and_keeps_reported_state(self):
        command = self.create_command()
        marquer_commande_envoyee(command)
        url = f"/api/internal/energy-assets/commandes/{command.id}/echouer/"

        invalid_response = self.client.post(
            url,
            {"detail": "  "},
            format="json",
            **self.token_headers,
        )
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            url,
            {"detail": " Passerelle indisponible "},
            format="json",
            **self.token_headers,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["statut"], CommandeEquipement.Statut.FAILED)
        command.refresh_from_db()
        self.assertEqual(command.detail_echec, "Passerelle indisponible")
        self.etat.refresh_from_db()
        self.assertEqual(self.etat.etat_rapporte, Equipement.Etat.UNKNOWN)

    def test_internal_api_rejects_invalid_transitions(self):
        command = self.create_command()

        response = self.client.post(
            f"/api/internal/energy-assets/commandes/{command.id}/confirmer/",
            {},
            format="json",
            **self.token_headers,
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
