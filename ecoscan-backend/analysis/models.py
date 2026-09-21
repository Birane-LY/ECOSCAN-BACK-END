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
        """Valide l'adoption de la recommandation, fige la date d'arbitrage, et
        fait progresser l'Objectif lié d'autant que l'économie estimée — c'est
        aujourd'hui la SEULE façon dont un Objectif progresse (aucun calcul
        automatique depuis les métriques réelles n'existe encore, et ce n'est
        pas ajouté ici pour ne pas présenter une progression déduite comme une
        mesure certaine)."""
        self.statut = self.Statut.DECIDEE
        self.date_decision = timezone.now()
        self.save(update_fields=("statut", "date_decision"))

        objectif = self.objectif
        if objectif.unite == self.unite:
            nouvelle_progression = (objectif.progression_actuelle or 0) + self.economie_estimee
            objectif.mettre_a_jour_progression(nouvelle_progression)
        # Si les unités diffèrent (ex. objectif en %, recommandation en kWh),
        # on ne force aucune conversion arbitraire — la progression reste
        # inchangée, à mettre à jour manuellement si besoin.

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

    # Mesure d'impact réel (impact_service.py) — distincts de
    # Recommandation.economie_estimee, qui reste la prévision AVANT exécution.
    # Restent à None tant qu'aucune mesure n'a été faite : ne jamais les
    # confondre avec "aucun impact".
    economie_realisee_fcfa = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    taux_realisation_impact = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    date_mesure_impact = models.DateTimeField(null=True, blank=True)

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
    fichier_source = models.ForeignKey(
        "energy.FichierSource", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="resultats_metriques",
        help_text="Fichier source à l'origine de ce résultat, quand il provient d'un import OCR/capture plutôt que d'un calcul sur relevés.",
    )

    class Meta:
        ordering = ("-periode_fin",)
        constraints = [
            models.UniqueConstraint(
                fields=("organisation", "compteur", "code_metrique", "periode_debut", "periode_fin", "version_metrique"),
                name="resultat_metrique_unique_par_periode",
            ),
    
            models.UniqueConstraint(
                fields=("fichier_source",),
                condition=models.Q(fichier_source__isnull=False),
                name="resultat_metrique_unique_par_fichier_source",
            ),
        ]

    def __str__(self):
        return f"{self.code_metrique} ({self.organisation_id}) : {self.valeur} {self.unite}"


class ObservationOperationnelle(models.Model):
    """Note de terrain (redémarrage de machines, panne, événement exceptionnel...)
    associée à une organisation et un créneau — c'est le "contexte" que
    context_service.py rapproche des anomalies détectées.

    `valide` est délibérément séparé de la création : une observation n'entre
    dans le contexte fourni à l'IA (hypothesis_service.py) qu'une fois confirmée
    par un humain, jamais une note brute non vérifiée.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.CASCADE, related_name="observations")
    auteur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="observations_creees")
    texte = models.TextField()
    date_observation = models.DateTimeField()
    creneau = models.CharField(max_length=10, blank=True)
    valide = models.BooleanField(default=False)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_observation",)

    def __str__(self):
        return f"{self.date_observation:%Y-%m-%d %H:%M} — {self.texte[:60]}"


class Anomalie(models.Model):
    """Écart statistique détecté sur une métrique — PAS une cause, PAS une erreur
    confirmée. Les seuils (10/20/40 %) viennent du document de justification
    partagé pour ce projet ; ils sont un point de départ, pas calibrés par
    organisation (voir anomaly_service.py)."""

    class Severite(models.TextChoices):
        SURVEILLANCE = "SURVEILLANCE", "Surveillance"
        ALERTE = "ALERTE", "Alerte"
        INVESTIGATION_PRIORITAIRE = "INVESTIGATION_PRIORITAIRE", "Investigation prioritaire"

    class Statut(models.TextChoices):
        DETECTED = "DETECTED", "Détectée"
        NEEDS_CONTEXT = "NEEDS_CONTEXT", "Contexte requis"
        CONFIRMED = "CONFIRMED", "Confirmée"
        DISMISSED = "DISMISSED", "Écartée"
        ACTION_CREATED = "ACTION_CREATED", "Action créée"
        RESOLVED = "RESOLVED", "Résolue"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.CASCADE, related_name="anomalies")
    resultat_metrique = models.ForeignKey(
        ResultatMetrique, on_delete=models.SET_NULL, null=True, blank=True, related_name="anomalies"
    )
    type = models.CharField(max_length=50)
    severite = models.CharField(max_length=30, choices=Severite.choices)
    valeur_observee = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    valeur_attendue = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    ecart_pourcentage = models.DecimalField(max_digits=8, decimal_places=2)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.DETECTED)
    date_detection = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_detection",)

    def __str__(self):
        return f"{self.type} ({self.severite}) — {self.ecart_pourcentage}%"


class Hypothese(models.Model):
    """Cause PROBABLE générée par l'IA à partir d'une anomalie + son contexte —
    jamais présentée comme confirmée tant qu'un humain ne l'a pas validée.

    `confiance` reste NULL tant qu'aucune estimation fiable n'existe : on
    n'extrait pas un chiffre de confiance depuis du texte libre généré par LLM
    (peu fiable), donc ce champ est rempli par un humain qui évalue l'hypothèse,
    pas automatiquement par hypothesis_service.py.
    """

    class Statut(models.TextChoices):
        PROPOSEE = "PROPOSEE", "Proposée"
        CONFIRMEE = "CONFIRMEE", "Confirmée"
        REJETEE = "REJETEE", "Rejetée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    anomalie = models.ForeignKey(Anomalie, on_delete=models.CASCADE, related_name="hypotheses")
    texte = models.TextField()
    preuves = models.JSONField(default=list, blank=True)
    confiance = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.PROPOSEE)
    genere_par_ia = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_creation",)

    def __str__(self):
        return self.texte[:80]


class MemoireStrategique(models.Model):
    """Diagnostic complet et vérifié : signal -> hypothèse -> action -> résultat.
    C'est cette table, pas les tables individuelles, qui est poussée vers le RAG
    (voir memory_service.py et ai_client.py) : elle seule contient une histoire
    complète et vérifiée, pas juste un fragment isolé."""

    class Statut(models.TextChoices):
        A_VERIFIER = "A_VERIFIER", "À vérifier"
        PARTIELLEMENT_CONFIRMEE = "PARTIELLEMENT_CONFIRMEE", "Partiellement confirmée"
        CONFIRMEE = "CONFIRMEE", "Confirmée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.CASCADE, related_name="memoires_strategiques")
    anomalie = models.ForeignKey(Anomalie, on_delete=models.SET_NULL, null=True, blank=True, related_name="memoires")
    action = models.ForeignKey(Action, on_delete=models.SET_NULL, null=True, blank=True, related_name="memoires")
    titre = models.CharField(max_length=180)
    signal_initial = models.TextField()
    hypothese_texte = models.TextField(blank=True)
    action_texte = models.TextField(blank=True)
    impact_attendu_fcfa = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    impact_mesure_fcfa = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    taux_realisation = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    statut = models.CharField(max_length=30, choices=Statut.choices, default=Statut.A_VERIFIER)
    sources = models.JSONField(default=list, blank=True)
    # Traçabilité de la publication vers le RAG — permet de savoir si cette
    # mémoire est déjà indexée sans requêter le service FastAPI à chaque fois.
    indexee_rag = models.BooleanField(default=False)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_creation",)

    def __str__(self):
        return self.titre


class DocumentEntreprise(models.Model):
    """Document propre à l'organisation (politique interne, rapport, note de
    méthodologie, dossier de subvention...) — distinct du pipeline de factures
    Senelec (app energy). Alimente le RAG au même titre que les mémoires
    stratégiques, mais seulement une fois `valide=True` : un document déposé
    n'est jamais indexé automatiquement sans confirmation humaine.
    """

    class Type(models.TextChoices):
        POLITIQUE = "POLITIQUE", "Politique interne"
        RAPPORT = "RAPPORT", "Rapport"
        METHODOLOGIE = "METHODOLOGIE", "Méthodologie"
        SUBVENTION = "SUBVENTION", "Dossier de subvention"
        AUTRE = "AUTRE", "Autre"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("organizations.Organisation", on_delete=models.CASCADE, related_name="documents_entreprise")
    depose_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents_entreprise_deposes")
    titre = models.CharField(max_length=180)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.AUTRE)
    contenu_texte = models.TextField()
    valide = models.BooleanField(default=False)
    valide_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents_entreprise_valides")
    date_validation = models.DateTimeField(null=True, blank=True)
    indexe_rag = models.BooleanField(default=False)
    date_depot = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-date_depot",)

    def valider(self, utilisateur):
        self.valide = True
        self.valide_par = utilisateur
        self.date_validation = timezone.now()
        self.save(update_fields=("valide", "valide_par", "date_validation"))

    def __str__(self):
        return self.titre

class OpportuniteFinancement(models.Model):
    """Catalogue d'aides/subventions énergie & éco-responsabilité au Sénégal,
    alimenté exclusivement par le workflow n8n d'ingestion. Aucun calcul
    d'éligibilité automatique n'est fait — on affiche les critères et laisse
    la PME juger."""

    class Statut(models.TextChoices):
        ACTIF = "ACTIF", "Actif"
        A_VERIFIER = "A_VERIFIER", "À vérifier"
        EXPIRE = "EXPIRE", "Expiré"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisme = models.CharField(max_length=200)
    titre = models.CharField(max_length=255)
    description = models.TextField()
    montant_max = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    devise = models.CharField(max_length=10, default="FCFA")
    taux_financement_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    criteres_eligibilite = models.TextField()
    secteur = models.CharField(max_length=150, blank=True)
    date_limite = models.DateField(null=True, blank=True)
    url_source = models.URLField(blank=True)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.A_VERIFIER)
    date_ingestion = models.DateTimeField(auto_now=True)
    source_ingestion = models.CharField(max_length=50, default="n8n")

    class Meta:
        ordering = ("date_limite",)

    def __str__(self):
        return self.titre