from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from energy.models import BaremeTarifaire
from energy.services.prediction_tranches import (
    bareme_actuel,
    etat_tranche,
    predire_kwh,
)


class PredictionTranchesTests(SimpleTestCase):
    @patch("energy.services.prediction_tranches.BaremeTarifaire.objects.filter")
    def test_bareme_filtre_par_categorie_mode_et_date(self, mock_filter):
        queryset = Mock()
        mock_filter.return_value = queryset
        queryset.filter.return_value = queryset
        queryset.order_by.return_value = ["barème"]

        resultat = bareme_actuel("DPP", "PREPAYE")

        self.assertEqual(resultat, ["barème"])
        mock_filter.assert_called_once()
        self.assertEqual(mock_filter.call_args.kwargs["categorie"], "DPP")
        self.assertEqual(mock_filter.call_args.kwargs["mode_paiement"], "PREPAYE")

    @patch("energy.services.prediction_tranches.bareme_actuel")
    def test_etat_tranche_n_invente_pas_de_periode_de_reference(self, mock_bareme):
        mock_bareme.return_value = [
            SimpleNamespace(
                nom_tranche="Tranche 2",
                kwh_min=Decimal("150"),
                kwh_max=Decimal("250"),
                prix_fcfa_par_kwh=Decimal("136.49"),
            ),
            SimpleNamespace(
                nom_tranche="Tranche 3",
                kwh_min=Decimal("250"),
                kwh_max=None,
                prix_fcfa_par_kwh=Decimal("136.49"),
            ),
        ]
        compteur = SimpleNamespace(
            categorie_tarifaire="DPP",
            mode_paiement="PREPAYE",
        )

        resultat = etat_tranche(compteur)

        self.assertIn("période de référence", resultat["erreur"])
        self.assertEqual(resultat["bareme"][1]["prix_fcfa_par_kwh"], 136.49)
        self.assertNotIn("kwh_restants_dans_tranche", resultat)

    @patch("energy.services.prediction_tranches.bareme_actuel")
    def test_montant_de_recharge_seul_ne_produit_pas_de_kwh_fictifs(self, mock_bareme):
        mock_bareme.return_value = [SimpleNamespace()]
        compteur = SimpleNamespace(
            categorie_tarifaire="DPP",
            mode_paiement="PREPAYE",
        )

        resultat = predire_kwh(compteur, Decimal("5000"))

        self.assertIsNone(resultat["kwh_estimes"])
        self.assertEqual(resultat["detail_paliers"], [])
        self.assertIn("retenues applicables", resultat["erreur"])


class BaremeTarifaireSeedTests(TestCase):
    def test_barèmes_2026_distinguent_les_categories_et_le_prépaiement(self):
        baremes = BaremeTarifaire.objects.filter(
            date_entree_vigueur=date(2026, 1, 1)
        )

        self.assertEqual(baremes.count(), 24)
        self.assertEqual(
            set(baremes.values_list("categorie", "mode_paiement")),
            {
                (categorie, mode)
                for categorie in ("DPP", "DMP", "PPP", "PMP")
                for mode in ("PREPAYE", "POSTPAYE")
            },
        )
        for categorie in ("DPP", "DMP", "PPP", "PMP"):
            tranches_prepayees = list(
                baremes.filter(
                    categorie=categorie,
                    mode_paiement="PREPAYE",
                ).order_by("ordre")
            )
            self.assertEqual(
                tranches_prepayees[2].prix_fcfa_par_kwh,
                tranches_prepayees[1].prix_fcfa_par_kwh,
            )

        dpp_postpaye = list(
            baremes.filter(categorie="DPP", mode_paiement="POSTPAYE").order_by("ordre")
        )
        self.assertEqual(dpp_postpaye[2].prix_fcfa_par_kwh, Decimal("159.3600"))
        self.assertEqual(
            list(
                baremes.filter(categorie="PMP", mode_paiement="POSTPAYE")
                .order_by("ordre")
                .values_list("prix_fcfa_par_kwh", flat=True)
            ),
            [Decimal("165.0100"), Decimal("191.0100"), Decimal("210.8100")],
        )
