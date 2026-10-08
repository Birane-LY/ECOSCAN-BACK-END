from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from billing.models import Abonnement, Plan
from organizations.models import Organisation, Site, UtilisateurOrganisation

from .models import (
    Capteur,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    ProfilFonctionnement,
    Zone,
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
