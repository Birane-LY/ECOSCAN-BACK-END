from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase
from accounts.models import Utilisateur

from organizations.models import Organisation

from .models import Abonnement, Plan


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
		self.assertEqual(abonnement.fournisseur, Abonnement.Fournisseur.MANUAL)


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
