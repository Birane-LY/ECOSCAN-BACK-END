from django.contrib import admin
from .models import (
    Organisation,
    UtilisateurOrganisation,
    Site,
    FicheProjet,
    Activite,
    Compteur,
)


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les organisations."""

    list_display = ("nom", "secteur", "localisation", "statut", "date_creation")
    list_filter = ("statut", "secteur")
    search_fields = ("nom", "localisation")
    ordering = ("-date_creation",)


@admin.register(UtilisateurOrganisation)
class UtilisateurOrganisationAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les liaisons membres-organisations."""

    list_display = ("utilisateur", "organisation", "date_affiliation")
    list_filter = ("organisation",)
    search_fields = ("utilisateur__email", "organisation__nom")


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les sites physiques."""

    list_display = ("nom", "organisation", "pays", "fuseau_horaire")
    list_filter = ("organisation", "pays")
    search_fields = ("nom", "adresse")


@admin.register(FicheProjet)
class FicheProjetAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les fiches projets."""

    list_display = ("nom", "organisation", "statut", "date_debut", "date_fin")
    list_filter = ("statut", "organisation")
    search_fields = ("nom", "description")


@admin.register(Activite)
class ActiviteAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les activités de projet."""

    list_display = ("nom", "fiche_projet", "categorie", "statut")
    list_filter = ("categorie", "statut")
    search_fields = ("nom", "description")


@admin.register(Compteur)
class CompteurAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les compteurs énergétiques."""

    list_display = ("reference", "site", "type_energie", "unite", "statut_synchronisation")
    list_filter = ("type_energie", "statut_synchronisation", "site")
    search_fields = ("reference", "localisation")
    filter_horizontal = ("activites",)  # Facilite la sélection des activités associées (Many-to-Many)
