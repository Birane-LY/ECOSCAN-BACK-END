from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organisation

from .models import Livrable, MemoireStrategique, OpportuniteFinancement
from .services.report_service import _contenu_memoire, generer_pdf_livrable


@override_settings(N8N_INGESTION_TOKEN="test-ingestion-token")
class OpportuniteFinancementIngestionTests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.url = "/api/analyses/opportunites-financement/ingestion/"

	def test_ingestion_n8n_cree_opportunite_a_verifier(self):
		response = self.client.post(
			self.url,
			{
				"organisme": "AEME",
				"titre": "Aide solaire pour PME",
				"description": "Programme de financement solaire.",
				"criteres_eligibilite": "PME sénégalaise",
				"secteur": "Énergie",
				"url_source": "https://example.com/aide-solaire",
				"confiance_extraction": 0.6,
			},
			format="json",
			HTTP_X_N8N_INGESTION_TOKEN="test-ingestion-token",
		)

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["crees"], 1)
		opportunite = OpportuniteFinancement.objects.get(titre="Aide solaire pour PME")
		self.assertEqual(opportunite.statut, OpportuniteFinancement.Statut.A_VERIFIER)

	def test_ingestion_n8n_refuse_un_token_invalide(self):
		response = self.client.post(
			self.url,
			{"organisme": "AEME", "titre": "Tentative"},
			format="json",
			HTTP_X_N8N_INGESTION_TOKEN="mauvais-token",
		)

		self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ReportGenerationTests(TestCase):
	def test_generer_un_rapport_memoire_sans_projet(self):
		organisation = Organisation.objects.create(
			nom="EcoScan Test",
			secteur="Énergie",
			localisation="Dakar",
		)
		memoire = MemoireStrategique.objects.create(
			organisation=organisation,
			titre="Réduction de consommation",
			signal_initial="Hausse de 18 % de la consommation.",
			hypothese_texte="Un équipement fonctionne hors horaires.",
			action_texte="Programmer l'arrêt automatique.",
		)
		livrable = Livrable.objects.create(
			organisation=organisation,
			memoire=memoire,
			nom="Rapport mémoire",
			type="MEMOIRE",
		)

		rendu_memoire = Mock(wraps=_contenu_memoire)
		with patch.dict(
			"analysis.services.report_service.SECTION_BUILDERS",
			{"memoire": ("Mémoire stratégique", rendu_memoire)},
		):
			pdf = generer_pdf_livrable(livrable)

		rendu_memoire.assert_called_once()
		self.assertIs(rendu_memoire.call_args.args[0], memoire)
		self.assertTrue(pdf.read().startswith(b"%PDF-"))
		self.assertIsNone(livrable.fiche_projet)
