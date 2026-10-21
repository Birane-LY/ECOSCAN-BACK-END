from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from django.test import TestCase, override_settings

from billing.models import Abonnement, Plan
from billing.testing import grant_test_trial
from organizations.models import Organisation, Site, UtilisateurOrganisation
from .services.ai_client import transcrire_audio
from .views import _verifier_automatiquement_facture
from .models import (
    FichierSource,
    ImportDonnees,
    PointSuiviEnergetique,
    ReleveRituelEnergetique,
)
from .serializers import valider_fichier_source
from .services.classificateur import ClassificateurService
from .services.parseur import ParseurMetierService


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
            statut=Organisation.Statut.ACTIVE,
        )
        grant_test_trial(self.organisation)
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


class FichierSourceValidationTests(TestCase):
    def test_accepts_a_pdf_with_a_matching_signature(self):
        fichier = SimpleUploadedFile("facture.pdf", b"%PDF-1.7\ncontenu")

        self.assertIs(valider_fichier_source(fichier), fichier)

    def test_rejects_a_file_with_a_mismatched_signature(self):
        fichier = SimpleUploadedFile("facture.pdf", b"not a pdf")

        with self.assertRaises(ValidationError):
            valider_fichier_source(fichier)

    def test_rejects_an_unapproved_extension(self):
        fichier = SimpleUploadedFile("facture.exe", b"%PDF-1.7")

        with self.assertRaises(ValidationError):
            valider_fichier_source(fichier)


class FichierSourceUploadApiTests(TestCase):
    def test_multipart_upload_uses_the_users_organisation_when_omitted(self):
        organisation = Organisation.objects.create(
            nom="Organisation dépôt facture",
            secteur="Commerce",
            localisation="Dakar",
            statut=Organisation.Statut.ACTIVE,
        )
        grant_test_trial(organisation)
        user = get_user_model().objects.create_user(
            email="upload-facture@example.com",
            password="test-password",
            nom="Admin dépôt",
            role="ADMIN_ORGANISATION",
            actif=True,
        )
        UtilisateurOrganisation.objects.create(
            organisation=organisation,
            utilisateur=user,
        )
        client = APIClient()
        client.force_authenticate(user)

        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = client.post(
                reverse("energy:fichier-source-list"),
                {"fichier": SimpleUploadedFile("facture.pdf", b"%PDF-1.7\ncontenu")},
                format="multipart",
            )
            import_response = client.post(
                reverse("energy:import-donnees-list"),
                {"fichier_source": response.data["id"]},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(str(response.data["organisation"]), str(organisation.id))
        self.assertEqual(response.data["nom"], "facture.pdf")
        self.assertEqual(import_response.status_code, status.HTTP_201_CREATED, import_response.data)
        self.assertEqual(str(import_response.data["organisation"]), str(organisation.id))


class ClassificateurDocumentTests(TestCase):
    def test_energy_document_is_classified_as_an_unverified_candidate(self):
        classification = ClassificateurService().classifier_document(
            "SENELEC facture consommation kWh montant total facture"
        )

        self.assertEqual(classification["decision"], "candidate")
        self.assertEqual(classification["authenticite"]["statut"], "NON_VERIFIEE")
        self.assertFalse(
            classification["authenticite"]["verification_officielle_disponible"]
        )

    def test_woyofal_recharge_is_not_classified_as_a_senelec_invoice(self):
        classification = ClassificateurService().classifier_document(
            "SENELEC Woyofal ticket recharge 20 kWh crédités"
        )

        self.assertEqual(classification["type"], "TICKET_RECHARGE_WOYOFAL")
        self.assertEqual(classification["emetteur"], "SENELEC")
        self.assertEqual(classification["decision"], "reject")
        self.assertIn("ne représentent pas la consommation", classification["motif_rejet"])

    def test_non_energy_document_is_rejected(self):
        classification = ClassificateurService().classifier_document(
            "Relevé bancaire"
        )

        self.assertEqual(classification["decision"], "reject")
        self.assertEqual(classification["authenticite"]["statut"], "REJETE")


class ParseurFactureTests(TestCase):
    def test_single_new_index_is_flagged_as_incomplete_and_not_duplicated(self):
        parseur = ParseurMetierService()
        champs = parseur.extraire_champs_energetiques("Nouveau index : 197")
        validation = parseur.valider_champs_energetiques(champs)
        warning_codes = {warning["code"] for warning in validation["warnings"]}

        self.assertEqual(champs["nouveau_index"], 197)
        self.assertIsNone(champs["ancien_index"])
        self.assertIsNone(champs["consommation_kwh"])
        self.assertNotIn("index", champs)
        self.assertNotIn("montant", champs)
        self.assertNotIn("date", champs)
        self.assertIn("ANCIEN_INDEX_MANQUANT", warning_codes)
        self.assertIn("CONSOMMATION_MANQUANTE", warning_codes)
        self.assertTrue(validation["requires_review"])


class VerificationAutomatiqueFactureTests(TestCase):
    def setUp(self):
        self.classification = {
            "type": "FACTURE_SENELEC",
            "emetteur": "SENELEC",
            "relevance_score": 0.95,
        }
        self.validation = {
            "valide": True,
            "erreurs": [],
            "warnings": [],
        }
        self.champs = {
            "ancien_index": 1000,
            "nouveau_index": 1231,
            "consommation_kwh": 231,
            "montant_net_paye": 28700,
            "periode_debut": "01/09/2024",
            "periode_fin": "30/09/2024",
        }

    def test_high_confidence_consistent_invoice_passes_automatic_checks(self):
        result = _verifier_automatiquement_facture(
            self.classification,
            self.validation,
            self.champs,
            Decimal("99.46"),
            Decimal("0.99"),
        )

        self.assertTrue(result["valide"])
        self.assertEqual(result["raisons"], [])
        self.assertEqual(result["periode_normalisee"]["periode_debut"], "2024-09-01")

    def test_low_confidence_or_incomplete_invoice_is_not_automatically_approved(self):
        result = _verifier_automatiquement_facture(
            self.classification,
            {**self.validation, "warnings": [{"message": "Montant incertain."}]},
            self.champs,
            Decimal("89.99"),
            Decimal("0.99"),
        )

        self.assertFalse(result["valide"])
        self.assertTrue(any("90 %" in reason for reason in result["raisons"]))
        self.assertIn("Montant incertain.", result["raisons"])

    def test_invoice_amount_above_supported_range_is_not_automatically_approved(self):
        champs = {**self.champs, "montant_net_paye": "1000000000000"}

        result = _verifier_automatiquement_facture(
            self.classification,
            self.validation,
            champs,
            Decimal("99.46"),
            Decimal("0.99"),
        )

        self.assertFalse(result["valide"])
        self.assertIn("Le montant total dépasse la plage autorisée.", result["raisons"])


class ImportDocumentReviewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organisation = Organisation.objects.create(
            nom="Organisation revue facture",
            secteur="Commerce",
            localisation="Dakar",
            statut=Organisation.Statut.ACTIVE,
        )
        grant_test_trial(self.organisation)
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            email="admin-revue@example.com",
            password="test-password-for-review",
            nom="Admin revue",
            role="ADMIN_ORGANISATION",
            actif=True,
        )
        self.member = user_model.objects.create_user(
            email="membre-revue@example.com",
            password="test-password-for-review",
            nom="Membre revue",
            role="UTILISATEUR_ORGANISATION",
            actif=True,
        )
        for user in (self.admin, self.member):
            UtilisateurOrganisation.objects.create(
                organisation=self.organisation,
                utilisateur=user,
            )
        self.import_instance = self._creer_import()
        self.url = reverse(
            "energy:import-donnees-valider-et-publier",
            args=(self.import_instance.id,),
        )
        self.attestation = {
            "origine_senegalaise_confirmee": True,
            "authenticite_confirmee": True,
            "emetteur_confirme": "SENELEC",
            "periode_debut": "2026-03-01",
            "periode_fin": "2026-03-31",
        }

    def _creer_import(self):
        fichier_source = FichierSource.objects.create(
            organisation=self.organisation,
            nom="facture.pdf",
            fichier="fichiers-sources/facture.pdf",
            mime_type="application/pdf",
            taille_octets=10,
            hash="hash-facture-test",
            depose_par=self.admin,
        )
        return ImportDonnees.objects.create(
            fichier_source=fichier_source,
            organisation=self.organisation,
            lance_par=self.admin,
            nom_fichier=fichier_source.nom,
            format="PDF",
            type_donnees="FACTURE_SENELEC",
            statut=ImportDonnees.Statut.REVUE_REQUISE,
            rapport_analyse={
                "validation": {"valide": True},
                "controle_document": {"statut": "REVUE_HUMAINE_REQUISE"},
            },
            donnees_extraites={"consommation_kwh": "120"},
        )

    def test_non_admin_cannot_validate_a_document(self):
        self.client.force_authenticate(self.member)

        response = self.client.post(self.url, self.attestation, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.import_instance.refresh_from_db()
        self.assertEqual(self.import_instance.statut, ImportDonnees.Statut.REVUE_REQUISE)

    def test_admin_must_attest_senegalese_origin_and_issuer(self):
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.import_instance.refresh_from_db()
        self.assertEqual(self.import_instance.statut, ImportDonnees.Statut.REVUE_REQUISE)

    def test_woyofal_is_not_an_issuer(self):
        self.client.force_authenticate(self.admin)
        attestation = {**self.attestation, "emetteur_confirme": "WOYOFAL"}

        response = self.client.post(self.url, attestation, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.import_instance.refresh_from_db()
        self.assertEqual(self.import_instance.statut, ImportDonnees.Statut.REVUE_REQUISE)

    def test_admin_must_provide_a_valid_billing_period(self):
        self.client.force_authenticate(self.admin)
        attestation = {**self.attestation, "periode_fin": ""}

        response = self.client.post(self.url, attestation, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.import_instance.refresh_from_db()
        self.assertEqual(self.import_instance.statut, ImportDonnees.Statut.REVUE_REQUISE)

    def test_integration_endpoint_cannot_bypass_manual_review(self):
        self.client.force_authenticate(self.member)

        response = self.client.post(
            "/api/analyses/integration/energy/",
            {"import_id": str(self.import_instance.id)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.import_instance.refresh_from_db()
        self.assertEqual(self.import_instance.statut, ImportDonnees.Statut.REVUE_REQUISE)

    @patch("analysis.views._publier_resultat_depuis_import")
    @patch("energy.views.OCRService")
    def test_high_confidence_invoice_is_checked_and_published_automatically(self, ocr_service, publier):
        self.client.force_authenticate(self.admin)
        service = ocr_service.return_value
        service.traiter_import.return_value = "Texte de facture SENELEC"
        service.calculer_score_lisibilite.return_value = 0.99
        service.classifier_document.return_value = {
            "type": "FACTURE_SENELEC",
            "emetteur": "SENELEC",
            "relevance_score": 0.95,
            "decision": "candidate",
            "signals": ["senelec", "kwh"],
            "negative_signals": [],
        }
        service.extraire_champs_energetiques.return_value = {
            "ancien_index": 1000,
            "nouveau_index": 1120,
            "consommation_kwh": 120,
            "montant_net_paye": 28700,
            "periode_debut": "01/03/2026",
            "periode_fin": "31/03/2026",
        }
        service.valider_champs_energetiques.return_value = {
            "valide": True,
            "erreurs": [],
            "warnings": [],
            "requires_review": False,
        }

        response = self.client.post(
            reverse("energy:import-donnees-lancer", args=(self.import_instance.id,)),
            {},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["statut"], ImportDonnees.Statut.TERMINE)
        controle = response.data["rapport_analyse"]["controle_document"]
        self.assertEqual(controle["statut"], "VERIFICATION_AUTOMATIQUE_PASSEE")
        self.assertFalse(controle["verification_officielle"])
        self.assertEqual(response.data["donnees_extraites"]["periode_debut"], "2026-03-01")
        publier.assert_called_once()

    @patch("analysis.views._publier_resultat_depuis_import")
    def test_admin_attestation_is_recorded_before_publication(self, publier):
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url, self.attestation, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.import_instance.refresh_from_db()
        self.assertEqual(self.import_instance.statut, ImportDonnees.Statut.TERMINE)
        controle = self.import_instance.rapport_analyse["controle_document"]
        self.assertEqual(controle["statut"], "REVUE_HUMAINE_ACQUITTEE")
        self.assertEqual(controle["emetteur_confirme"], "SENELEC")
        self.assertTrue(controle["authenticite_confirmee"])
        self.assertFalse(controle["verification_officielle"])
        self.assertEqual(self.import_instance.donnees_extraites["periode_debut"], "2026-03-01")
        self.assertEqual(self.import_instance.donnees_extraites["periode_fin"], "2026-03-31")
        publier.assert_called_once()

    @patch("analysis.views._publier_resultat_depuis_import", side_effect=RuntimeError("échec"))
    def test_failed_publication_leaves_document_in_review(self, _publier):
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url, self.attestation, format="json")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.import_instance.refresh_from_db()
        self.assertEqual(self.import_instance.statut, ImportDonnees.Statut.REVUE_REQUISE)
        self.assertEqual(
            self.import_instance.rapport_analyse["controle_document"]["statut"],
            "REVUE_HUMAINE_REQUISE",
        )

    def test_high_confidence_automatic_check_allows_publication_without_claiming_official_authenticity(self):
        from analysis.views import _document_revu_pour_publication

        self.import_instance.statut = ImportDonnees.Statut.TERMINE
        self.import_instance.score_qualite = Decimal("96.00")
        self.import_instance.score_lisibilite = Decimal("0.98")
        self.import_instance.type_donnees = "FACTURE_SENELEC"
        self.import_instance.donnees_extraites.update({
            "consommation_kwh": "120",
            "montant_net_paye": "28700",
            "periode_debut": "2026-03-01",
            "periode_fin": "2026-03-31",
        })
        self.import_instance.rapport_analyse = {
            "classification": {
                "type": "FACTURE_SENELEC",
                "emetteur": "SENELEC",
                "relevance_score": 0.95,
            },
            "validation": {"valide": True, "warnings": []},
            "controle_document": {
                "statut": "VERIFICATION_AUTOMATIQUE_PASSEE",
                "publication_automatique": True,
                "verification_officielle": False,
                "authenticite": "NON_VERIFIEE",
                "controles_automatiques": {"valide": True, "raisons": []},
            },
        }
        self.import_instance.save()

        self.assertTrue(_document_revu_pour_publication(self.import_instance))
        self.assertFalse(
            self.import_instance.rapport_analyse["controle_document"]["verification_officielle"]
        )

    def test_automatic_publication_is_rejected_when_any_check_fails(self):
        from analysis.views import _document_revu_pour_publication

        self.import_instance.statut = ImportDonnees.Statut.TERMINE
        self.import_instance.score_qualite = Decimal("96.00")
        self.import_instance.score_lisibilite = Decimal("0.98")
        self.import_instance.type_donnees = "FACTURE_SENELEC"
        self.import_instance.donnees_extraites.update({
            "consommation_kwh": "120",
            "montant_net_paye": "28700",
            "periode_debut": "2026-03-01",
            "periode_fin": "2026-03-31",
        })
        self.import_instance.rapport_analyse = {
            "classification": {
                "type": "FACTURE_SENELEC",
                "emetteur": "SENELEC",
                "relevance_score": 0.95,
            },
            "validation": {"valide": True, "warnings": []},
            "controle_document": {
                "statut": "VERIFICATION_AUTOMATIQUE_PASSEE",
                "publication_automatique": True,
                "verification_officielle": False,
                "authenticite": "NON_VERIFIEE",
                "controles_automatiques": {
                    "valide": True,
                    "raisons": ["La cohérence des données a échoué."],
                },
            },
        }
        self.import_instance.save()

        self.assertFalse(_document_revu_pour_publication(self.import_instance))
