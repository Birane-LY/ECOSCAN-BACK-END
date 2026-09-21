from django.utils import timezone
from rest_framework import serializers
from rest_framework.validators import UniqueValidator
from .models import (
    Organisation,
    UtilisateurOrganisation,
    Site,
    FicheProjet,
    Activite,
    Compteur,
    ConfigurationSecurite,
)


class OrganisationSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la consultation, la création et la gestion des organisations."""

    nom = serializers.CharField(
        validators=[
            UniqueValidator(
                queryset=Organisation.objects.all(),
                message="Une organisation possédant ce nom exact est déjà enregistrée."
            )
        ]
    )
    nombre_membres = serializers.SerializerMethodField()
    emails_admin = serializers.SerializerMethodField()

    class Meta:
        model = Organisation
        fields = (
            "id", "nom", "secteur", "localisation", "date_creation", "statut",
            "defaut_paiement", "nombre_membres", "emails_admin",
        )
        read_only_fields = ("id", "date_creation", "nombre_membres", "emails_admin")

    def get_nombre_membres(self, obj):
        return obj.membres.count()

    def get_emails_admin(self, obj):
        """Emails des ADMIN_ORGANISATION de cette organisation — c'est à ces
        adresses que n8n envoie les relances de paiement, faute de champ
        'email de contact facturation' dédié sur ce modèle."""
        return list(
            obj.membres.filter(
                utilisateur__role="ADMIN_ORGANISATION"
            ).values_list("utilisateur__email", flat=True)
        )

    def validate(self, data):
        """Sécurise le statut de l'organisation lors de sa soumission."""
        request = self.context.get("request")
        
        # Si c'est une création (POST), seul un membre de l'équipe de dev (Super Admin)
        # peut forcer un statut actif. Pour les autres, le statut est impérativement mis en attente.
        if not self.instance:
            if not request or not request.user or request.user.role != "SUPER_ADMIN":
                data["statut"] = Organisation.Statut.EN_ATTENTE
                
        return data


class UtilisateurOrganisationSerializer(serializers.ModelSerializer):
    """Sérialiseur gérant la table de liaison entre les membres et leurs organisations."""

    class Meta:
        model = UtilisateurOrganisation
        fields = ("id", "organisation", "utilisateur", "date_affiliation")
        read_only_fields = ("id", "date_affiliation")


class SiteSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la gestion des établissements physiques ou géographiques."""

    class Meta:
        model = Site
        fields = ("id", "organisation", "nom", "adresse", "pays", "fuseau_horaire")
        read_only_fields = ("id",)

    def validate_organisation(self, value):
        """Vérifie que l'acteur connecté appartient bien à l'organisation pour y ajouter un site."""
        request = self.context.get("request")
        if not request or not request.user:
            raise serializers.ValidationError("Authentification requise.")

        # L'équipe de dev (Super Admin) contourne la règle pour la maintenance
        if request.user.role == "SUPER_ADMIN":
            return value

        # L'Admin d'organisation doit appartenir à l'organisation ciblée
        est_membre = UtilisateurOrganisation.objects.filter(
            organisation=value, 
            utilisateur=request.user
        ).exists()
        
        if not est_membre:
            raise serializers.ValidationError(
                "Vous n'avez pas l'autorisation d'ajouter ou de lier un établissement à cette organisation."
            )
        return value


class FicheProjetSerializer(serializers.ModelSerializer):
    """Sérialiseur pour la gestion du cycle de vie des fiches projets et des audits."""

    class Meta:
        model = FicheProjet
        fields = (
            "id",
            "organisation",
            "nom",
            "description",
            "date_debut",
            "date_fin",
            "statut",
            "perimetre_analyse",
        )
        read_only_fields = ("id",)

    def validate_organisation(self, value):
        """Vérifie le cloisonnement de la structure avant d'autoriser la création d'un projet."""
        request = self.context.get("request")
        if not request or not request.user:
            raise serializers.ValidationError("Authentification requise.")

        if request.user.role == "SUPER_ADMIN":
            return value

        est_membre = UtilisateurOrganisation.objects.filter(
            organisation=value, 
            utilisateur=request.user
        ).exists()
        
        if not est_membre:
            raise serializers.ValidationError(
                "Vous ne pouvez pas initier de fiche projet pour une organisation dont vous n'êtes pas membre."
            )
        return value

    def validate(self, data):
        """Validation temporelle des dates de l'audit."""
        date_debut = data.get("date_debut") or (self.instance.date_debut if self.instance else None)
        date_fin = data.get("date_fin") or (self.instance.date_fin if self.instance else None)

        if date_debut and date_fin and date_fin < date_debut:
            raise serializers.ValidationError(
                {"date_fin": "La date de fin de la fiche projet ne peut pas être antérieure à sa date de début."}
            )
        return data


class ActiviteSerializer(serializers.ModelSerializer):
    """Sérialiseur pour cartographier les tâches opérationnelles d'un projet."""

    class Meta:
        model = Activite
        fields = ("id", "fiche_projet", "nom", "categorie", "description", "statut")
        read_only_fields = ("id",)


class CompteurSerializer(serializers.ModelSerializer):
    """Sérialiseur pour le suivi des compteurs et équipements de mesure énergétique."""

    reference = serializers.CharField(
        validators=[
            UniqueValidator(
                queryset=Compteur.objects.all(),
                message="Ce numéro de référence de compteur est déjà enregistré dans le système."
            )
        ]
    )

    class Meta:
        model = Compteur
        fields = (
            "id",
            "site",
            "activites",
            "reference",
            "type_energie",
            "unite",
            "localisation",
            "statut_synchronisation",
            "derniere_synchronisation",
            "puissance_souscrite_kva",
        )
        read_only_fields = ("id",)

    def validate(self, data):
        """Validation complexe de cohérence entre le site du compteur, ses
        activités liées, ET l'appartenance de l'acteur à l'organisation du site
        (absente jusqu'ici — create() n'est protégé par aucune vérification
        d'objet puisqu'aucun objet n'existe encore à ce stade)."""
        request = self.context.get("request")
        site_cible = data.get("site") or (self.instance.site if self.instance else None)

        if site_cible and request and request.user:
            if getattr(request.user, "role", None) != "SUPER_ADMIN":
                est_membre = UtilisateurOrganisation.objects.filter(
                    organisation=site_cible.organisation, utilisateur=request.user
                ).exists()
                if not est_membre:
                    raise serializers.ValidationError(
                        {"site": "Vous ne pouvez pas ajouter un compteur à un site d'une organisation dont vous n'êtes pas membre."}
                    )

        list_activites = data.get("activites") or (list(self.instance.activites.all()) if self.instance else [])
        if site_cible and list_activites:
            org_du_site = site_cible.organisation
            for activite in list_activites:
                if activite.fiche_projet.organisation != org_du_site:
                    raise serializers.ValidationError(
                        {"activites": f"L'activité '{activite.nom}' n'appartient pas à l'organisation gérant ce site. Liaison impossible."}
                    )
        return data


class ConfigurationSecuriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConfigurationSecurite
        fields = ("id", "organisation", "deux_facteurs_obligatoire", "date_maj")
        read_only_fields = ("id", "organisation", "date_maj")