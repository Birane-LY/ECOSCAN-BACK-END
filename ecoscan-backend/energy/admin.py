from django.contrib import admin
from .models import (
    FichierSource,
    SourceDonnee,
    ImportDonnees,
    FacteurEmission,
    DonneeEnergetique,
    HistoriquePerformance,
    SyntheseFinanciere,
    Objectif,
    Indicateur,
    IndicateurObjectif,
)


@admin.register(FichierSource)
class FichierSourceAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les fichiers téléversés."""

    list_display = ("nom", "mime_type", "taille_octets", "depose_par", "date_depot")
    list_filter = ("mime_type", "date_depot")
    search_fields = ("nom", "depose_par__email")
    ordering = ("-date_depot",)


@admin.register(SourceDonnee)
class SourceDonneeAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les sources de données."""

    list_display = ("nom", "organisation", "type", "origine", "frequence", "statut_synchronisation")
    list_filter = ("type", "statut_synchronisation", "organisation")
    search_fields = ("nom", "origine")


@admin.register(ImportDonnees)
class ImportDonneesAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour le suivi des processus d'import."""

    list_display = ("nom_fichier", "organisation", "format", "nombre_lignes", "nombre_erreurs", "statut", "date_import")
    list_filter = ("statut", "format", "organisation")
    search_fields = ("nom_fichier", "lance_par__email")
    ordering = ("-date_import",)
    actions = ["forcer_le_lancement", "forcer_l_annulation"]

    @admin.action(description="Lancer l'importation (sélection)")
    def forcer_le_lancement(self, request, queryset):
        for obj in queryset:
            obj.lancer_import()
        self.message_user(request, f"{queryset.count()} processus d'importation ont été lancés.")

    @admin.action(description="Annuler l'importation (sélection)")
    def forcer_l_annulation(self, request, queryset):
        for obj in queryset:
            obj.annuler_import()
        self.message_user(request, f"{queryset.count()} processus d'importation ont été annulés.")


@admin.register(FacteurEmission)
class FacteurEmissionAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les facteurs d'émission réglementaires."""

    list_display = ("nom", "type_energie", "valeur", "unite", "source_reglementaire", "version")
    list_filter = ("type_energie", "source_reglementaire")
    search_fields = ("nom",)


@admin.register(DonneeEnergetique)
class DonneeEnergetiqueAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les index de consommation brute."""

    list_display = ("compteur", "source_donnee", "valeur", "unite", "statut_validation", "periode_debut")
    list_filter = ("statut_validation", "unite", "compteur__site__organisation")
    search_fields = ("compteur__reference", "source")
    ordering = ("-periode_debut",)


@admin.register(HistoriquePerformance)
class HistoriquePerformanceAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour la consolidation des performances."""

    list_display = ("fiche_projet", "periode", "consommation", "emissions", "economie", "unite")
    list_filter = ("fiche_projet__organisation",)
    ordering = ("-periode",)


@admin.register(SyntheseFinanciere)
class SyntheseFinanciereAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les bilans financiers et ROI."""

    list_display = ("fiche_projet", "cout_energie", "economie_estimee", "economie_realisee", "retour_investissement", "date_calcul")
    list_filter = ("fiche_projet__organisation",)


@admin.register(Objectif)
class ObjectifAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour le suivi des objectifs de l'organisation."""

    list_display = ("nom", "organisation", "type", "valeur_cible", "progression_actuelle", "statut", "date_fin")
    list_filter = ("statut", "type", "organisation")
    search_fields = ("nom", "description")


@admin.register(Indicateur)
class IndicateurAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les métriques et KPIs."""

    list_display = ("nom", "objectif", "type", "valeur", "unite", "periode")
    list_filter = ("type", "objectif__organisation")
    search_fields = ("nom",)


@admin.register(IndicateurObjectif)
class IndicateurObjectifAdmin(admin.ModelAdmin):
    """Configuration de l'administration pour les consolidations d'objectifs."""

    list_display = ("nom", "objectif", "valeur_initiale", "valeur_cible", "valeur_actuelle", "progression")
    list_filter = ("objectif__organisation",)
    filter_horizontal = ("indicateurs",)
