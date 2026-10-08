from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from accounts.models import Utilisateur

from organizations.models import Organisation, UtilisateurOrganisation

from .models import Abonnement, Facture, Plan
from .services import BillingAccessService, InvoiceService


class BillingSignalTests(TestCase):
	def test_essai_standard_commence_apres_approbation_de_l_organisation(self):
		plan = Plan.objects.create(
			code="standard",
			nom="Standard",
			prix_mensuel="10000",
		)

		organisation = Organisation.objects.create(
			nom="Organisation test billing",
			secteur="Énergie",
			localisation="Dakar",
		)

		self.assertFalse(Abonnement.objects.filter(organisation=organisation).exists())
		organisation.statut = Organisation.Statut.ACTIVE
		organisation.save(update_fields=("statut",))

		abonnement = Abonnement.objects.get(organisation=organisation)
		self.assertEqual(abonnement.plan, plan)
		self.assertEqual(abonnement.statut, Abonnement.Statut.TRIALING)
		self.assertEqual(abonnement.fournisseur, Abonnement.Fournisseur.PAYDUNYA)


class BillingAccessTests(APITestCase):
	def test_un_essai_expire_ne_donne_plus_acces_meme_avant_la_tache_periodique(self):
		plan = Plan.objects.create(code="acces", nom="Accès", prix_mensuel="10000")
		organisation = Organisation.objects.create(
			nom="Organisation essai expiré",
			secteur="Énergie",
			localisation="Dakar",
			statut=Organisation.Statut.ACTIVE,
		)
		maintenant = timezone.now()
		abonnement = Abonnement.objects.create(
			organisation=organisation,
			plan=plan,
			statut=Abonnement.Statut.TRIALING,
			debut=maintenant - timedelta(days=15),
			fin_periode=maintenant - timedelta(seconds=1),
			fournisseur=Abonnement.Fournisseur.MANUAL,
		)

		service = BillingAccessService()
		self.assertIsNone(service.abonnement_courant(organisation))
		self.assertFalse(service.can_use_feature(organisation, "analytics_avances"))
		self.assertFalse(service.statut_acces(organisation)["acces"])
		self.assertEqual(abonnement.statut, Abonnement.Statut.TRIALING)

	def test_un_abonnement_en_periode_de_grace_conserve_temporairement_l_acces(self):
		plan = Plan.objects.create(code="acces-grace", nom="Accès grâce", prix_mensuel="10000")
		organisation = Organisation.objects.create(
			nom="Organisation période grâce",
			secteur="Énergie",
			localisation="Dakar",
			statut=Organisation.Statut.ACTIVE,
		)
		maintenant = timezone.now()
		abonnement = Abonnement.objects.create(
			organisation=organisation,
			plan=plan,
			statut=Abonnement.Statut.GRACE_PERIOD,
			debut=maintenant - timedelta(days=35),
			fin_periode=maintenant - timedelta(days=1),
			fin_grace=maintenant + timedelta(days=6),
			fournisseur=Abonnement.Fournisseur.MANUAL,
		)

		service = BillingAccessService()
		self.assertEqual(service.abonnement_courant(organisation), abonnement)
		self.assertTrue(service.statut_acces(organisation)["acces"])

	def test_les_endpoints_metier_refusent_un_essai_expire(self):
		plan = Plan.objects.create(code="acces-api", nom="Accès API", prix_mensuel="10000")
		utilisateur = Utilisateur.objects.create_user(
			email="essai-expire-api@example.com",
			nom="Admin essai expiré",
			role=Utilisateur.Role.ADMIN_ORGANISATION,
			actif=True,
		)
		organisation = Organisation.objects.create(
			nom="Organisation API essai expiré",
			secteur="Énergie",
			localisation="Dakar",
			statut=Organisation.Statut.ACTIVE,
		)
		UtilisateurOrganisation.objects.create(organisation=organisation, utilisateur=utilisateur)
		maintenant = timezone.now()
		Abonnement.objects.create(
			organisation=organisation,
			plan=plan,
			statut=Abonnement.Statut.TRIALING,
			debut=maintenant - timedelta(days=15),
			fin_periode=maintenant - timedelta(seconds=1),
			fournisseur=Abonnement.Fournisseur.MANUAL,
		)
		self.client.force_authenticate(user=utilisateur)

		response = self.client.get("/api/energies/achats-woyofal/")

		self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PublicPlanCatalogueTests(APITestCase):
	def test_vitrine_lit_seulement_les_formules_actives(self):
		Plan.objects.create(code="public", nom="Formule publique", prix_mensuel="15000", actif=True)
		Plan.objects.create(code="archive", nom="Formule masquée", prix_mensuel="5000", actif=False)

		response = self.client.get("/api/billing/plans/")

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual([plan["code"] for plan in response.data["results"]], ["public"])

	def test_super_admin_cree_une_formule_visible_sur_la_vitrine(self):
		admin = Utilisateur.objects.create_user(
			email="super-admin-plans@example.com",
			nom="Super Admin",
			role=Utilisateur.Role.SUPER_ADMIN,
			actif=True,
		)
		self.client.force_authenticate(user=admin)

		response = self.client.post(
			"/api/billing/plans/",
			{
				"code": "formule-pro",
				"nom": "Formule Pro",
				"description": "Suivi de la consommation",
				"prix_mensuel": "29000",
				"devise": "XOF",
				"limites": {"utilisateurs": 25},
				"fonctionnalites": {"analytics_avances": True},
				"actif": True,
			},
			format="json",
		)

		self.assertEqual(response.status_code, status.HTTP_201_CREATED)
		self.assertEqual(response.data["nom"], "Formule Pro")


class PaymentReturnConfirmationTests(APITestCase):
	def setUp(self):
		self.utilisateur = Utilisateur.objects.create_user(
			email="retour-paiement@example.com",
			nom="Admin paiement",
			role=Utilisateur.Role.ADMIN_ORGANISATION,
			actif=True,
		)
		self.organisation = Organisation.objects.create(
			nom="Organisation retour paiement",
			secteur="Énergie",
			localisation="Dakar",
			statut=Organisation.Statut.ACTIVE,
		)
		UtilisateurOrganisation.objects.create(
			organisation=self.organisation,
			utilisateur=self.utilisateur,
		)
		self.plan = Plan.objects.create(
			code="retour-paiement",
			nom="Retour paiement",
			prix_mensuel="15000",
		)
		self.abonnement = Abonnement.objects.create(
			organisation=self.organisation,
			plan=self.plan,
			statut=Abonnement.Statut.EXPIRED,
			periodicite="MENSUEL",
			debut=timezone.now(),
			fin_periode=timezone.now(),
			fournisseur=Abonnement.Fournisseur.PAYDUNYA,
		)
		self.facture = InvoiceService().generer_facture_cycle(self.abonnement)
		self.facture.external_invoice_id = "test_invoice_return_token"
		self.facture.save(update_fields=("external_invoice_id",))
		self.client.force_authenticate(user=self.utilisateur)

	def test_retour_paydunya_confirme_et_active_l_abonnement(self):
		with patch("billing.views.obtenir_provider") as obtenir_provider:
			obtenir_provider.return_value.recuperer_transaction.return_value = {
				"external_event_id": "test_invoice_return_token",
				"external_invoice_id": "test_invoice_return_token",
				"facture_id": str(self.facture.id),
				"statut": "SUCCEEDED",
				"montant": Decimal("15000"),
				"devise": "XOF",
				"methode": "",
				"external_payment_id": "test_payment_return_token",
			}

			response = self.client.post(
				"/api/billing/abonnements/confirmer-retour/",
				{"token": "test_invoice_return_token"},
				format="json",
			)

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["status"], "confirmed")
		self.abonnement.refresh_from_db()
		self.facture.refresh_from_db()
		self.assertEqual(self.abonnement.statut, Abonnement.Statut.ACTIVE)
		self.assertEqual(self.facture.statut, Facture.Statut.PAID)

	def test_retour_paydunya_en_attente_ne_donne_pas_acces(self):
		with patch("billing.views.obtenir_provider") as obtenir_provider:
			obtenir_provider.return_value.recuperer_transaction.return_value = {
				"external_event_id": "test_invoice_return_token",
				"external_invoice_id": "test_invoice_return_token",
				"facture_id": str(self.facture.id),
				"statut": "PENDING",
				"montant": Decimal("15000"),
				"devise": "XOF",
				"methode": "",
				"external_payment_id": "test_payment_return_token",
			}

			response = self.client.post(
				"/api/billing/abonnements/confirmer-retour/",
				{"token": "test_invoice_return_token"},
				format="json",
			)

		self.assertEqual(response.status_code, status.HTTP_200_OK)
		self.assertEqual(response.data["status"], "pending")
		self.abonnement.refresh_from_db()
		self.facture.refresh_from_db()
		self.assertEqual(self.abonnement.statut, Abonnement.Statut.EXPIRED)
		self.assertEqual(self.facture.statut, Facture.Statut.OPEN)
