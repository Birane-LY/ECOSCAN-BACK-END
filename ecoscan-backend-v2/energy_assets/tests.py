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
