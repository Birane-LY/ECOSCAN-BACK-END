import re
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from .models import Utilisateur


class AccountsModuleTests(APITestCase):
    """Suite de tests unitaires pour valider la sécurité et les parcours d'authentification."""

    def setUp(self):
        self.dev_user = Utilisateur.objects.create_user(
            email="dev@gmail.com",
            nom="Équipe Dev",
            role=Utilisateur.Role.SUPER_ADMIN,
            actif=True
        )
        self.admin_org = Utilisateur.objects.create_user(
            email="admin@gmail.com",
            nom="Admin Org",
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=True
        )
        self.consultant = Utilisateur.objects.create_user(
            email="consultant@gmail.com",
            nom="Consultant Externe",
            role=Utilisateur.Role.CONSULTANT,
            actif=True
        )

        self.onboarding_url = "/api/onboarding/"
        self.activation_url = "/api/activation/"
        self.membres_list_url = "/api/membres/"

    def obtenir_headers_jwt(self, user):
        refresh = RefreshToken.for_user(user)
        return {"HTTP_AUTHORIZATION": f"Bearer {refresh.access_token}"}

    def test_onboarding_cree_utilisateur_inactif(self):
        data = {"nom_admin": "Nouvel Admin", "email_connexion": "onboarding.ecoscan@gmail.com"}
        response = self.client.post(self.onboarding_url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        nouvel_user = Utilisateur.objects.get(email="onboarding.ecoscan@gmail.com")
        self.assertFalse(nouvel_user.actif)
        self.assertEqual(nouvel_user.role, Utilisateur.Role.ADMIN_ORGANISATION)

    def test_onboarding_refuse_format_email_invalide(self):
        data = {"nom_admin": "Test", "email_connexion": "format_incorrect"}
        response = self.client.post(self.onboarding_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_activation_compte_reussite(self):
        self.admin_org.actif = False
        self.admin_org.set_unusable_password()
        self.admin_org.save()

        uid = urlsafe_base64_encode(force_bytes(self.admin_org.pk))
        token = default_token_generator.make_token(self.admin_org)

        data = {"uid": uid, "token": token, "mot_de_passe": "Securise123"}
        response = self.client.post(self.activation_url, data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.admin_org.refresh_from_db()
        self.assertTrue(self.admin_org.actif)
        self.assertTrue(self.admin_org.check_password("Securise123"))

    def test_activation_refuse_mot_de_passe_sans_chiffre(self):
        uid = urlsafe_base64_encode(force_bytes(self.admin_org.pk))
        token = default_token_generator.make_token(self.admin_org)

        data = {"uid": uid, "token": token, "mot_de_passe": "seulementdeslettres"}
        response = self.client.post(self.activation_url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_organisation_ne_voit_pas_les_devs(self):
        headers = self.obtenir_headers_jwt(self.admin_org)
        response = self.client.get(self.membres_list_url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails_recuperee = [u["email"] for u in response.data["results"]]
        
        self.assertIn("consultant@gmail.com", emails_recuperee)
        self.assertNotIn("dev@gmail.com", emails_recuperee)

    def test_consultant_ne_peut_pas_lister_les_membres(self):
        headers = self.obtenir_headers_jwt(self.consultant)
        response = self.client.get(self.membres_list_url, **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_utilisateur_peut_modifier_ses_propres_infos(self):
        self.client.credentials(**self.obtenir_headers_jwt(self.consultant))
        detail_url = f"{self.membres_list_url}{self.consultant.pk}/"
        
        data = {"nom": "Consultant Mis à Jour"}
        response = self.client.patch(detail_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.consultant.refresh_from_db()
        self.assertEqual(self.consultant.nom, "Consultant Mis à Jour")

    def test_admin_organisation_peut_inviter_un_consultant(self):
        headers = self.obtenir_headers_jwt(self.admin_org)
        data = {
            "nom": "Nouveau Externe",
            "email": "invite.ecoscan@gmail.com",
            "role": Utilisateur.Role.CONSULTANT
        }
        
        response = self.client.post(self.membres_list_url, data, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Invitation à rejoindre la plateforme", mail.outbox[0].subject)

    def test_admin_organisation_ne_peut_pas_creer_de_dev(self):
        headers = self.obtenir_headers_jwt(self.admin_org)
        data = {
            "nom": "Tentative Pirate",
            "email": "hacker.ecoscan@gmail.com",
            "role": Utilisateur.Role.SUPER_ADMIN
        }
        response = self.client.post(self.membres_list_url, data, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
