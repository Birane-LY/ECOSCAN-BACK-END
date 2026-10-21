from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from analysis.models import Anomalie, Recommandation, ResultatMetrique
from organizations.models import Organisation, Site

from .models import (
    Capteur,
    CapteurPresence,
    Equipement,
    MesureCapteur,
    MesurePresence,
    Zone,
)
from .occupancy_anomalies import analyser_occupation_site
from .simulation import generer_mesure_presence_simulee


class OccupancyAnomalyTests(TestCase):
    def setUp(self):
        self.organisation = Organisation.objects.create(
            nom="Organisation occupation test",
            secteur="Commerce",
            localisation="Dakar",
            statut=Organisation.Statut.ACTIVE,
        )
        self.site = Site.objects.create(
            organisation=self.organisation,
            nom="Site occupation test",
            adresse="Dakar",
            pays="Sénégal",
            fuseau_horaire="Africa/Dakar",
        )
        self.zone = Zone.objects.create(site=self.site, nom="Bureau")
        self.equipement = Equipement.objects.create(
            site=self.site,
            zone=self.zone,
            nom="Climatiseur",
            categorie="CLIMATISATION",
            puissance_nominale_kw="1.000",
            puissance_veille_kw="0.100",
            quantite=1,
        )
        self.power_sensor = Capteur.objects.create(
            equipement=self.equipement,
            identifiant="POWER-OCCUPANCY-TEST",
            type="POWER",
            frequence_secondes=300,
        )
        self.presence_sensor = CapteurPresence.objects.create(
            zone=self.zone,
            identifiant="PRESENCE-OCCUPANCY-TEST",
            frequence_secondes=300,
        )
        self.day = timezone.localdate() - timedelta(days=1)
        self.start = timezone.make_aware(datetime.combine(self.day, datetime.min.time()))

    def add_confirmed_empty_intervals(self):
        for index in range(-1, 13):
            MesurePresence.objects.create(
                capteur=self.presence_sensor,
                occupee=False,
                date_mesure=self.start + timedelta(minutes=5 * index),
            )

    def add_power_readings(self, power_kw):
        for index in range(5):
            MesureCapteur.objects.create(
                capteur=self.power_sensor,
                valeur=Decimal(power_kw),
                unite="kW",
                date_mesure=self.start + timedelta(minutes=15 * index),
            )

    def test_anomaly_and_recommendation_are_created_idempotently_from_empty_zone(self):
        self.add_confirmed_empty_intervals()
        self.add_power_readings("0.300")

        first = analyser_occupation_site(self.site, jour=self.day)
        second = analyser_occupation_site(self.site, jour=self.day)

        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["zones_analyzed"], 1)
        self.assertEqual(first["anomalies_detected"], 1)
        self.assertEqual(second["anomalies_detected"], 1)
        self.assertEqual(Anomalie.objects.count(), 1)
        self.assertEqual(Recommandation.objects.count(), 1)
        self.assertEqual(
            Anomalie.objects.get().type,
            "ENERGY_USE_IN_UNOCCUPIED_ZONE",
        )
        self.assertEqual(
            Anomalie.objects.get().resultat_metrique.statut_qualite,
            ResultatMetrique.StatutQualite.ESTIME,
        )
        self.assertEqual(
            Recommandation.objects.get().statut,
            Recommandation.Statut.PROPOSEE,
        )
        self.assertEqual(
            Recommandation.objects.get().objectif.unite,
            "kWh",
        )

    def test_occupancy_anomaly_schedules_automatic_diagnostic(self):
        self.add_confirmed_empty_intervals()
        self.add_power_readings("0.300")

        with patch("analysis.services.taches.lancer_en_arriere_plan") as schedule:
            with self.captureOnCommitCallbacks(execute=True):
                result = analyser_occupation_site(self.site, jour=self.day)

        anomalie = Anomalie.objects.get()
        self.assertEqual(result["anomalies_detected"], 1)
        schedule.assert_called_once()
        self.assertEqual(
            schedule.call_args.args[0].__name__,
            "traiter_suivi_anomalie",
        )
        self.assertEqual(schedule.call_args.args[1], str(anomalie.id))

    def test_normal_measurement_resolves_existing_anomaly(self):
        self.add_confirmed_empty_intervals()
        self.add_power_readings("0.300")
        analyser_occupation_site(self.site, jour=self.day)
        MesureCapteur.objects.filter(capteur=self.power_sensor).delete()
        self.add_power_readings("0.100")

        result = analyser_occupation_site(self.site, jour=self.day)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(Anomalie.objects.get().statut, Anomalie.Statut.RESOLVED)

    def test_stale_presence_produces_no_metric_or_anomaly(self):
        MesurePresence.objects.create(
            capteur=self.presence_sensor,
            occupee=False,
            date_mesure=self.start - timedelta(minutes=30),
        )
        self.add_power_readings("0.300")

        result = analyser_occupation_site(self.site, jour=self.day)

        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(ResultatMetrique.objects.count(), 0)
        self.assertEqual(Anomalie.objects.count(), 0)

    def test_presence_simulator_emits_configured_state_only_on_schedule(self):
        self.presence_sensor.occupation_simulee = True
        self.presence_sensor.save(update_fields=("occupation_simulee",))
        measured_at = timezone.now()

        measure = generer_mesure_presence_simulee(
            self.presence_sensor.id,
            maintenant=measured_at,
        )
        repeated = generer_mesure_presence_simulee(
            self.presence_sensor.id,
            maintenant=measured_at + timedelta(seconds=10),
        )

        self.assertTrue(measure.occupee)
        self.assertIsNone(repeated)
        self.assertEqual(MesurePresence.objects.count(), 1)

    def test_real_presence_sensor_is_never_simulated(self):
        self.presence_sensor.mode = CapteurPresence.Mode.REAL
        self.presence_sensor.save(update_fields=("mode",))

        measure = generer_mesure_presence_simulee(self.presence_sensor.id)

        self.assertIsNone(measure)
        self.assertEqual(MesurePresence.objects.count(), 0)

    def test_unconfigured_presence_simulation_does_not_assume_empty_zone(self):
        measure = generer_mesure_presence_simulee(self.presence_sensor.id)

        self.assertIsNone(measure)
        self.assertEqual(MesurePresence.objects.count(), 0)
