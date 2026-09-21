import uuid

from django.conf import settings
from django.db import models


class JournalAudit(models.Model):
    """Trace immuable des actions sensibles effectuées sur la plateforme.

    Immuabilité appliquée au niveau du modèle (save/delete), pas seulement au
    niveau de la vue : un JournalAudit qu'on peut modifier ou supprimer après coup
    n'est plus une preuve d'audit fiable, quelle que soit la couche qui l'a écrit.
    """

    class Resultat(models.TextChoices):
        SUCCES = "SUCCES", "Succès"
        ECHEC = "ECHEC", "Échec"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="journaux_audit")
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.CASCADE, null=True, blank=True, related_name="journaux_audit")
    action = models.CharField(max_length=100)
    ressource = models.CharField(max_length=100)
    identifiant_ressource = models.CharField(max_length=100, blank=True)
    resultat = models.CharField(max_length=10, choices=Resultat.choices, default=Resultat.SUCCES)
    details = models.JSONField(default=dict, blank=True)
    adresse_ip = models.GenericIPAddressField(null=True, blank=True)
    date_action = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_action",)
        indexes = [
            models.Index(fields=["organisation", "-date_action"]),
            models.Index(fields=["utilisateur", "-date_action"]),
            models.Index(fields=["ressource", "identifiant_ressource"]),
        ]

    def save(self, *args, **kwargs):
        """Autorise UNIQUEMENT la création — jamais la modification d'une entrée
        déjà persistée. `force_insert` ne suffit pas seul comme garde-fou (Django
        l'utilise aussi en interne) : on vérifie explicitement l'existence en base."""
        if self.pk and JournalAudit.objects.filter(pk=self.pk).exists():
            raise ValueError(
                "JournalAudit est immuable : une entrée déjà enregistrée ne peut "
                "pas être modifiée."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("JournalAudit est immuable : une entrée ne peut pas être supprimée.")

    def __str__(self):
        return f"{self.action} - {self.ressource} ({self.resultat})"