from datetime import date, datetime, timezone as datetime_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from analysis.services.alertes_energetiques import (
    comparer_facture_telemetrie,
)
from energy.services.prediction_tranches import bareme_actuel
from energy_assets.models import Capteur


class AlertesEnergetiquesTests(SimpleTestCase):
    def test_periode_de_facture_doit_etre_complete_et_coherente(self):
        from analysis.views import _extraire_periode_facture

        debut, fin = _extraire_periode_facture(
            {"periode_debut": "01/02/2026", "periode_fin": "28-02-2026"}
        )

        self.assertEqual(debut.date().isoformat(), "2026-02-01")
        self.assertEqual(fin.date().isoformat(), "2026-02-28")
        self.assertEqual(fin.hour, 23)
        with self.assertRaisesMessage(ValueError, "période de facturation complète"):
            _extraire_periode_facture({"periode_debut": "01/02/2026"})
        with self.assertRaisesMessage(ValueError, "doit précéder"):
            _extraire_periode_facture(
                {"periode_debut": "02/02/2026", "periode_fin": "01/02/2026"}
            )

    def test_aucun_bareme_domestique_n_est_choisi_par_defaut(self):
        self.assertEqual(bareme_actuel(None, "POSTPAYE"), [])
        self.assertEqual(bareme_actuel("DPP", None), [])

    @patch("analysis.services.factures.bareme_actuel")
    def test_premiere_facture_hors_30_jours_ne_proratise_pas_le_seuil(self, mock_bareme):
        from analysis.services.factures import _analyser_premiere_facture

        facture = SimpleNamespace(
            valeur=Decimal("6395"),
            organisation_id="organisation-1",
            compteur=SimpleNamespace(
                categorie_tarifaire="PMP",
                mode_paiement="POSTPAYE",
            ),
            periode_debut=datetime(2026, 5, 21, tzinfo=datetime_timezone.utc),
            periode_fin=datetime(2026, 7, 22, 23, 59, tzinfo=datetime_timezone.utc),
        )
        mock_bareme.return_value = [
            SimpleNamespace(kwh_min=Decimal("0")),
            SimpleNamespace(kwh_min=Decimal("500")),
        ]

        self.assertIsNone(_analyser_premiere_facture(facture))
        mock_bareme.assert_called_once_with(
            "PMP",
            "POSTPAYE",
            a_la_date=date(2026, 7, 22),
        )

    @patch("analysis.services.alertes_energetiques.MesureCapteur.objects.filter")
    @patch("analysis.services.alertes_energetiques.Capteur.objects.filter")
    def test_comparaison_facture_qualifie_la_provenance_simulee(
        self, mock_capteurs, mock_mesures
    ):
        debut = datetime(2026, 2, 1, tzinfo=datetime_timezone.utc)
        fin = datetime(2026, 2, 28, 23, 59, tzinfo=datetime_timezone.utc)
        capteur = SimpleNamespace(
            pk="sensor-1",
            mode=Capteur.Mode.SIMULATED,
        )
        mock_capteurs.return_value.order_by.return_value = [capteur]
        mock_mesures.return_value.order_by.return_value = [
            SimpleNamespace(
                date_mesure=datetime(2026, 1, 31, 23, 59, tzinfo=datetime_timezone.utc),
                valeur=Decimal("100"),
                unite="kWh",
            ),
            SimpleNamespace(
                date_mesure=datetime(2026, 2, 2, tzinfo=datetime_timezone.utc),
                valeur=Decimal("110"),
                unite="kWh",
            ),
            SimpleNamespace(
                date_mesure=datetime(2026, 2, 9, tzinfo=datetime_timezone.utc),
                valeur=Decimal("118"),
                unite="kWh",
            ),
            SimpleNamespace(
                date_mesure=datetime(2026, 2, 16, tzinfo=datetime_timezone.utc),
                valeur=Decimal("125"),
                unite="kWh",
            ),
            SimpleNamespace(
                date_mesure=datetime(2026, 2, 23, tzinfo=datetime_timezone.utc),
                valeur=Decimal("134"),
                unite="kWh",
            ),
            SimpleNamespace(
                date_mesure=datetime(2026, 2, 27, tzinfo=datetime_timezone.utc),
                valeur=Decimal("140"),
                unite="kWh",
            ),
            SimpleNamespace(
                date_mesure=datetime(2026, 3, 1, 0, 0, tzinfo=datetime_timezone.utc),
                valeur=Decimal("150"),
                unite="kWh",
            ),
        ]
        facture = SimpleNamespace(
            code_metrique="consommation_facture_periodique",
            valeur=Decimal("50"),
            statut_qualite="FIABLE",
            fichier_source=SimpleNamespace(
                import_donnees=SimpleNamespace(
                    rapport_analyse={
                        "controle_document": {"emetteur_confirme": "SENELEC"}
                    }
                )
            ),
            compteur_id="meter-1",
            compteur=SimpleNamespace(
                site_id="site-1",
                site=SimpleNamespace(compteurs=SimpleNamespace(count=lambda: 1)),
            ),
            periode_debut=debut,
            periode_fin=fin,
        )

        resultat = comparer_facture_telemetrie(facture)

        self.assertEqual(resultat["statut"], "scenario_simule")
        self.assertEqual(resultat["provenance"], "SIMULATED")
        self.assertEqual(resultat["telemetrie_kwh"], 50.0)

    @patch("analysis.services.alertes_energetiques.Capteur.objects.filter")
    def test_comparaison_refuse_plusieurs_capteurs_reels(self, mock_capteurs):
        mock_capteurs.return_value.order_by.return_value = [
            SimpleNamespace(pk="sensor-1", mode=Capteur.Mode.REAL),
            SimpleNamespace(pk="sensor-2", mode=Capteur.Mode.REAL),
        ]
        facture = SimpleNamespace(
            code_metrique="consommation_facture_periodique",
            valeur=Decimal("50"),
            statut_qualite="FIABLE",
            fichier_source=SimpleNamespace(
                import_donnees=SimpleNamespace(
                    rapport_analyse={
                        "controle_document": {"emetteur_confirme": "SENELEC"}
                    }
                )
            ),
            compteur_id="meter-1",
            compteur=SimpleNamespace(
                site_id="site-1",
                site=SimpleNamespace(compteurs=SimpleNamespace(count=lambda: 1)),
            ),
            periode_debut=datetime(2026, 2, 1, tzinfo=datetime_timezone.utc),
            periode_fin=datetime(2026, 2, 28, 23, 59, tzinfo=datetime_timezone.utc),
        )

        resultat = comparer_facture_telemetrie(facture)

        self.assertEqual(resultat["statut"], "donnees_insuffisantes")
        self.assertIn("double comptage", resultat["raison"])
