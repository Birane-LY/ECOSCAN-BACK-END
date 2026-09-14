from django.contrib import admin
from .models import Recommandation, Action, Decision, Livrable, ResultatMetrique


@admin.register(Recommandation)
class RecommandationAdmin(admin.ModelAdmin):
    """Configuration de la supervision des recommandations d'efficacité énergétique."""

    list_display = ("titre", "objectif", "priorite", "statut", "economie_estimee", "unite", "date_echeance")
    list_filter = ("priorite", "statut", "objectif__organisation")
    search_fields = ("titre", "description")
    ordering = ("-date_echeance",)


@admin.register(Action)
class ActionAdmin(admin.ModelAdmin):
    """Configuration du suivi opérationnel des plans d'actions de transition."""

    list_display = ("titre", "recommandation", "statut", "responsable", "date_echeance", "date_realisation")
    list_filter = ("statut", "recommandation__objectif__organisation" if hasattr(Action, "recommandation") else "statut",)
    search_fields = ("titre", "description", "responsable__email")
    ordering = ("-date_echeance",)


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    """Configuration d'audit des arbitrages et validations de plans de décarbonation."""

    list_display = ("recommandation", "resultat", "decideur", "date_decision")
    list_filter = ("resultat",)
    search_fields = ("recommandation__titre", "commentaire", "decideur__email")


@admin.register(Livrable)
class LivrableAdmin(admin.ModelAdmin):
    """Configuration de la traçabilité des livrables et rapports d'audits officiels."""

    list_display = ("nom", "fiche_projet", "type", "statut", "version", "date_generation")
    list_filter = ("statut", "type", "fiche_projet__organisation")
    search_fields = ("nom", "type")


@admin.register(ResultatMetrique)
class ResultatMetriqueAdmin(admin.ModelAdmin):
    """Configuration de l'Audit Trail des calculs de métriques et baselines dénormalisés.

    Garantit l'accès aux critères d'éco-conception, de complétude et de fiabilité.
    """

    list_display = (
        "code_metrique",
        "organisation",
        "compteur",
        "valeur",
        "unite",
        "statut_qualite",
        "completude",
        "version_metrique",
        "date_calcul",
    )
    list_filter = ("statut_qualite", "code_metrique", "organisation")
    search_fields = ("code_metrique", "compteur__reference")
    ordering = ("-periode_fin",)
    readonly_fields = ("date_calcul",)
