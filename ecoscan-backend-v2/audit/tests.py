from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Utilisateur
from organizations.models import Organisation, UtilisateurOrganisation
from .models import JournalAudit


class JournalAuditApiTests(TestCase):
    def setUp(self):
        self.admin = Utilisateur.objects.create_user(
            email="audit-admin@example.com",
            password="test-password",
            nom="Administratrice EcoScan",
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=True,
        )
        self.organisation = Organisation.objects.create(
            nom="Organisation Audit",
            secteur="Énergie",
            localisation="Dakar",
        )
        UtilisateurOrganisation.objects.create(
            organisation=self.organisation,
            utilisateur=self.admin,
        )
        self.evenement = JournalAudit.objects.create(
            utilisateur=self.admin,
            organisation=self.organisation,
            action="DESACTIVER_ACCES",
            ressource="Utilisateur",
            identifiant_ressource="cible-123",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_journal_expose_nom_email_organisation_et_date_action(self):
        response = self.client.get("/api/audits/logs/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        log = response.data["results"][0]
        self.assertEqual(log["id"], str(self.evenement.pk))
        self.assertEqual(log["utilisateur_nom"], "Administratrice EcoScan")
        self.assertEqual(log["utilisateur_email"], "audit-admin@example.com")
        self.assertEqual(log["organisation_nom"], "Organisation Audit")
        self.assertEqual(log["date_action"], self.evenement.date_action.isoformat().replace("+00:00", "Z"))
