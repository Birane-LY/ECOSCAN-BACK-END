import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Plan(models.Model):
    """Plan tarifaire, géré exclusivement par le SUPER_ADMIN d'EcoScan (voir
    permissions.py) même si son usage concerne les organisations — cohérent avec
    la demande : le catalogue de plans est une décision plateforme, pas une
    donnée par tenant.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    nom = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    prix_mensuel = models.DecimalField(max_digits=12, decimal_places=2)
    prix_annuel = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    devise = models.CharField(max_length=3, default="XOF")

    limites = models.JSONField(default=dict, blank=True)
    fonctionnalites = models.JSONField(default=dict, blank=True)
    actif = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.nom


class Abonnement(models.Model):
    """Rattaché à l'organisation, jamais à un utilisateur individuel."""

    class Statut(models.TextChoices):
        TRIALING = "TRIALING", "Essai"
        ACTIVE = "ACTIVE", "Actif"
        PAST_DUE = "PAST_DUE", "Paiement en retard"
        GRACE_PERIOD = "GRACE_PERIOD", "Période de grâce"
        SUSPENDED = "SUSPENDED", "Suspendu"
        CANCELED = "CANCELED", "Annulé"
        EXPIRED = "EXPIRED", "Expiré"

    class Fournisseur(models.TextChoices):
        PAYDUNYA = "PAYDUNYA", "PayDunya"
        PAYTECH = "PAYTECH", "PayTech"
        FLUTTERWAVE = "FLUTTERWAVE", "Flutterwave"
        MANUAL = "MANUAL", "Manuel"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.PROTECT, related_name="abonnements")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)

    statut = models.CharField(max_length=30, choices=Statut.choices)
    periodicite = models.CharField(max_length=10, choices=[("MENSUEL", "Mensuel"), ("ANNUEL", "Annuel")], default="MENSUEL")

    debut = models.DateTimeField()
    fin_periode = models.DateTimeField()
    fin_grace = models.DateTimeField(null=True, blank=True)
    annule_le = models.DateTimeField(null=True, blank=True)
    fin_acces_si_annule = models.DateTimeField(null=True, blank=True)

    fournisseur = models.CharField(max_length=50, choices=Fournisseur.choices)
    external_customer_id = models.CharField(max_length=255, blank=True)
    external_subscription_id = models.CharField(max_length=255, blank=True)

    metadata = models.JSONField(default=dict, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-debut",)

    def __str__(self):
        return f"{self.organisation} — {self.plan.nom} ({self.statut})"


class Facture(models.Model):
    class Statut(models.TextChoices):
        DRAFT = "DRAFT", "Brouillon"
        OPEN = "OPEN", "Ouverte"
        PAID = "PAID", "Payée"
        PAST_DUE = "PAST_DUE", "En retard"
        VOID = "VOID", "Annulée"
        UNCOLLECTIBLE = "UNCOLLECTIBLE", "Irrécouvrable"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    abonnement = models.ForeignKey(Abonnement, on_delete=models.PROTECT, related_name="factures")
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.PROTECT, related_name="factures")

    numero = models.CharField(max_length=100, unique=True)
    montant_ht = models.DecimalField(max_digits=12, decimal_places=2)
    taxes = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    montant_total = models.DecimalField(max_digits=12, decimal_places=2)
    devise = models.CharField(max_length=3, default="XOF")

    periode_debut = models.DateTimeField()
    periode_fin = models.DateTimeField()
    date_echeance = models.DateTimeField()

    statut = models.CharField(max_length=30, choices=Statut.choices, default=Statut.DRAFT)

    external_invoice_id = models.CharField(max_length=255, blank=True)
    pdf_url = models.URLField(blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-periode_debut",)

    def __str__(self):
        return self.numero


class Paiement(models.Model):
    class Statut(models.TextChoices):
        PENDING = "PENDING", "En attente"
        SUCCEEDED = "SUCCEEDED", "Réussi"
        FAILED = "FAILED", "Échoué"
        REFUNDED = "REFUNDED", "Remboursé"
        CANCELED = "CANCELED", "Annulé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facture = models.ForeignKey(Facture, on_delete=models.PROTECT, related_name="paiements")
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.PROTECT, related_name="paiements")

    montant = models.DecimalField(max_digits=12, decimal_places=2)
    devise = models.CharField(max_length=3, default="XOF")
    methode = models.CharField(max_length=50, blank=True)

    statut = models.CharField(max_length=30, choices=Statut.choices, default=Statut.PENDING)

    external_payment_id = models.CharField(max_length=255, unique=True)
    date_paiement = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_creation",)

    def __str__(self):
        return f"{self.montant} {self.devise} — {self.statut}"


class Relance(models.Model):
    class Canal(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"
        IN_APP = "IN_APP", "Notification interne"

    class Statut(models.TextChoices):
        PENDING = "PENDING", "En attente"
        PROCESSING = "PROCESSING", "En cours d'envoi"
        SENT = "SENT", "Envoyée"
        CANCELED = "CANCELED", "Annulée"
        FAILED = "FAILED", "Échouée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facture = models.ForeignKey(Facture, on_delete=models.CASCADE, related_name="relances")

    niveau = models.PositiveIntegerField()
    canal = models.CharField(max_length=20, choices=Canal.choices)
    planifiee_le = models.DateTimeField()
    envoyee_le = models.DateTimeField(null=True, blank=True)

    statut = models.CharField(max_length=30, choices=Statut.choices, default=Statut.PENDING)
    idempotency_key = models.CharField(max_length=255, unique=True)

    class Meta:
        ordering = ("planifiee_le",)

    def __str__(self):
        return f"Relance niveau {self.niveau} — {self.facture.numero}"


class WebhookEvent(models.Model):
    """Déduplication des événements webhook du prestataire."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fournisseur = models.CharField(max_length=50)
    external_event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict)
    traite_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("fournisseur", "external_event_id"), name="webhook_event_unique_par_fournisseur"),
        ]

    def __str__(self):
        return f"{self.fournisseur}:{self.external_event_id}"


class HistoriqueAbonnement(models.Model):
    """Trace chaque changement de plan ou de statut — immuable comme JournalAudit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    abonnement = models.ForeignKey(Abonnement, on_delete=models.CASCADE, related_name="historique")
    ancien_plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    nouveau_plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    ancien_statut = models.CharField(max_length=30, blank=True)
    nouveau_statut = models.CharField(max_length=30, blank=True)
    raison = models.CharField(max_length=100)
    date_effet = models.DateTimeField()
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_creation",)

    def save(self, *args, **kwargs):
        if self.pk and HistoriqueAbonnement.objects.filter(pk=self.pk).exists():
            raise ValueError("HistoriqueAbonnement est immuable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("HistoriqueAbonnement est immuable.")