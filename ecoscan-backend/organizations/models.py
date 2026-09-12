import uuid

from django.conf import settings
from django.db import models


class Organisation(models.Model):
    """Représente une entité légale ou une entreprise cliente sur la plateforme."""

    class Statut(models.TextChoices):
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDUE = "SUSPENDUE", "Suspendue"
        ARCHIVEE = "ARCHIVEE", "Archivée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nom = models.CharField(max_length=180)
    secteur = models.CharField(max_length=120)
    localisation = models.CharField(max_length=255)
    date_creation = models.DateTimeField(auto_now_add=True)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.EN_ATTENTE)

    def __str__(self):
        return self.nom


class UtilisateurOrganisation(models.Model):
    """Table de liaison unissant un utilisateur physique à une organisation.

    Le rôle applicatif (Admin, Consultant, etc.) et le statut d'activation du compte
    restent centralisés dans le modèle Utilisateur principal pour la sécurité JWT.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="membres")
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organisations_membres")
    date_affiliation = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("organisation", "utilisateur"), name="utilisateur_organisation_unique"),
        ]

    def __str__(self):
        return f"{self.utilisateur.email} <-> {self.organisation.nom}"


class Site(models.Model):
    """Représente un établissement physique ou géographique rattaché à une organisation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="sites")
    nom = models.CharField(max_length=150)
    adresse = models.CharField(max_length=255)
    pays = models.CharField(max_length=100)
    fuseau_horaire = models.CharField(max_length=80)

    def __str__(self):
        return self.nom


class FicheProjet(models.Model):
    """Représente une étude, un projet ou un audit mené au sein d'une organisation."""

    class Statut(models.TextChoices):
        BROUILLON = "BROUILLON", "Brouillon"
        ACTIF = "ACTIF", "Actif"
        TERMINE = "TERMINE", "Terminé"
        ARCHIVE = "ARCHIVE", "Archivé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="fiches_projet")
    nom = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    date_debut = models.DateTimeField(null=True, blank=True)
    date_fin = models.DateTimeField(null=True, blank=True)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.BROUILLON)
    perimetre_analyse = models.TextField(blank=True)

    def creer(self):
        self.statut = self.Statut.ACTIF
        self.save(update_fields=("statut",))

    def archiver(self):
        self.statut = self.Statut.ARCHIVE
        self.save(update_fields=("statut",))

    def __str__(self):
        return self.nom


class Activite(models.Model):
    """Définit un pôle, une tâche ou une sous-section opérationnelle d'une fiche projet."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fiche_projet = models.ForeignKey(FicheProjet, on_delete=models.CASCADE, related_name="activites")
    nom = models.CharField(max_length=180)
    categorie = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    statut = models.CharField(max_length=50)

    def __str__(self):
        return self.nom


class Compteur(models.Model):
    """Représente un équipement de mesure ou un compteur énergétique lié à un site."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="compteurs")
    activites = models.ManyToManyField(Activite, related_name="compteurs", blank=True)
    reference = models.CharField(max_length=120, unique=True)
    type_energie = models.CharField(max_length=80)
    unite = models.CharField(max_length=30)
    localisation = models.CharField(max_length=255, blank=True)
    statut_synchronisation = models.CharField(max_length=50)
    derniere_synchronisation = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.reference
