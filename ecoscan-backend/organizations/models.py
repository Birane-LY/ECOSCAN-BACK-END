import uuid

from django.conf import settings
from django.db import models


class Organisation(models.Model):
    """Représente une entité légale ou une entreprise cliente sur la plateforme."""

    class Statut(models.TextChoices):
        """Statuts définissant le cycle de vie et les accès d'une organisation."""
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
        """Renvoie le nom de l'organisation comme représentation textuelle."""
        return self.nom


class Permission(models.Model):
    """Définit une autorisation atomique basée sur une ressource et une action précise."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nom = models.CharField(max_length=100)
    ressource = models.CharField(max_length=100)
    action = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("ressource", "action"), name="permission_ressource_action_unique"),
        ]

    def __str__(self):
        """Renvoie le nom de la permission."""
        return self.nom


class Role(models.Model):
    """Définit un profil ou un niveau d'accès au sein des organisations."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nom = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    # Déclaration standardisée de la relation Many-to-Many
    permissions_associees = models.ManyToManyField(
        Permission,
        through="RolePermission",
        related_name="roles_associes"
    )

    def __str__(self):
        """Renvoie le nom du rôle."""
        return self.nom


class RolePermission(models.Model):
    """Table de liaison associant les permissions aux différents rôles."""

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="roles")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("role", "permission"), name="role_permission_unique"),
        ]


class UtilisateurOrganisation(models.Model):
    """Gère l'affectation, le rôle et le statut d'un utilisateur au sein d'une organisation."""

    class Statut(models.TextChoices):
        """États de l'affiliation d'un collaborateur à une organisation."""
        INVITE = "INVITE", "Invité"
        ACTIF = "ACTIF", "Actif"
        REFUSE = "REFUSE", "Refusé"
        RETIRE = "RETIRE", "Retiré"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="membres")
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organisations_membres")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="membres")
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.INVITE)
    date_invitation = models.DateTimeField(auto_now_add=True)
    date_acceptation = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("organisation", "utilisateur"), name="utilisateur_organisation_unique"),
        ]


class Site(models.Model):
    """Représente un établissement physique ou géographique rattaché à une organisation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="sites")
    nom = models.CharField(max_length=150)
    adresse = models.CharField(max_length=255)
    pays = models.CharField(max_length=100)
    fuseau_horaire = models.CharField(max_length=80)

    def __str__(self):
        """Renvoie le nom du site."""
        return self.nom


class FicheProjet(models.Model):
    """Représente une étude, un projet ou un audit mené au sein d'une organisation."""

    class Statut(models.TextChoices):
        """Cycle de vie d'une fiche projet."""
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
        """Fait passer la fiche projet du statut brouillon au statut actif."""
        self.statut = self.Statut.ACTIF
        self.save(update_fields=("statut",))

    def archiver(self):
        """Archive la fiche projet pour restreindre ses modifications futures."""
        self.statut = self.Statut.ARCHIVE
        self.save(update_fields=("statut",))


class Activite(models.Model):
    """Définit un pôle, une tâche ou une sous-section opérationnelle d'une fiche projet."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fiche_projet = models.ForeignKey(FicheProjet, on_delete=models.CASCADE, related_name="activites")
    nom = models.CharField(max_length=180)
    categorie = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    statut = models.CharField(max_length=50)


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
        """Renvoie la référence unique du compteur."""
        return self.reference
