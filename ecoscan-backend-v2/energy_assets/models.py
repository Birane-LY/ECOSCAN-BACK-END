import uuid

from django.db import models


class Zone(models.Model):
    """Représente une zone physique d'un site contenant des équipements."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(
        "organizations.Site",
        on_delete=models.CASCADE,
        related_name="zones",
    )
    nom = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("site__nom", "nom")
        constraints = [
            models.UniqueConstraint(fields=("site", "nom"), name="zone_site_nom_unique"),
        ]

    def __str__(self):
        return f"{self.site.nom} — {self.nom}"


class Equipement(models.Model):
    """Représente un équipement électrique individuel suivi sur un site."""

    class Etat(models.TextChoices):
        ON = "ON", "En marche"
        OFF = "OFF", "À l'arrêt"
        UNKNOWN = "UNKNOWN", "Inconnu"

    class Criticite(models.TextChoices):
        BASSE = "BASSE", "Basse"
        MOYENNE = "MOYENNE", "Moyenne"
        HAUTE = "HAUTE", "Haute"
        CRITIQUE = "CRITIQUE", "Critique"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(
        "organizations.Site",
        on_delete=models.CASCADE,
        related_name="equipements",
    )
    zone = models.ForeignKey(
        Zone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="equipements",
    )
    nom = models.CharField(max_length=180)
    categorie = models.CharField(max_length=100)
    puissance_nominale_kw = models.DecimalField(max_digits=12, decimal_places=3)
    puissance_veille_kw = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=0,
    )
    quantite = models.PositiveIntegerField(default=1)
    etat_operationnel = models.CharField(
        max_length=10,
        choices=Etat.choices,
        default=Etat.UNKNOWN,
    )
    criticite = models.CharField(
        max_length=12,
        choices=Criticite.choices,
        default=Criticite.MOYENNE,
    )
    monitoring_active = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("site__nom", "nom")
        indexes = [
            models.Index(
                fields=("site", "monitoring_active"),
                name="equip_site_monitor_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(puissance_nominale_kw__gte=0),
                name="equip_puissance_nom_non_neg",
            ),
            models.CheckConstraint(
                condition=models.Q(puissance_veille_kw__gte=0),
                name="equip_puissance_veille_non_neg",
            ),
        ]

    def __str__(self):
        return f"{self.site.nom} — {self.nom}"


class ProfilFonctionnement(models.Model):
    """Définit un créneau récurrent de fonctionnement d'un équipement."""

    class JourSemaine(models.IntegerChoices):
        LUNDI = 0, "Lundi"
        MARDI = 1, "Mardi"
        MERCREDI = 2, "Mercredi"
        JEUDI = 3, "Jeudi"
        VENDREDI = 4, "Vendredi"
        SAMEDI = 5, "Samedi"
        DIMANCHE = 6, "Dimanche"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipement = models.ForeignKey(
        Equipement,
        on_delete=models.CASCADE,
        related_name="profils_fonctionnement",
    )
    nom = models.CharField(max_length=120, blank=True)
    jour_semaine = models.PositiveSmallIntegerField(choices=JourSemaine.choices)
    heure_debut = models.TimeField()
    heure_fin = models.TimeField()
    actif = models.BooleanField(default=True)

    class Meta:
        ordering = ("jour_semaine", "heure_debut")
        constraints = [
            models.UniqueConstraint(
                fields=("equipement", "jour_semaine", "heure_debut"),
                name="profil_equip_jour_start_uq",
            ),
            models.CheckConstraint(
                condition=models.Q(heure_debut__lt=models.F("heure_fin")),
                name="profil_heure_fin_apres_debut",
            ),
        ]

    def __str__(self):
        return f"{self.equipement.nom} — {self.get_jour_semaine_display()}"


class Capteur(models.Model):
    """Associe un capteur physique ou simulé à un équipement."""

    class Mode(models.TextChoices):
        SIMULATED = "SIMULATED", "Simulé"
        REAL = "REAL", "Réel"

    class Statut(models.TextChoices):
        ACTIVE = "ACTIVE", "Actif"
        INACTIVE = "INACTIVE", "Inactif"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipement = models.ForeignKey(
        Equipement,
        on_delete=models.CASCADE,
        related_name="capteurs",
    )
    identifiant = models.CharField(max_length=120, unique=True)
    type = models.CharField(max_length=80)
    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.SIMULATED)
    frequence_secondes = models.PositiveIntegerField(default=15)
    statut = models.CharField(max_length=10, choices=Statut.choices, default=Statut.ACTIVE)
    derniere_communication = models.DateTimeField(null=True, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("identifiant",)
        indexes = [
            models.Index(
                fields=("statut", "derniere_communication"),
                name="capteur_statut_last_idx",
            ),
        ]

    def __str__(self):
        return self.identifiant


class MesureCapteur(models.Model):
    """Enregistre une mesure horodatée reçue d'un capteur."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capteur = models.ForeignKey(
        Capteur,
        on_delete=models.CASCADE,
        related_name="mesures",
    )
    valeur = models.DecimalField(max_digits=20, decimal_places=6)
    unite = models.CharField(max_length=24, blank=True)
    date_mesure = models.DateTimeField()
    date_reception = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_mesure",)
        indexes = [
            models.Index(
                fields=("capteur", "date_mesure"),
                name="mesure_capteur_date_idx",
            ),
        ]

    def __str__(self):
        return f"{self.capteur.identifiant} — {self.valeur} {self.unite}".strip()


class EtatEquipement(models.Model):
    """Conserve l'état souhaité, l'état rapporté et les dernières mesures d'un équipement."""

    class StatutSynchronisation(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Inconnu"
        SYNCHRONIZED = "SYNCHRONIZED", "Synchronisé"
        OUT_OF_SYNC = "OUT_OF_SYNC", "Désynchronisé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipement = models.OneToOneField(
        Equipement,
        on_delete=models.CASCADE,
        related_name="etat",
    )
    etat_souhaite = models.CharField(
        max_length=10,
        choices=Equipement.Etat.choices,
        default=Equipement.Etat.UNKNOWN,
    )
    etat_rapporte = models.CharField(
        max_length=10,
        choices=Equipement.Etat.choices,
        default=Equipement.Etat.UNKNOWN,
    )
    puissance_actuelle_kw = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=0,
    )
    energie_cumulee_kwh = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        default=0,
    )
    statut_synchronisation = models.CharField(
        max_length=16,
        choices=StatutSynchronisation.choices,
        default=StatutSynchronisation.UNKNOWN,
    )
    date_etat_souhaite = models.DateTimeField(null=True, blank=True)
    date_etat_rapporte = models.DateTimeField(null=True, blank=True)
    date_maj = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(puissance_actuelle_kw__gte=0),
                name="etat_puissance_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(energie_cumulee_kwh__gte=0),
                name="etat_energie_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.equipement.nom} — {self.get_etat_rapporte_display()}"
