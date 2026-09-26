import re
from urllib.parse import parse_qs, urlsplit
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import override_settings
from unittest.mock import patch
from .models import Utilisateur
from organizations.models import Organisation, UtilisateurOrganisation
from billing.models import Abonnement, Plan


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
        self.organisation = Organisation.objects.create(
            nom="Organisation de test",
            secteur="Énergie",
            localisation="Dakar",
            statut=Organisation.Statut.ACTIVE,
        )
        UtilisateurOrganisation.objects.create(
            organisation=self.organisation,
            utilisateur=self.admin_org,
        )

        self.onboarding_url = "/api/onboarding/"
        self.activation_url = "/api/activation/"
        self.membres_list_url = "/api/membres/"

    def obtenir_headers_jwt(self, user):
        refresh = RefreshToken.for_user(user)
        return {"HTTP_AUTHORIZATION": f"Bearer {refresh.access_token}"}

    def test_onboarding_cree_utilisateur_inactif(self):
        data = {
            "nom_admin": "Nouvel Admin",
            "email_connexion": "onboarding.ecoscan@gmail.com",
            "nom_organisation": "Entreprise en attente",
            "secteur": "Énergie",
            "localisation": "Dakar, Sénégal",
            "details_demande": {
                "profil": "pme",
                "objectifs": ["Réduire les coûts"],
            },
        }
        response = self.client.post(self.onboarding_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        nouvel_user = Utilisateur.objects.get(email="onboarding.ecoscan@gmail.com")
        self.assertFalse(nouvel_user.actif)
        self.assertEqual(nouvel_user.role, Utilisateur.Role.ADMIN_ORGANISATION)
        organisation = Organisation.objects.get(nom="Entreprise en attente")
        self.assertEqual(organisation.statut, Organisation.Statut.EN_ATTENTE)
        self.assertEqual(organisation.secteur, "Énergie")
        self.assertEqual(organisation.details_demande["profil"], "pme")
        self.assertTrue(
            UtilisateurOrganisation.objects.filter(
                organisation=organisation,
                utilisateur=nouvel_user,
            ).exists()
        )
        self.client.credentials(**self.obtenir_headers_jwt(self.dev_user))
        demandes = self.client.get("/api/organisations/structures/")
        self.assertEqual(demandes.status_code, status.HTTP_200_OK)
        demande_visible = next(
            item for item in demandes.data["results"] if item["id"] == str(organisation.pk)
        )
        self.assertEqual(demande_visible["statut"], Organisation.Statut.EN_ATTENTE)
        self.assertEqual(demande_visible["emails_admin"], ["onboarding.ecoscan@gmail.com"])

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

        data = {
            "uid": uid,
            "token": token,
            "mot_de_passe": "Securise123",
            "mot_de_passe_confirmation": "Securise123",
        }
        response = self.client.post(self.activation_url, data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.admin_org.refresh_from_db()
        self.assertTrue(self.admin_org.actif)
        self.assertTrue(self.admin_org.check_password("Securise123"))

    def test_activation_refuse_confirmation_mot_de_passe_differente(self):
        self.admin_org.actif = False
        self.admin_org.set_unusable_password()
        self.admin_org.save()

        uid = urlsafe_base64_encode(force_bytes(self.admin_org.pk))
        token = default_token_generator.make_token(self.admin_org)

        data = {
            "uid": uid,
            "token": token,
            "mot_de_passe": "Securise123",
            "mot_de_passe_confirmation": "AutreMot123",
        }
        response = self.client.post(self.activation_url, data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.admin_org.refresh_from_db()
        self.assertFalse(self.admin_org.actif)

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

    @patch.dict(
        "os.environ",
        {"EMAIL_PROVIDER": "brevo", "BREVO_API_KEY": "test-key", "BREVO_SENDER_EMAIL": "sender@example.com"},
    )
    @patch("accounts.services.httpx.post")
    def test_admin_organisation_peut_inviter_un_consultant(self, brevo_post):
        headers = self.obtenir_headers_jwt(self.admin_org)
        data = {
            "nom": "Nouveau Externe",
            "email": "invite.ecoscan@gmail.com",
            "role": Utilisateur.Role.CONSULTANT
        }
        
        response = self.client.post(self.membres_list_url, data, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["activation_email_sent"])
        brevo_post.assert_called_once()
        self.assertEqual(
            brevo_post.call_args.kwargs["json"]["subject"],
            "Invitation à rejoindre la plateforme",
        )
        self.assertIn(
            "http://localhost:3000/?uid=",
            brevo_post.call_args.kwargs["json"]["textContent"],
        )

    @patch.dict(
        "os.environ",
        {"EMAIL_PROVIDER": "brevo", "BREVO_API_KEY": "test-key", "BREVO_SENDER_EMAIL": "sender@example.com"},
    )
    @patch("accounts.services.httpx.post")
    def test_super_admin_peut_inviter_un_autre_super_admin(self, brevo_post):
        headers = self.obtenir_headers_jwt(self.dev_user)
        response = self.client.post(
            self.membres_list_url,
            {
                "nom": "Nouveau Super Admin",
                "email": "nouveau.superadmin@gmail.com",
                "role": Utilisateur.Role.SUPER_ADMIN,
            },
            format="json",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["activation_email_sent"])
        self.assertFalse(Utilisateur.objects.get(email="nouveau.superadmin@gmail.com").actif)
        brevo_post.assert_called_once()

    @patch.dict("os.environ", {"EMAIL_PROVIDER": "smtp"})
    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="smtp.example.com",
        DEFAULT_FROM_EMAIL="EcoScan <noreply@ecoscan.example>",
    )
    @patch("accounts.services.send_mail")
    def test_super_admin_peut_inviter_par_smtp(self, smtp_send_mail):
        headers = self.obtenir_headers_jwt(self.dev_user)
        response = self.client.post(
            self.membres_list_url,
            {
                "nom": "Membre SMTP",
                "email": "membre.smtp@gmail.com",
                "role": Utilisateur.Role.SUPER_ADMIN,
            },
            format="json",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["activation_email_sent"])
        smtp_send_mail.assert_called_once()
        self.assertEqual(smtp_send_mail.call_args.args[0], "Invitation à rejoindre la plateforme")
        self.assertEqual(smtp_send_mail.call_args.args[3], ["membre.smtp@gmail.com"])

    @patch("accounts.serializers.envoyer_email_activation", side_effect=RuntimeError("Brevo indisponible"))
    def test_invitation_signale_si_le_mail_na_pas_ete_envoye(self, envoyer_email):
        headers = self.obtenir_headers_jwt(self.dev_user)
        response = self.client.post(
            self.membres_list_url,
            {
                "nom": "Admin sans email",
                "email": "admin.sans.email@gmail.com",
                "role": Utilisateur.Role.SUPER_ADMIN,
            },
            format="json",
            **headers,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data["activation_email_sent"])
        self.assertFalse(Utilisateur.objects.get(email="admin.sans.email@gmail.com").actif)
        envoyer_email.assert_called_once()

    @patch.dict(
        "os.environ",
        {"EMAIL_PROVIDER": "brevo", "BREVO_API_KEY": "test-key", "BREVO_SENDER_EMAIL": "sender@example.com"},
    )
    @patch("accounts.services.httpx.post")
    def test_super_admin_approbation_active_organisation_et_envoie_email_brevo(self, brevo_post):
        brevo_post.return_value.raise_for_status.return_value = None
        plan = Plan.objects.create(
            code="pme-annuel",
            nom="Formule PME annuelle",
            prix_mensuel="10000",
        )
        demandeur = Utilisateur.objects.create_user(
            email="demandeur@example.com",
            nom="Demandeur",
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=False,
        )
        demandeur.set_unusable_password()
        demandeur.save()
        organisation = Organisation.objects.create(
            nom="Demande à approuver",
            secteur="Industrie",
            localisation="Dakar",
            statut=Organisation.Statut.EN_ATTENTE,
            details_demande={"plan_id": str(plan.pk)},
        )
        UtilisateurOrganisation.objects.create(
            organisation=organisation,
            utilisateur=demandeur,
        )

        self.client.credentials(**self.obtenir_headers_jwt(self.dev_user))
        response = self.client.patch(
            f"/api/organisations/structures/{organisation.pk}/",
            {"statut": "ACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["activation_email_sent"])
        organisation.refresh_from_db()
        self.assertEqual(organisation.statut, Organisation.Statut.ACTIVE)
        self.assertEqual(
            Abonnement.objects.get(organisation=organisation).plan,
            plan,
        )
        brevo_post.assert_called_once()
        self.assertEqual(
            brevo_post.call_args.kwargs["json"]["subject"],
            "Votre demande EcoScan est approuvée",
        )
        activation_url = brevo_post.call_args.kwargs["json"]["textContent"].split(
            "Définissez votre mot de passe avec ce lien sécurisé :\n",
            1,
        )[1].splitlines()[0]
        activation_params = parse_qs(urlsplit(activation_url).query)
        activation_response = self.client.post(
            self.activation_url,
            {
                "uid": activation_params["uid"][0],
                "token": activation_params["token"][0],
                "mot_de_passe": "Securise123",
                "mot_de_passe_confirmation": "Securise123",
            },
            format="json",
        )
        self.assertEqual(activation_response.status_code, status.HTTP_200_OK)
        demandeur.refresh_from_db()
        self.assertTrue(demandeur.actif)
        self.assertTrue(demandeur.check_password("Securise123"))

    @patch.dict("os.environ", {"BREVO_API_KEY": "", "BREVO_SENDER_EMAIL": ""})
    def test_echec_brevo_laisse_la_demande_en_attente(self):
        Plan.objects.create(
            code="standard",
            nom="Standard",
            prix_mensuel="10000",
        )
        demandeur = Utilisateur.objects.create_user(
            email="demandeur-brevo@example.com",
            nom="Demandeur",
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=False,
        )
        demandeur.set_unusable_password()
        demandeur.save()
        organisation = Organisation.objects.create(
            nom="Demande sans configuration Brevo",
            secteur="Industrie",
            localisation="Dakar",
            statut=Organisation.Statut.EN_ATTENTE,
        )
        UtilisateurOrganisation.objects.create(
            organisation=organisation,
            utilisateur=demandeur,
        )
        self.client.credentials(**self.obtenir_headers_jwt(self.dev_user))

        response = self.client.patch(
            f"/api/organisations/structures/{organisation.pk}/",
            {"statut": "ACTIVE"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        organisation.refresh_from_db()
        self.assertEqual(organisation.statut, Organisation.Statut.EN_ATTENTE)
        self.assertFalse(Abonnement.objects.filter(organisation=organisation).exists())

    def test_admin_organisation_ne_peut_pas_creer_de_dev(self):
        headers = self.obtenir_headers_jwt(self.admin_org)
        data = {
            "nom": "Tentative Pirate",
            "email": "hacker.ecoscan@gmail.com",
            "role": Utilisateur.Role.SUPER_ADMIN
        }
        response = self.client.post(self.membres_list_url, data, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
