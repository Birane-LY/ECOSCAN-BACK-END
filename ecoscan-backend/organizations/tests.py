from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from .models import Organisation, UtilisateurOrganisation, Site, FicheProjet, Compteur, Activite

Utilisateur = get_user_model()


class OrganisationModuleTests(APITestCase):
    """Suite de tests unitaires pour valider le cloisonnement multi-tenant et la gestion SaaS."""

    def setUp(self):
        """Initialise l'environnement technique, les organisations et les différents acteurs."""
        self.super_admin = Utilisateur.objects.create_user(
            email="dev@gmail.com", nom="Équipe Dev", role="SUPER_ADMIN", actif=True
        )
        self.user_org_a = Utilisateur.objects.create_user(
            email="admin.a@gmail.com", nom="Responsable A", role="ADMIN_ORGANISATION", actif=True
        )
        self.user_org_b = Utilisateur.objects.create_user(
            email="admin.b@gmail.com", nom="Responsable B", role="ADMIN_ORGANISATION", actif=True
        )

        self.org_a = Organisation.objects.create(
            nom="Organisation A", secteur="Énergie", localisation="Dakar", statut="ACTIVE"
        )
        self.org_b = Organisation.objects.create(
            nom="Organisation B", secteur="BTP", localisation="Thiès", statut="ACTIVE"
        )

        # Liaison pour le super_admin et les admins d'organisation
        UtilisateurOrganisation.objects.create(organisation=self.org_a, utilisateur=self.super_admin)
        UtilisateurOrganisation.objects.create(organisation=self.org_a, utilisateur=self.user_org_a)
        UtilisateurOrganisation.objects.create(organisation=self.org_b, utilisateur=self.user_org_b)

        self.site_a = Site.objects.create(
            organisation=self.org_a, nom="Usine Dakar", adresse="Zone Indu", pays="Sénégal", fuseau_horaire="GMT"
        )
        self.projet_a = FicheProjet.objects.create(
            organisation=self.org_a, nom="Audit Énergétique 2026", statut="BROUILLON"
        )
        self.activite_a = Activite.objects.create(
            fiche_projet=self.projet_a, nom="Mesure Électrique", categorie="Audit", statut="EN_COURS"
        )
        self.compteur_a = Compteur.objects.create(
            site=self.site_a, reference="COMPT-9988", type_energie="Électricité", unite="kWh", statut_synchronisation="OK"
        )

        self.structures_list_url = "/api/organisations/structures/"
        self.sites_list_url = "/api/organisations/sites/"
        self.projets_list_url = "/api/organisations/projets/"

    def obtenir_headers_jwt(self, user):
        """Génère un en-tête d'authentification JWT pour simuler une session."""
        refresh = RefreshToken.for_user(user)
        return {"HTTP_AUTHORIZATION": f"Bearer {refresh.access_token}"}

    def test_utilisateur_ne_voit_que_les_sites_de_son_organisation(self):
        """L'API doit appliquer l'aveuglement par défaut sur la liste des sites physiques."""
        headers = self.obtenir_headers_jwt(self.user_org_a)
        response = self.client.get(self.sites_list_url, **headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["nom"], "Usine Dakar")

        headers_b = self.obtenir_headers_jwt(self.user_org_b)
        response_b = self.client.get(self.sites_list_url, **headers_b)
        self.assertEqual(len(response_b.data["results"]), 0)

    def test_interdiction_acceder_unitairement_au_projet_dun_tiers(self):
        """Une tentative d'accès par URL (UUID) au projet d'une autre entreprise doit être bloquée."""
        headers = self.obtenir_headers_jwt(self.user_org_b)
        detail_url = f"{self.projets_list_url}{self.projet_a.id}/"
        
        response = self.client.get(detail_url, **headers)
        self.assertIn(response.status_code, [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN])

    def test_super_admin_aveugle_par_defaut_sur_les_projets_clients(self):
        """Le Super Admin (Dev) ne doit pas voir l'espace projet des clients s'il n'y est pas affilié."""
        UtilisateurOrganisation.objects.filter(utilisateur=self.super_admin).delete()
        
        headers = self.obtenir_headers_jwt(self.super_admin)
        response = self.client.get(self.projets_list_url, **headers)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 0)

    def test_super_admin_ne_peut_pas_modifier_les_textes_de_lorganisation(self):
        """Le rôle technique ne peut pas modifier la raison sociale ou les données d'un client."""
        headers = self.obtenir_headers_jwt(self.super_admin)
        detail_url = f"{self.structures_list_url}{self.org_a.id}/"
        
        data = {"nom": "Nom Piraté par le Dev", "secteur": "Hacking"}
        response = self.client.put(detail_url, data, format="json", **headers)
        
        # L'API accepte la requête (200 OK) mais ignore silencieusement les champs non autorisés
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.org_a.refresh_from_db()
        # On vérifie mathématiquement que les textes privés du client n'ont PAS bougé en base
        self.assertEqual(self.org_a.nom, "Organisation A")
        self.assertEqual(self.org_a.secteur, "Énergie")

    def test_blocage_suspension_sans_defaut_de_paiement(self):
        """L'API doit refuse la suspension d'un client dont les paiements sont à jour."""
        headers = self.obtenir_headers_jwt(self.super_admin)
        detail_url = f"{self.structures_list_url}{self.org_a.id}/"
        
        data = {"statut": "SUSPENDUE"}
        response = self.client.patch(detail_url, data, format="json", **headers)
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.statut, "ACTIVE")

    def test_simulation_workflow_n8n_suspension_pour_impaye(self):
        """n8n doit pouvoir injecter le défaut de paiement et couper les accès de la structure."""
        headers = self.obtenir_headers_jwt(self.super_admin)
        detail_url = f"{self.structures_list_url}{self.org_a.id}/"
        
        data = {"defaut_paiement": True, "statut": "SUSPENDUE"}
        response = self.client.patch(detail_url, data, format="json", **headers)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.org_a.refresh_from_db()
        self.assertTrue(self.org_a.defaut_paiement)
        self.assertEqual(self.org_a.statut, "SUSPENDUE")

    def test_interdiction_formelle_de_supprimer_une_organisation(self):
        """La suppression physique d'un espace client est bannie pour préserver l'historique."""
        headers = self.obtenir_headers_jwt(self.super_admin)
        detail_url = f"{self.structures_list_url}{self.org_a.id}/"
        
        response = self.client.delete(detail_url, **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Organisation.objects.filter(id=self.org_a.id).exists())

    def test_validation_empeche_liaison_compteur_activite_inter_entreprises(self):
        """Un sérialiseur doit bloquer la liaison d'un compteur à une activité d'une autre entreprise."""
        headers = self.obtenir_headers_jwt(self.user_org_a)
        
        projet_b = FicheProjet.objects.create(organisation=self.org_b, nom="Projet Concurrent")
        activite_b = Activite.objects.create(fiche_projet=projet_b, nom="Espionnage", statut="OK")
        
        compteur_url = "/api/organisations/compteurs/"
        data = {
            "site": str(self.site_a.id),
            "reference": "COMPT-FRAUD",
            "type_energie": "Gaz",
            "unite": "m3",
            "statut_synchronisation": "OK",
            "activites": [str(activite_b.id)]
        }
        
        response = self.client.post(compteur_url, data, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Vérification sur la clé exacte du champ ciblé par le validateur
        self.assertIn("activites", response.data)
