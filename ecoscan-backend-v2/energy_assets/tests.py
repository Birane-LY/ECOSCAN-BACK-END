from django.db import IntegrityError, transaction
from django.test import TestCase

from organizations.models import Organisation, Site

from .models import Capteur, Equipement, EtatEquipement, ProfilFonctionnement, Zone


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
