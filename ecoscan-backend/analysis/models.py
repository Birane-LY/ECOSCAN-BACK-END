import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Recommandation(models.Model):
    """Représente une piste d'amélioration ou d'économie d'énergie proposée.

    Liée à un objectif global de réduction des consommations ou de l'empreinte carbone.
    """

    class Priorite(models.TextChoices):
        BASSE = "BASSE", "Basse"
        MOYENNE = "MOYENNE", "Moyenne"
        HAUTE = "HAUTE", "Haute"
        CRITIQUE = "CRITIQUE", "Critique"

    class Statut(models.TextChoices):
        PROPOSEE = "PROPOSEE", "Proposée"
        EN_COURS = "EN_COURS", "En cours"
        DECIDEE = "DECIDEE", "Décidée"
        REJETEE = "REJETEE", "Rejetée"
        TERMINEE = "TERMINEE", "Terminée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    objectif = models.ForeignKey(
        "energy.Objectif",
        on_delete=models.CASCADE,
        related_name="recommandations",
    )
    titre = models.CharField(max_length=180)
    description = models.TextField()
    impact_estime = models.DecimalField(max_digits=18, decimal_places=6)
    economie_estimee = models.DecimalField(max_digits=18, decimal_places=2)
    unite = models.CharField(max_length=30)
    priorite = models.CharField(max_length=20, choices=Priorite.choices, default=Priorite.MOYENNE)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.PROPOSEE)
    date_echeance = models.DateTimeField(null=True, blank=True)
    date_decision = models.DateTimeField(null=True, blank=True)

    def generer(self):
        """Initialise la recommandation au statut Proposée."""
        self.statut = self.Statut.PROPOSEE
        self.save(update_fields=("statut",))

    def marquer_comme_decidee(self):
        """Valide l'adoption de la recommandation et fige la date d'arbitrage."""
        self.statut = self.Statut.DECIDEE
        self.date_decision = timezone.now()
        self.save(update_fields=("statut", "date_decision"))

    def __str__(self):
        return self.titre


class Action(models.Model):
    """Étape opérationnelle concrète découlant d'une recommandation d'efficacité énergétique.

    Permet de suivre l'avancement technique sur le terrain et d'affecter un responsable.
    """

    class Statut(models.TextChoices):
        A_FAIRE = "A_FAIRE", "À faire"
        EN_COURS = "EN_COURS", "En cours"
        TERMINEE = "TERMINEE", "Terminée"
        ANNULEE = "ANNULEE", "Annulée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recommandation = models.ForeignKey(Recommandation, on_delete=models.CASCADE, related_name="actions")
    titre = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.A_FAIRE)
    responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="actions_responsables",
    )
    date_echeance = models.DateTimeField(null=True, blank=True)
    date_realisation = models.DateTimeField(null=True, blank=True)

    def suivre(self):
        """Passe l'étape opérationnelle en cours de traitement sur le terrain."""
        self.statut = self.Statut.EN_COURS
        self.save(update_fields=("statut",))

    def cloturer(self):
        """Marque l'action comme complétée et enregistre l'instant exact de réalisation."""
        self.statut = self.Statut.TERMINEE
        self.date_realisation = timezone.now()
        self.save(update_fields=("statut", "date_realisation"))

    def __str__(self):
        return self.titre


class Decision(models.Model):
    """Trace l'arbitrage formel rendu par un décideur pour une recommandation.

    Garantit l'Audit Trail des choix stratégiques d'investissement de l'organisation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recommandation = models.OneToOneField(Recommandation, on_delete=models.CASCADE, related_name="decision")
    resultat = models.CharField(max_length=100)
    commentaire = models.TextField(blank=True)
    date_decision = models.DateTimeField()
    decideur = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="decisions_prises",
    )

    def __str__(self):
        return f"{self.recommandation.titre} : {self.resultat}"


class Livrable(models.Model):
    """Document, rapport d'audit ou certification officielle généré pour un projet.

    Assure la traçabilité des livrables et la gestion de leurs versions successives.
    """

    class Statut(models.TextChoices):
        BROUILLON = "BROUILLON", "Brouillon"
        GENERE = "GENERE", "Généré"
        VALIDE = "VALIDE", "Validé"
        ARCHIVE = "ARCHIVE", "Archivé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fiche_projet = models.ForeignKey(
        "organizations.FicheProjet",
        on_delete=models.CASCADE,
        related_name="livrables",
    )
    nom = models.CharField(max_length=180)
    type = models.CharField(max_length=80)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.BROUILLON)
    url_fichier = models.URLField(blank=True)
    version = models.PositiveIntegerField(default=1)
    date_generation = models.DateTimeField(null=True, blank=True)

    def generer(self):
        """Enregistre la publication du document et fige la date d'édition."""
        self.statut = self.Statut.GENERE
        self.date_generation = timezone.now()
        self.save(update_fields=("statut", "date_generation"))

    def valider(self):
        """Approuve définitivement la conformité technique du livrable."""
        self.statut = self.Statut.VALIDE
        self.save(update_fields=("statut",))

    def __str__(self):
        return self.nom


class ResultatMetrique(models.Model):
    """Persiste chaque valeur de métrique calculée, avec tout le contexte nécessaire

    pour la réinterpréter plus tard sans recalcul.

    Un rapport ancien doit rester interprétable avec la version de métrique utilisée
    à sa génération. D'où la dénormalisation de la version et des baselines associées.
    """

    class StatutQualite(models.TextChoices):
        FIABLE = "FIABLE", "Fiable"
        ESTIME = "ESTIME", "Estimé"
        INSUFFISANT = "INSUFFISANT", "Insuffisant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        "organizations.Organisation",
        on_delete=models.CASCADE,
        related_name="resultats_metriques",
    )
    compteur = models.ForeignKey(
        "organizations.Compteur",
        on_delete=models.CASCADE,
        related_name="resultats_metriques",
        null=True,
        blank=True,
    )
    code_metrique = models.CharField(max_length=80)
    version_metrique = models.CharField(max_length=20)
    valeur = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    unite = models.CharField(max_length=30)
    periode_debut = models.DateTimeField()
    periode_fin = models.DateTimeField()
    baseline_type = models.CharField(max_length=50, blank=True)
    baseline_valeur = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    baseline_nombre_observations = models.PositiveIntegerField(default=0)
    completude = models.DecimalField(max_digits=5, decimal_places=4)
    statut_qualite = models.CharField(max_length=20, choices=StatutQualite.choices)
    confiance = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)

    sources = models.JSONField(default=list)
    limites = models.JSONField(default=list)

    date_calcul = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-periode_fin",)
        constraints = [
            models.UniqueConstraint(
                fields=("organisation", "compteur", "code_metrique", "periode_debut", "periode_fin", "version_metrique"),
                name="resultat_metrique_unique_par_periode",
            ),
        ]

    def __str__(self):
        return f"{self.code_metrique} ({self.organisation_id}) : {self.valeur} {self.unite}"
