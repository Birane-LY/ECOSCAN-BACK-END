from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Utilisateur


@admin.register(Utilisateur)
class UtilisateurAdmin(UserAdmin):
    """Configuration de l'interface d'administration Django pour le modèle Utilisateur.

    Cette classe adapte le `UserAdmin` natif pour prendre en compte l'authentification
    par e-mail, l'affichage des rôles personnalisés et l'identifiant UUID.
    """

    # Tri par défaut dans la liste
    ordering = ("email",)

    # Colonnes affichées dans le tableau récapitulatif
    list_display = ("email", "nom", "role", "actif", "is_staff")

    # Filtres disponibles dans la barre latérale droite
    list_filter = ("role", "actif", "is_staff")

    # Champs utilisés pour la barre de recherche
    search_fields = ("email", "nom")

    # Organisation des champs lors de la modification d'un utilisateur
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Informations personnelles", {"fields": ("nom", "role")}),
        ("Permissions", {"fields": ("actif", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login",)}),
    )

    # Organisation des champs lors de la création d'un utilisateur depuis l'interface
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "nom", "password1", "password2", "role", "actif", "is_staff"),
            },
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        """Limite les droits de modification selon le rôle de l'utilisateur connecté.
        
        Un utilisateur (non super-admin) ne peut pas modifier les champs d'un autre profil.
        """
        # Si on crée un nouvel utilisateur ou si l'utilisateur est SUPER_ADMIN, aucune restriction automatique
        if not obj or request.user.role == Utilisateur.Role.SUPER_ADMIN:
            return self.readonly_fields

        # Si l'utilisateur connecté tente de modifier le profil de QUELQU'UN D'AUTRE
        if request.user.id != obj.id:
            # On rend TOUS les champs éditables invisibles/bloqués pour lui
            return [f.name for f in obj._meta.fields if not f.primary_key]

        # Si l'utilisateur modifie SON PROPRE profil, on peut bloquer certains champs sensibles (comme son propre rôle)
        return ("role", "is_staff", "is_superuser")

