from io import StringIO
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import call, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Utilisateur
from analysis.models import Action, Recommandation
from analysis.serializers import ActionSerializer
from analysis.services.progression_objectifs import (
    _convertir,
    calculer_progression_mesuree,
)
from analysis.services.telemetry_impact import calculer_impact_telemetrie_action
from energy.models import Objectif
from energy_assets.models import Capteur, Equipement, MesureCapteur
from organizations.models import Organisation, Site, UtilisateurOrganisation


class TelemetryActionImpactTests(TestCase):
    def setUp(self):
        self.organisation = Organisation.objects.create(
            nom="EcoScan impact",
            secteur="Commerce",
            localisation="Dakar",
        )
        self.site = Site.objects.create(
            organisation=self.organisation,
            nom="Site impact",
            adresse="Dakar",
            pays="Sénégal",
            fuseau_horaire="Africa/Dakar",
        )
        self.equipement = Equipement.objects.create(
            site=self.site,
            nom="Pompe",
            categorie="Pompe",
            puissance_nominale_kw=Decimal("2.000"),
        )
        self.capteur = Capteur.objects.create(
            equipement=self.equipement,
            identifiant="ENERGY-IMPACT-001",
            type="ENERGY",
            mode=Capteur.Mode.REAL,
        )
        self.date_realisation = timezone.make_aware(
            datetime(2026, 1, 15, 12, 0),
            timezone=timezone.get_fixed_timezone(0),
        )
        self.objectif = Objectif.objects.create(
            organisation=self.organisation,
            nom="Réduire la consommation",
            type="REDUCTION_CONSOMMATION",
            valeur_cible=Decimal("100.000000"),
            unite="kWh",
            date_debut=timezone.make_aware(datetime(2026, 1, 1)),
            date_fin=timezone.make_aware(datetime(2026, 12, 31)),
            statut=Objectif.Statut.ACTIF,
        )
        self.recommandation = Recommandation.objects.create(
            objectif=self.objectif,
            titre="Réduire la durée de fonctionnement",
            description="Adapter la durée de fonctionnement de la pompe.",
            impact_estime=Decimal("14.000000"),
            economie_estimee=Decimal("14.00"),
            unite="kWh",
        )
        self.action = Action.objects.create(
            recommandation=self.recommandation,
            titre="Réglage de la pompe",
            statut=Action.Statut.TERMINEE,
            equipement=self.equipement,
            date_realisation=self.date_realisation,
        )
        self.user = Utilisateur.objects.create_user(
            email="impact@example.com",
            nom="Utilisateur impact",
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=True,
        )
        UtilisateurOrganisation.objects.create(
            organisation=self.organisation,
            utilisateur=self.user,
        )

    def _ajouter_semaine(self, debut, consommation_journaliere, jours=7, index_initial=None):
        index = Decimal("0") if index_initial is None else index_initial
        for decalage in range(jours):
            jour = debut + timedelta(days=decalage)
            with timezone.override(self.site.fuseau_horaire):
                releves = [
                    index,
                    index + consommation_journaliere / 3,
                    index + consommation_journaliere * 2 / 3,
                    index + consommation_journaliere,
                ]
                for heure, valeur in zip((1, 7, 13, 23), releves):
                    date_mesure = timezone.make_aware(datetime.combine(jour, time(hour=heure)))
                    MesureCapteur.objects.create(
                        capteur=self.capteur,
                        valeur=valeur,
                        unite="kWh",
                        date_mesure=date_mesure,
                    )
            index += consommation_journaliere
        return index

    def _ajouter_periodes_completes(self, jours_apres=7, consommation_apres=Decimal("8")):
        debut_reference = self.date_realisation.date() - timedelta(days=7)
        index = self._ajouter_semaine(debut_reference, Decimal("10"))
        debut_apres = self.date_realisation.date() + timedelta(days=1)
        self._ajouter_semaine(
            debut_apres,
            consommation_apres,
            jours=jours_apres,
            index_initial=index,
        )

    def test_measures_real_energy_savings_from_complete_weekly_periods(self):
        self._ajouter_periodes_completes()

        result = calculer_impact_telemetrie_action(self.action)

        self.assertEqual(result["statut"], "mesure")
        self.assertEqual(result["reference_kwh"], Decimal("70"))
        self.assertEqual(result["consommation_kwh"], Decimal("56"))
        self.assertEqual(result["economie_kwh"], Decimal("14"))
        self.action.refresh_from_db()
        self.assertIsNone(self.action.economie_energie_kwh)

    def test_objective_progress_uses_measured_action_savings_without_overwriting_declared(self):
        self._ajouter_periodes_completes()

        result = calculer_progression_mesuree(self.objectif)

        self.assertEqual(result["source"], "capteurs_energy_actions")
        self.assertEqual(result["mesuree"], 14)
        self.assertEqual(result["declaree"], 0)
        self.objectif.refresh_from_db()
        self.assertEqual(self.objectif.progression_actuelle, Decimal("0"))

    def test_incomplete_daily_readings_do_not_create_an_impact(self):
        self._ajouter_periodes_completes(jours_apres=6)

        result = calculer_impact_telemetrie_action(self.action)

        self.assertEqual(result["statut"], "donnees_insuffisantes")
        self.action.refresh_from_db()
        self.assertIsNone(self.action.economie_energie_kwh)

    def test_simulated_sensor_is_not_accepted_as_real_impact(self):
        self.capteur.mode = Capteur.Mode.SIMULATED
        self.capteur.save(update_fields=("mode",))

        result = calculer_impact_telemetrie_action(self.action)

        self.assertEqual(result["statut"], "indisponible")
        self.assertIn("réel actif", result["raison"])

    def test_objective_progress_exposes_simulated_telemetry_as_scenario(self):
        self.capteur.mode = Capteur.Mode.SIMULATED
        self.capteur.save(update_fields=("mode",))
        self._ajouter_periodes_completes()

        result = calculer_progression_mesuree(self.objectif)

        self.assertEqual(result["mesuree"], 14)
        self.assertEqual(result["source"], "capteurs_energy_actions_simulated")
        self.assertEqual(result["fiabilite"], "simulation")
        self.assertIn("pas une économie réelle", result["detail"])
        self.action.refresh_from_db()
        self.assertIsNone(self.action.economie_energie_kwh)

    def test_financial_progress_requires_an_explicit_applicable_tariff(self):
        value, reason = _convertir(
            Decimal("10"),
            Decimal("100"),
            "FCFA",
        )

        self.assertIsNone(value)
        self.assertIn("tarif vérifié", reason)

    def test_higher_consumption_is_reported_as_negative_savings(self):
        self._ajouter_periodes_completes(consommation_apres=Decimal("12"))

        result = calculer_impact_telemetrie_action(self.action)

        self.assertEqual(result["economie_kwh"], Decimal("-14"))

    def test_decreasing_cumulative_index_invalidates_the_measurement(self):
        self._ajouter_periodes_completes()
        first_post_reading = MesureCapteur.objects.filter(
            capteur=self.capteur,
            date_mesure__date=self.date_realisation.date() + timedelta(days=1),
        ).order_by("date_mesure").first()
        first_post_reading.valeur = Decimal("0")
        first_post_reading.save(update_fields=("valeur",))

        result = calculer_impact_telemetrie_action(self.action)

        self.assertEqual(result["statut"], "donnees_invalides")

    def test_action_serializer_rejects_equipment_from_another_organisation(self):
        autre_organisation = Organisation.objects.create(
            nom="Autre organisation",
            secteur="Industrie",
            localisation="Saint-Louis",
        )
        autre_site = Site.objects.create(
            organisation=autre_organisation,
            nom="Site externe",
            adresse="Saint-Louis",
            pays="Sénégal",
            fuseau_horaire="Africa/Dakar",
        )
        autre_equipement = Equipement.objects.create(
            site=autre_site,
            nom="Compresseur externe",
            categorie="Compresseur",
            puissance_nominale_kw=Decimal("3.000"),
        )
        serializer = ActionSerializer(
            data={
                "recommandation": str(self.recommandation.id),
                "equipement": str(autre_equipement.id),
                "titre": "Action hors organisation",
            },
            context={"request": SimpleNamespace(user=self.user)},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("equipement", serializer.errors)

    def test_management_command_persists_result_once(self):
        self._ajouter_periodes_completes()

        call_command("mesurer_impacts_telemetrie", stdout=StringIO(), stderr=StringIO())

        self.action.refresh_from_db()
        self.assertEqual(self.action.energie_reference_kwh, Decimal("70"))
        self.assertEqual(self.action.energie_post_action_kwh, Decimal("56"))
        self.assertEqual(self.action.economie_energie_kwh, Decimal("14"))
        self.assertEqual(self.action.capteur_impact, self.capteur)
        self.assertIsNotNone(self.action.date_mesure_energie)

    def test_daily_command_runs_anomaly_analysis_before_impact_measurement(self):
        from analysis.management.commands.executer_taches_energetiques_quotidiennes import Command

        with patch(
            "analysis.management.commands.executer_taches_energetiques_quotidiennes.call_command"
        ) as call_command_mock, patch(
            "analysis.management.commands.executer_taches_energetiques_quotidiennes.executer_alertes_energetiques"
        ) as alertes_mock:
            Command(stdout=StringIO()).handle()

        self.assertEqual(
            call_command_mock.call_args_list,
            [
                call("analyser_compteurs"),
                call("analyser_anomalies_telemetrie"),
                call("mesurer_impacts_telemetrie"),
                call("analyser_occupation"),
            ],
        )
        alertes_mock.assert_called_once_with()
