import os
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.models import PreferencesUtilisateur, Utilisateur
from organizations.models import Organisation, UtilisateurOrganisation


PASSWORD_ENV = {
    "ECOSCAN_DEMO_CONTROL_PASSWORD": "control-password-for-demo-only-2026",
    "ECOSCAN_DEMO_MULTISITE_PASSWORD": "multisite-password-for-demo-only-2026",
    "ECOSCAN_DEMO_ANOMALIES_PASSWORD": "anomalies-password-for-demo-only-2026",
    "ECOSCAN_DEMO_LECTURE_PASSWORD": "lecture-password-for-demo-only-2026",
}

PILOT_EMAILS = (
    "mobile.commande.test@ecoscan.invalid",
    "pilote.multisite@ecoscan.invalid",
    "pilote.anomalies@ecoscan.invalid",
)


class DemoAccountProvisioningTests(TestCase):
    def setUp(self):
        for index, email in enumerate(PILOT_EMAILS, start=1):
            user = Utilisateur.objects.create_user(
                email=email,
                password="initial-demo-password",
                nom=f"Pilote {index}",
                role=Utilisateur.Role.ADMIN_ORGANISATION,
            )
            organisation = Organisation.objects.create(
                nom=f"Organisation démo {index}",
                secteur="Services",
                localisation="Sénégal",
                statut=Organisation.Statut.ACTIVE,
            )
            UtilisateurOrganisation.objects.create(
                organisation=organisation,
                utilisateur=user,
            )

    @patch.dict(os.environ, PASSWORD_ENV, clear=True)
    def test_provisions_accounts_and_links_reader_to_all_demo_organizations(self):
        call_command("provisionner_comptes_demo")

        reader = Utilisateur.objects.get(email="demo.lecture@demo.ecoscan.test")
        self.assertEqual(reader.role, Utilisateur.Role.CONSULTANT)
        self.assertFalse(reader.is_staff)
        self.assertFalse(reader.is_superuser)
        self.assertEqual(
            UtilisateurOrganisation.objects.filter(utilisateur=reader).count(),
            3,
        )
        self.assertTrue(PreferencesUtilisateur.objects.filter(utilisateur=reader).exists())

        for email, password in (
            (PILOT_EMAILS[0], PASSWORD_ENV["ECOSCAN_DEMO_CONTROL_PASSWORD"]),
            (PILOT_EMAILS[1], PASSWORD_ENV["ECOSCAN_DEMO_MULTISITE_PASSWORD"]),
            (PILOT_EMAILS[2], PASSWORD_ENV["ECOSCAN_DEMO_ANOMALIES_PASSWORD"]),
            (
                "demo.lecture@demo.ecoscan.test",
                PASSWORD_ENV["ECOSCAN_DEMO_LECTURE_PASSWORD"],
            ),
        ):
            user = Utilisateur.objects.get(email=email)
            self.assertTrue(user.check_password(password))
            self.assertEqual(user.password, user.mot_de_passe_hash)

    @patch.dict(os.environ, {}, clear=True)
    def test_rejects_missing_passwords_before_creating_reader(self):
        with self.assertRaises(CommandError):
            call_command("provisionner_comptes_demo")

        self.assertFalse(
            Utilisateur.objects.filter(email="demo.lecture@demo.ecoscan.test").exists()
        )
