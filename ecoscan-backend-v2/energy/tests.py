from datetime import date, datetime, timedelta
from io import BytesIO
from urllib.error import HTTPError
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from django.utils import timezone

from billing.models import Abonnement, Plan
from organizations.models import Organisation, Site, UtilisateurOrganisation
from .services.ai_client import transcrire_audio
from .models import PointSuiviEnergetique, ReleveRituelEnergetique


class RituelEnergetiqueApiTests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="rituel@example.com",
            password="test-password",
            nom="Utilisateur test",
        )
        self.organisation = Organisation.objects.create(
            nom="PME test",
            secteur="Commerce",
            localisation="Dakar",
        )
        UtilisateurOrganisation.objects.create(
            organisation=self.organisation,
            utilisateur=self.user,
        )
        self.site = self.create_site(self.organisation, "Site principal")
        self.other_organisation = Organisation.objects.create(
            nom="Autre PME",
            secteur="Commerce",
            localisation="Thiès",
        )
        self.other_site = self.create_site(self.other_organisation, "Site étranger")
        self.client.force_authenticate(self.user)

    @staticmethod
    def create_site(organisation, nom):
        return Site.objects.create(
            organisation=organisation,
            nom=nom,
            adresse="Dakar",
            pays="Sénégal",
            fuseau_horaire="Africa/Dakar",
        )

    def test_create_site_scoped_point_without_physical_meter(self):
        response = self.client.post(
            "/api/energies/points-suivi-energetique/",
            {
                "organisation": str(self.organisation.id),
                "site": str(self.site.id),
                "nom": "Suivi atelier",
                "mode_mesure": PointSuiviEnergetique.ModeMesure.SOLDE_WOYOFAL,
                "compteur": None,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["compteur"])
        self.assertEqual(
            PointSuiviEnergetique.objects.get().site_id,
            self.site.id,
        )

    def test_reject_point_for_site_outside_user_organisation(self):
        response = self.client.post(
            "/api/energies/points-suivi-energetique/",
            {
                "organisation": str(self.organisation.id),
                "site": str(self.other_site.id),
                "nom": "Suivi non autorisé",
                "mode_mesure": PointSuiviEnergetique.ModeMesure.INDEX_CUMULATIF,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("site", response.data)

    @patch("energy.serializers.timezone.localdate", return_value=date(2026, 6, 1))
    @patch("energy.serializers.timezone.localtime")
    def test_reject_duplicate_boundary_reading(self, mock_localtime, _mock_localdate):
        mock_localtime.return_value = datetime(
            2026, 6, 1, 12, 30, tzinfo=ZoneInfo("Africa/Dakar")
        )
        point = PointSuiviEnergetique.objects.create(
            organisation=self.organisation,
            site=self.site,
            nom="Suivi atelier",
            mode_mesure=PointSuiviEnergetique.ModeMesure.INDEX_CUMULATIF,
        )
        payload = {
            "point_suivi": str(point.id),
            "date_releve": date(2026, 6, 1).isoformat(),
            "creneau": "12:00",
            "valeur_kwh": "100",
        }
        first = self.client.post(
            "/api/energies/releves-rituel/",
            payload,
            format="json",
        )
        second = self.client.post(
            "/api/energies/releves-rituel/",
            payload,
            format="json",
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 400)

    @patch("energy.serializers.timezone.localdate", return_value=date(2026, 6, 1))
    @patch("energy.serializers.timezone.localtime")
    def test_reject_elapsed_reading_slot(self, mock_localtime, _mock_localdate):
        mock_localtime.return_value = datetime(
            2026, 6, 1, 12, 30, tzinfo=ZoneInfo("Africa/Dakar")
        )
        point = PointSuiviEnergetique.objects.create(
            organisation=self.organisation,
            site=self.site,
            nom="Suivi à temps",
            mode_mesure=PointSuiviEnergetique.ModeMesure.INDEX_CUMULATIF,
        )
        response = self.client.post(
            "/api/energies/releves-rituel/",
            {
                "point_suivi": str(point.id),
                "date_releve": date(2026, 6, 1).isoformat(),
                "creneau": "08:00",
                "valeur_kwh": "100",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("creneau", response.data)

    def test_recharge_is_rejected_for_cumulative_index_point(self):
        point = PointSuiviEnergetique.objects.create(
            organisation=self.organisation,
            site=self.site,
            nom="Suivi SENELEC",
            mode_mesure=PointSuiviEnergetique.ModeMesure.INDEX_CUMULATIF,
        )
        response = self.client.post(
            "/api/energies/recharges-rituel-woyofal/",
            {
                "point_suivi": str(point.id),
                "montant_fcfa": "5000",
                "kwh_credites": "20",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("point_suivi", response.data)

    def test_recharge_is_saved_for_woyofal_point(self):
        point = PointSuiviEnergetique.objects.create(
            organisation=self.organisation,
            site=self.site,
            nom="Suivi Woyofal",
            mode_mesure=PointSuiviEnergetique.ModeMesure.SOLDE_WOYOFAL,
        )
        response = self.client.post(
            "/api/energies/recharges-rituel-woyofal/",
            {
                "point_suivi": str(point.id),
                "montant_fcfa": "5000",
                "kwh_credites": "20.5",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["kwh_credites"], "20.500")

    def test_create_point_without_site(self):
        response = self.client.post(
            "/api/energies/points-suivi-energetique/",
            {
                "organisation": str(self.organisation.id),
                "site": None,
                "nom": "Suivi sans site",
                "mode_mesure": PointSuiviEnergetique.ModeMesure.SOLDE_WOYOFAL,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["site"])
        self.assertEqual(str(response.data["organisation"]), str(self.organisation.id))

    @patch("energy.views.transcrire_audio", return_value={
        "transcription": "La production a commencé tôt.",
        "language_detected": "fr",
        "engine_used": "groq:whisper-large-v3",
        "execution_time_seconds": 1.2,
    })
    def test_transcription_vocale_est_disponible_aux_membres_abonnes(self, transcrire):
        self.organisation.statut = Organisation.Statut.ACTIVE
        self.organisation.save(update_fields=("statut",))
        plan = Plan.objects.create(code="audio-test", nom="Audio test", prix_mensuel="10000")
        Abonnement.objects.create(
            organisation=self.organisation,
            plan=plan,
            statut=Abonnement.Statut.ACTIVE,
            periodicite="MENSUEL",
            debut=timezone.now(),
            fin_periode=timezone.now() + timedelta(days=30),
            fournisseur=Abonnement.Fournisseur.MANUAL,
        )

        response = self.client.post(
            "/api/energies/transcrire-audio/",
            {
                "file": SimpleUploadedFile("bilan-vocal.webm", b"audio-bytes", content_type="audio/webm"),
                "language": "fr",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["transcription"], "La production a commencé tôt.")
        transcrire.assert_called_once()

    @patch("energy.views.transcrire_audio")
    def test_transcription_vocale_refuse_un_membre_sans_acces_abonnement(self, transcrire):
        response = self.client.post(
            "/api/energies/transcrire-audio/",
            {
                "file": SimpleUploadedFile("bilan-vocal.webm", b"audio-bytes", content_type="audio/webm"),
                "language": "fr",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 403)
        transcrire.assert_not_called()

    @patch("energy.services.ai_client.AI_SERVICE_INTERNAL_TOKEN", "configured-test-token")
    @patch("energy.services.ai_client._post_multipart")
    def test_transcription_explique_une_erreur_401_du_service_ia(self, post_multipart):
        post_multipart.side_effect = HTTPError(
            "http://ai.test/audio/transcribe",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"detail":"Jeton absent ou invalide."}'),
        )

        result = transcrire_audio(b"audio", "bilan-vocal.webm")

        self.assertIn("Redémarrez les deux serveurs", result["_error"])

    @patch("energy.services.ai_client.AI_SERVICE_INTERNAL_TOKEN", "configured-test-token")
    @patch("energy.services.ai_client._post_multipart")
    def test_transcription_preserve_le_message_422_sans_parole(self, post_multipart):
        post_multipart.side_effect = HTTPError(
            "http://ai.test/audio/transcribe",
            422,
            "Unprocessable Content",
            {},
            BytesIO(b'{"detail":"Aucune parole reconnue."}'),
        )

        result = transcrire_audio(b"audio", "bilan-vocal.webm")

        self.assertEqual(result["_error"], "Aucune parole reconnue.")
