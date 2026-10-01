import re
from urllib.parse import parse_qs, urlsplit
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from django.test import override_settings
from django.core.cache import cache
from unittest.mock import patch
from audit.models import JournalAudit
from .models import Utilisateur
from organizations.models import Organisation, UtilisateurOrganisation
from billing.models import Abonnement, Plan


class AccountsModuleTests(APITestCase):
    """Suite de tests unitaires pour valider la sécurité et les parcours d'authentification."""

    def setUp(self):
        cache.clear()
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
        UtilisateurOrganisation.objects.create(
            organisation=self.organisation,
            utilisateur=self.consultant,
        )

        self.onboarding_url = "/api/onboarding/"
        self.activation_url = "/api/activation/"
        self.membres_list_url = "/api/membres/"

    def obtenir_headers_jwt(self, user):
        refresh = RefreshToken.for_user(user)
        return {"HTTP_AUTHORIZATION": f"Bearer {refresh.access_token}"}

    @patch("accounts.views.envoyer_email_activation")
    def test_onboarding_cree_utilisateur_inactif_et_envoie_le_lien(self, envoyer_email):
        plan = Plan.objects.create(code="onboarding-plan", nom="Plan onboarding", prix_mensuel="10000")
        data = {
            "nom_admin": "Nouvel Admin",
            "email_connexion": "onboarding.ecoscan@gmail.com",
            "nom_organisation": "Entreprise en attente",
            "secteur": "Énergie",
            "localisation": "Dakar, Sénégal",
            "details_demande": {
                "plan_id": str(plan.pk),
                "profil": "pme",
                "objectifs": ["Réduire les coûts"],
            },
        }
        response = self.client.post(self.onboarding_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["duree_essai_jours"], 14)
        envoyer_email.assert_called_once()
        nouvel_user = Utilisateur.objects.get(email="onboarding.ecoscan@gmail.com")
        self.assertFalse(nouvel_user.actif)
        self.assertEqual(nouvel_user.role, Utilisateur.Role.ADMIN_ORGANISATION)
        organisation = Organisation.objects.get(nom="Entreprise en attente")
        self.assertEqual(organisation.statut, Organisation.Statut.EN_ATTENTE)
        self.assertEqual(organisation.secteur, "Énergie")
        self.assertEqual(organisation.details_demande["profil"], "pme")
        self.assertTrue(organisation.details_demande["inscription_autonome"])
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

    @patch("accounts.views.envoyer_email_activation")
    def test_activation_demarre_essai_sur_la_formule_choisie(self, envoyer_email):
        plan = Plan.objects.create(
            code="formule-essai",
            nom="Formule Essai",
            prix_mensuel="29000",
        )
        response = self.client.post(
            self.onboarding_url,
            {
                "nom_admin": "Nouvel Admin",
                "email_connexion": "essai.ecoscan@gmail.com",
                "nom_organisation": "Entreprise essai autonome",
                "secteur": "Énergie",
                "localisation": "Dakar",
                "details_demande": {"plan_id": str(plan.pk)},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        utilisateur = Utilisateur.objects.get(email="essai.ecoscan@gmail.com")
        organisation = Organisation.objects.get(nom="Entreprise essai autonome")
        self.assertFalse(Abonnement.objects.filter(organisation=organisation).exists())

        uid = urlsafe_base64_encode(force_bytes(utilisateur.pk))
        token = default_token_generator.make_token(utilisateur)
        activation = self.client.post(
            self.activation_url,
            {
                "uid": uid,
                "token": token,
                "mot_de_passe": "Securise123",
                "mot_de_passe_confirmation": "Securise123",
            },
            format="json",
        )

        self.assertEqual(activation.status_code, status.HTTP_200_OK)
        self.assertEqual(activation.data["duree_essai_jours"], 14)
        organisation.refresh_from_db()
        abonnement = Abonnement.objects.get(organisation=organisation)
        self.assertEqual(organisation.statut, Organisation.Statut.ACTIVE)
        self.assertEqual(abonnement.plan, plan)
        self.assertEqual(abonnement.statut, Abonnement.Statut.TRIALING)
        self.assertEqual(
            int((abonnement.fin_periode - abonnement.debut).total_seconds()),
            14 * 24 * 60 * 60,
        )
        nouvelle_activation = self.client.post(
            self.activation_url,
            {
                "uid": uid,
                "token": token,
                "mot_de_passe": "Securise123",
                "mot_de_passe_confirmation": "Securise123",
            },
            format="json",
        )
        self.assertEqual(nouvelle_activation.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Abonnement.objects.filter(organisation=organisation).count(), 1)

    @patch("accounts.views.envoyer_email_activation", side_effect=RuntimeError("Email indisponible"))
    def test_onboarding_annule_le_compte_si_le_lien_ne_peut_pas_etre_envoye(self, envoyer_email):
        plan = Plan.objects.create(code="plan-email", nom="Plan email", prix_mensuel="10000")
        response = self.client.post(
            self.onboarding_url,
            {
                "nom_admin": "Nouvel Admin",
                "email_connexion": "sans.email.ecoscan@gmail.com",
                "nom_organisation": "Entreprise sans activation",
                "secteur": "Énergie",
                "localisation": "Dakar",
                "details_demande": {"plan_id": str(plan.pk)},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertFalse(Utilisateur.objects.filter(email="sans.email.ecoscan@gmail.com").exists())
        self.assertFalse(Organisation.objects.filter(nom="Entreprise sans activation").exists())
        envoyer_email.assert_called_once()

    def test_onboarding_refuse_format_email_invalide(self):
        data = {
            "nom_admin": "Test",
            "email_connexion": "format_incorrect",
            "details_demande": {"plan_id": "00000000-0000-0000-0000-000000000000"},
        }
        response = self.client.post(self.onboarding_url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_onboarding_refuse_creation_sans_formule_choisie(self):
        response = self.client.post(
            self.onboarding_url,
            {
                "nom_admin": "Nouvel Admin",
                "email_connexion": "sans.plan.ecoscan@gmail.com",
                "nom_organisation": "Entreprise sans formule",
                "secteur": "Énergie",
                "localisation": "Dakar",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Utilisateur.objects.filter(email="sans.plan.ecoscan@gmail.com").exists())

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

    def test_admin_organisation_peut_suspendre_acces_et_action_est_auditee(self):
        self.client.credentials(**self.obtenir_headers_jwt(self.admin_org))

        response = self.client.patch(
            f"{self.membres_list_url}{self.consultant.pk}/acces/",
            {"actif": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["actif"])
        evenement = JournalAudit.objects.get(action="DESACTIVER_ACCES")
        self.assertEqual(evenement.utilisateur, self.admin_org)
        self.assertEqual(evenement.organisation, self.organisation)
        self.assertEqual(evenement.identifiant_ressource, str(self.consultant.pk))

    def test_un_admin_ne_peut_pas_gerer_un_membre_hors_de_son_organisation(self):
        autre_admin = Utilisateur.objects.create_user(
            email="autre.admin@gmail.com",
            nom="Autre admin",
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=True,
        )
        autre_organisation = Organisation.objects.create(
            nom="Autre organisation",
            secteur="BTP",
            localisation="Thiès",
            statut=Organisation.Statut.ACTIVE,
        )
        UtilisateurOrganisation.objects.create(
            organisation=autre_organisation,
            utilisateur=autre_admin,
        )
        membre_autre_org = Utilisateur.objects.create_user(
            email="membre.autre@gmail.com",
            nom="Membre autre organisation",
            role=Utilisateur.Role.UTILISATEUR_ORGANISATION,
            actif=True,
        )
        UtilisateurOrganisation.objects.create(
            organisation=autre_organisation,
            utilisateur=membre_autre_org,
        )
        self.client.credentials(**self.obtenir_headers_jwt(self.admin_org))

        response = self.client.delete(f"{self.membres_list_url}{membre_autre_org.pk}/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Utilisateur.objects.filter(pk=membre_autre_org.pk).exists())

    def test_suppression_membre_est_auditee_et_ne_peut_pas_supprimer_son_propre_compte(self):
        self.client.credentials(**self.obtenir_headers_jwt(self.admin_org))

        reponse_suppression_propre = self.client.delete(
            f"{self.membres_list_url}{self.admin_org.pk}/"
        )
        self.assertEqual(reponse_suppression_propre.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Utilisateur.objects.filter(pk=self.admin_org.pk).exists())

        response = self.client.delete(f"{self.membres_list_url}{self.consultant.pk}/")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        evenement = JournalAudit.objects.get(action="SUPPRIMER_UTILISATEUR")
        self.assertEqual(evenement.utilisateur, self.admin_org)
        self.assertEqual(evenement.organisation, self.organisation)
        self.assertEqual(evenement.details["utilisateur_email"], "consultant@gmail.com")

    def test_super_admin_peut_desactiver_et_supprimer_un_pair(self):
        pair = Utilisateur.objects.create_user(
            email="autre.dev@gmail.com",
            nom="Autre équipe EcoScan",
            role=Utilisateur.Role.SUPER_ADMIN,
            actif=True,
        )
        self.client.credentials(**self.obtenir_headers_jwt(self.dev_user))

        reponse_acces = self.client.patch(
            f"{self.membres_list_url}{pair.pk}/acces/",
            {"actif": False},
            format="json",
        )
        self.assertEqual(reponse_acces.status_code, status.HTTP_200_OK)
        self.assertFalse(reponse_acces.data["actif"])

        response = self.client.delete(f"{self.membres_list_url}{pair.pk}/")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Utilisateur.objects.filter(pk=pair.pk).exists())

    def test_patch_profil_ne_permet_pas_de_modifier_directement_acces(self):
        self.client.credentials(**self.obtenir_headers_jwt(self.admin_org))

        response = self.client.patch(
            f"{self.membres_list_url}{self.consultant.pk}/",
            {"actif": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["actif"])

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
            "http://localhost:3001/?uid=",
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
