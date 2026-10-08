from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Utilisateur
from organizations.models import Organisation, UtilisateurOrganisation

from .models import Anomalie, Livrable, MemoireStrategique, OpportuniteFinancement, ResultatMetrique
from .services.report_service import _chemin_logo, _contenu_memoire, generer_pdf_livrable


class InternalAIClientTokenTests(TestCase):
	def test_rag_client_uses_configured_ai_service_url(self):
		from .api import ai_client

		with override_settings(AI_SERVICE_URL="http://ai:8001"), patch.object(
			ai_client, "AI_SERVICE_INTERNAL_TOKEN", "test-service-token"
		), patch.object(ai_client, "_post_json", return_value={"answer": "ok"}) as post_json:
			result = ai_client.interroger_assistant("test", "organisation-test")

		self.assertEqual(result, {"answer": "ok"})
		self.assertEqual(post_json.call_args.args[0], "http://ai:8001/internal/query")

	def test_rag_client_does_not_call_service_without_token(self):
		from .api import ai_client

		with patch.object(ai_client, "AI_SERVICE_INTERNAL_TOKEN", ""), patch.object(
			ai_client, "_post_json"
		) as post_json:
			result = ai_client._appeler("http://service.test", {}, "Test")

		self.assertIn("_error", result)
		post_json.assert_not_called()

	def test_image_client_does_not_call_service_without_token(self):
		from energy.services import ai_client

		with patch.object(ai_client, "AI_SERVICE_INTERNAL_TOKEN", ""), patch.object(
			ai_client, "_post_multipart"
		) as post_multipart:
			result = ai_client.analyser_image(b"image", "test.jpg")

		self.assertIn("_error", result)
		post_multipart.assert_not_called()


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


class OpportuniteFinancementPublicationTests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.url = "/api/analyses/opportunites-financement/"
		self.opportunite = OpportuniteFinancement.objects.create(
			organisme="AEME",
			titre="Subvention solaire à vérifier",
			description="Programme de soutien à l’énergie solaire.",
			criteres_eligibilite="PME sénégalaise",
			statut=OpportuniteFinancement.Statut.A_VERIFIER,
		)
		self.admin = Utilisateur.objects.create_user(
			email="admin-financement@example.com",
			nom="Admin financement",
			role=Utilisateur.Role.ADMIN_ORGANISATION,
			actif=True,
		)
		self.member = Utilisateur.objects.create_user(
			email="membre-financement@example.com",
			nom="Membre organisation",
			role=Utilisateur.Role.UTILISATEUR_ORGANISATION,
			actif=True,
		)

	def _results(self, response):
		return response.data.get("results", response.data) if isinstance(response.data, dict) else response.data

	def test_opportunity_is_hidden_until_an_admin_publishes_it(self):
		self.client.force_authenticate(user=self.member)
		response = self.client.get(self.url)
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertNotIn(str(self.opportunite.id), [item["id"] for item in self._results(response)])

		self.client.force_authenticate(user=self.admin)
		response = self.client.get(f"{self.url}?statut=A_VERIFIER")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertIn(str(self.opportunite.id), [item["id"] for item in self._results(response)])

		response = self.client.post(f"{self.url}{self.opportunite.id}/valider/")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.opportunite.refresh_from_db()
		self.assertEqual(self.opportunite.statut, OpportuniteFinancement.Statut.ACTIF)

		self.client.force_authenticate(user=self.member)
		response = self.client.get(self.url)
		self.assertIn(str(self.opportunite.id), [item["id"] for item in self._results(response)])

	def test_non_admin_cannot_review_pending_opportunity(self):
		self.client.force_authenticate(user=self.member)
		response = self.client.get(f"{self.url}?statut=A_VERIFIER")
		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(self._results(response), [])

		response = self.client.post(f"{self.url}{self.opportunite.id}/valider/")
		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


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
			impact_attendu_fcfa=50000,
			impact_mesure_fcfa=42000,
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
		contenu_pdf = pdf.read()
		self.assertTrue(contenu_pdf.startswith(b"%PDF-"))
		self.assertIn(b"/Subtype /Image", contenu_pdf)
		self.assertEqual(Path(_chemin_logo()).name, "logo_ecoscan.png")
		self.assertTrue(Path(_chemin_logo()).is_file())
		self.assertIsNone(livrable.fiche_projet)


class LivrableDownloadTests(TestCase):
	def setUp(self):
		self.user = Utilisateur.objects.create_user(
			email="livrable@example.com",
			password="test-password",
			nom="Utilisateur test",
		)
		self.organisation = Organisation.objects.create(
			nom="EcoScan Test",
			secteur="Énergie",
			localisation="Dakar",
		)
		UtilisateurOrganisation.objects.create(
			organisation=self.organisation,
			utilisateur=self.user,
		)
		self.livrable = Livrable.objects.create(
			organisation=self.organisation,
			nom="EcoScan_Memoire_Test",
			type="MEMOIRE",
		)
		self.client = APIClient()
		self.client.force_authenticate(user=self.user)

	def test_download_serves_pdf_to_an_organisation_member(self):
		with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
			nom_fichier = "EcoScan_Memoire_Consumption_drop_—_investigation_prioritaire.pdf"
			path = default_storage.save(
				f"livrables/{self.livrable.id}/{nom_fichier}",
				ContentFile(b"%PDF-test"),
			)
			self.livrable.url_fichier = default_storage.url(path)
			self.livrable.save(update_fields=("url_fichier",))

			response = self.client.get(
				f"/api/analyses/livrables/{self.livrable.id}/telecharger/"
			)

			self.assertEqual(response.status_code, status.HTTP_200_OK)
			self.assertIn("attachment", response["Content-Disposition"])
			self.assertEqual(b"".join(response.streaming_content), b"%PDF-test")

	def test_download_hides_livrables_from_other_organisations(self):
		other_user = Utilisateur.objects.create_user(
			email="autre@example.com",
			password="test-password",
			nom="Autre utilisateur",
		)
		self.client.force_authenticate(user=other_user)

		response = self.client.get(
			f"/api/analyses/livrables/{self.livrable.id}/telecharger/"
		)

		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

	def test_download_returns_not_found_when_the_pdf_is_missing(self):
		response = self.client.get(
			f"/api/analyses/livrables/{self.livrable.id}/telecharger/"
		)

		self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class WoyofalRecommendationTests(TestCase):
	def test_ritual_anomaly_creates_recommendation_in_kwh_without_ai_tariff(self):
		from analysis.models import Recommandation
		from analysis.services.auto_recommandation import generer_recommandation_auto

		organisation = Organisation.objects.create(
			nom="EcoScan Woyofal",
			secteur="Commerce",
			localisation="Dakar",
		)
		now = timezone.now()
		resultat = ResultatMetrique.objects.create(
			organisation=organisation,
			code_metrique="variation_woyofal_rituelle_vs_moyenne_recente",
			version_metrique="1.0",
			valeur=Decimal("20"),
			unite="%",
			periode_debut=now - timedelta(days=1),
			periode_fin=now,
			baseline_type="moyenne_simple",
			baseline_valeur=Decimal("100"),
			completude=Decimal("1"),
			statut_qualite=ResultatMetrique.StatutQualite.FIABLE,
		)
		anomalie = Anomalie.objects.create(
			organisation=organisation,
			resultat_metrique=resultat,
			type="consumption_spike",
			severite=Anomalie.Severite.ALERTE,
			valeur_observee=Decimal("120"),
			valeur_attendue=Decimal("100"),
			ecart_pourcentage=Decimal("20"),
			statut=Anomalie.Statut.CONFIRMED,
		)

		with patch("analysis.services.auto_recommandation.demander_hypothese") as demander_ia, patch(
			"analysis.services.auto_recommandation.synchroniser_memoire_recommandations"
		):
			recommandation = generer_recommandation_auto(anomalie)

		demander_ia.assert_not_called()
		self.assertEqual(recommandation.unite, "kWh")
		self.assertEqual(recommandation.economie_estimee, Decimal("20.00"))
		self.assertEqual(recommandation.objectif.unite, "kWh")
		self.assertEqual(recommandation.objectif.valeur_cible, Decimal("10.000000"))
		self.assertEqual(recommandation.statut, Recommandation.Statut.PROPOSEE)
