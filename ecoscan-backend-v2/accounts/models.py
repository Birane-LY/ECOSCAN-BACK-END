import uuid
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models


class UtilisateurManager(BaseUserManager):
    """Gestionnaire personnalisé pour le modèle Utilisateur."""

    def create_user(self, email, password=None, **extra_fields):
        """Crée et enregistre un utilisateur standard avec un e-mail et un mot de passe."""
        if not email:
            raise ValueError("L'adresse e-mail est obligatoire.")
        email = self.normalize_email(email)
        utilisateur = self.model(email=email, **extra_fields)
        utilisateur.set_password(password)
        utilisateur.save(using=self._db)
        return utilisateur

    def create_superuser(self, email, password=None, **extra_fields):
        """Crée et enregistre un superutilisateur avec les privilèges administratifs complets."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("actif", True)
        extra_fields.setdefault("role", Utilisateur.Role.SUPER_ADMIN)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Un superutilisateur doit avoir is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Un superutilisateur doit avoir is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class Utilisateur(AbstractBaseUser, PermissionsMixin):
    """Modèle utilisateur personnalisé utilisant l'e-mail comme identifiant unique."""

    class Role(models.TextChoices):
        """Rôles disponibles pour les utilisateurs de l'application."""
        SUPER_ADMIN = "SUPER_ADMIN", "Super administrateur"
        ADMIN_ORGANISATION = "ADMIN_ORGANISATION", "Administrateur organisation"
        UTILISATEUR_ORGANISATION = "UTILISATEUR_ORGANISATION", "Utilisateur organisation"
        CONSULTANT = "CONSULTANT", "Consultant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nom = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=40, choices=Role.choices, default=Role.UTILISATEUR_ORGANISATION)
    mot_de_passe_hash = models.CharField(max_length=128, editable=False)
    date_creation = models.DateTimeField(auto_now_add=True)
    actif = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UtilisateurManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["nom"]

    @property
    def is_active(self):
        """Indique si le compte utilisateur est actif."""
        return self.actif

    @property
    def username(self):
        """Renvoie l'e-mail comme identifiant principal."""
        return self.email

    def set_password(self, raw_password):
        """Définit le mot de passe crypté et met à jour le hachage personnalisé."""
        super().set_password(raw_password)
        self.mot_de_passe_hash = self.password

    def check_password(self, raw_password):
        """Vérifie si le mot de passe en clair correspond au hachage enregistré."""
        return super().check_password(raw_password)

    def se_connecter(self):
        """Renvoie le statut actif pour valider la connexion."""
        return self.actif

    def se_deconnecter(self):
        """Renvoie None lors de la déconnexion."""
        return None

    def __str__(self):
        """Renvoie l'adresse e-mail comme représentation textuelle de l'utilisateur."""
        return self.email

class PreferencesUtilisateur(models.Model):
    """Préférences propres à un utilisateur, séparées du modèle Utilisateur
    pour ne pas alourdir le modèle d'auth avec des champs non liés à
    l'identité/la sécurité de connexion."""

    class Theme(models.TextChoices):
        SOMBRE = "SOMBRE", "Sombre"
        CLAIR = "CLAIR", "Clair"

    class Densite(models.TextChoices):
        COMPACTE = "COMPACTE", "Compacte"
        CONFORTABLE = "CONFORTABLE", "Confortable"
        AEREE = "AEREE", "Aérée"

    class Accent(models.TextChoices):
        GREEN = "GREEN", "Émeraude Solaire"
        CYAN = "CYAN", "Cyan Électrique"
        AMBER = "AMBER", "Ambre Vigilance"

    utilisateur = models.OneToOneField(
        Utilisateur, on_delete=models.CASCADE, related_name="preferences"
    )

    # Apparence
    theme = models.CharField(max_length=10, choices=Theme.choices, default=Theme.SOMBRE)
    densite = models.CharField(max_length=15, choices=Densite.choices, default=Densite.CONFORTABLE)
    accent = models.CharField(max_length=10, choices=Accent.choices, default=Accent.GREEN)

    # Notifications
    alertes_email = models.BooleanField(default=True)
    briefing_quotidien = models.BooleanField(default=True)
    detection_anomalies = models.BooleanField(default=True)
    rapport_hebdomadaire = models.BooleanField(default=False)
    delai_inactivite_minutes = models.PositiveIntegerField(default=30)

    date_maj = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Préférences de {self.utilisateur.email}"