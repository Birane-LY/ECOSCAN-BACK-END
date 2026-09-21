import uuid

from django.conf import settings
from django.db import models


class FichierSource(models.Model):
    """Enregistre les métadonnées et le fichier physique téléversé pour l'analyse énergétique."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        "organizations.Organisation",
        on_delete=models.CASCADE,
        related_name="fichiers_sources",
    )
    nom = models.CharField(max_length=255)
    fichier = models.FileField(upload_to="fichiers-sources/%Y/%m/%d/")
    chemin_stockage = models.CharField(max_length=500, blank=True)
    mime_type = models.CharField(max_length=120)
    taille_octets = models.PositiveBigIntegerField(default=0)
    hash = models.CharField(max_length=128)
    texte_brut_cache = models.TextField(blank=True, default="")
    date_depot = models.DateTimeField(auto_now_add=True)
    depose_par = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="fichiers_sources_deposes",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("hash", "organisation"), name="fichier_source_hash_organisation_unique"),
        ]

    def __str__(self):
        return self.nom


class SourceDonnee(models.Model):
    """Représente l'origine ou le canal d'acquisition des flux de données énergétiques."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        "organizations.Organisation",
        on_delete=models.CASCADE,
        related_name="sources_donnees",
    )
    nom = models.CharField(max_length=150)
    type = models.CharField(max_length=80)
    origine = models.CharField(max_length=180)
    frequence = models.CharField(max_length=80)
    statut_synchronisation = models.CharField(max_length=50)
    derniere_synchronisation = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.nom


class ImportDonnees(models.Model):
    """Pilote le cycle de traitement et d'intégration d'un fichier source de consommation."""

    class Statut(models.TextChoices):
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        EN_COURS = "EN_COURS", "En cours"
        HORS_PERIMETRE = "HORS_PERIMETRE", "Hors périmètre"
        REVUE_REQUISE = "REVUE_REQUISE", "Revue requise"
        INCOHERENT = "INCOHERENT", "Incohérent"
        TERMINE = "TERMINE", "Terminé"
        ECHOUE = "ECHOUE", "Échoué"
        ANNULE = "ANNULE", "Annulé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fichier_source = models.OneToOneField(
        FichierSource,
        on_delete=models.CASCADE,
        related_name="import_donnees",
    )
    organisation = models.ForeignKey(
        "organizations.Organisation",
        on_delete=models.CASCADE,
        related_name="imports_donnees",
    )
    source_donnee = models.ForeignKey(
        SourceDonnee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="imports_donnees",
    )
    lance_par = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="imports_lances",
    )
    nom_fichier = models.CharField(max_length=255)
    format = models.CharField(max_length=50)
    type_donnees = models.CharField(max_length=100)
    nombre_lignes = models.PositiveIntegerField(default=0)
    nombre_erreurs = models.PositiveIntegerField(default=0)
    score_qualite = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.EN_ATTENTE)
    date_import = models.DateTimeField(auto_now_add=True)
    ocr_statut = models.CharField(max_length=30, blank=True, default="")
    ocr_erreur = models.TextField(blank=True, default="")
    score_lisibilite = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    score_pertinence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    donnees_extraites = models.JSONField(default=dict, blank=True)
    rapport_analyse = models.JSONField(default=dict, blank=True)
    date_traitement = models.DateTimeField(null=True, blank=True)
    compteur = models.ForeignKey(
        "organizations.Compteur", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="imports_donnees",
        help_text="Compteur concerné par cet import, si connu — optionnel pour un import OCR classique sans sélection explicite.",
    )

    def lancer_import(self):
        """Déclenche le passage de l'import à l'état en cours de traitement."""
        self.statut = self.Statut.EN_COURS
        self.save(update_fields=("statut",))

    def annuler_import(self):
        """Annule le traitement de l'import."""
        self.statut = self.Statut.ANNULE
        self.save(update_fields=("statut",))

    def __str__(self):
        return self.nom_fichier


class FacteurEmission(models.Model):
    """Stocke les coefficients réglementaires permettant de convertir l'énergie en CO2 équivalent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nom = models.CharField(max_length=150)
    type_energie = models.CharField(max_length=80)
    valeur = models.DecimalField(max_digits=14, decimal_places=6)
    unite = models.CharField(max_length=50)
    source_reglementaire = models.CharField(max_length=180)
    version = models.DateTimeField()

    def __str__(self):
        return self.nom


class DonneeEnergetique(models.Model):
    """Enregistre les index de consommation brute relevés ou importés pour un compteur."""

    class StatutValidation(models.TextChoices):
        EN_ATTENTE = "EN_ATTENTE", "En attente"
        VALIDEE = "VALIDEE", "Validée"
        REJETEE = "REJETEE", "Rejetée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    compteur = models.ForeignKey(
        "organizations.Compteur",
        on_delete=models.CASCADE,
        related_name="donnees_energetiques",
    )
    source_donnee = models.ForeignKey(
        SourceDonnee,
        on_delete=models.PROTECT,
        related_name="donnees_energetiques",
    )
    facteur_emission = models.ForeignKey(
        FacteurEmission,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="donnees_converties",
    )
    valeur = models.DecimalField(max_digits=18, decimal_places=6)
    unite = models.CharField(max_length=30)
    periode_debut = models.DateTimeField()
    periode_fin = models.DateTimeField()
    statut_validation = models.CharField(
        max_length=20,
        choices=StatutValidation.choices,
        default=StatutValidation.EN_ATTENTE,
    )
    source = models.CharField(max_length=150)

    class Meta:
        ordering = ("-periode_debut",)

    def __str__(self):
        return f"{self.compteur.reference} : {self.valeur} {self.unite}"


class HistoriquePerformance(models.Model):
    """Consolide périodiquement les consommations, économies et émissions liées à un projet."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fiche_projet = models.ForeignKey(
        "organizations.FicheProjet",
        on_delete=models.CASCADE,
        related_name="historiques_performance",
    )
    periode = models.DateTimeField()
    consommation = models.DecimalField(max_digits=18, decimal_places=6)
    emissions = models.DecimalField(max_digits=18, decimal_places=6)
    economie = models.DecimalField(max_digits=18, decimal_places=6)
    unite = models.CharField(max_length=30)

    class Meta:
        ordering = ("-periode",)


class SyntheseFinanciere(models.Model):
    """Associe un bilan et des calculs de ROI à une fiche projet spécifique."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fiche_projet = models.OneToOneField(
        "organizations.FicheProjet",
        on_delete=models.CASCADE,
        related_name="synthese_financiere",
    )
    cout_energie = models.DecimalField(max_digits=18, decimal_places=2)
    economie_estimee = models.DecimalField(max_digits=18, decimal_places=2)
    economie_realisee = models.DecimalField(max_digits=18, decimal_places=2)
    retour_investissement = models.DecimalField(max_digits=8, decimal_places=2)
    date_calcul = models.DateTimeField()


class Objectif(models.Model):
    """Définit les cibles de réduction d'empreinte ou de consommation d'une organisation."""

    class Statut(models.TextChoices):
        BROUILLON = "BROUILLON", "Brouillon"
        ACTIF = "ACTIF", "Actif"
        ATTEINT = "ATTEINT", "Atteint"
        ABANDONNE = "ABANDONNE", "Abandonné"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(
        "organizations.Organisation",
        on_delete=models.CASCADE,
        related_name="objectifs",
    )
    nom = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    type = models.CharField(max_length=80)
    valeur_cible = models.DecimalField(max_digits=18, decimal_places=6)
    unite = models.CharField(max_length=30)
    date_debut = models.DateTimeField()
    date_fin = models.DateTimeField()
    progression_actuelle = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    prevision = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.BROUILLON)

    def mettre_a_jour_progression(self, progression):
        """Met à jour le niveau actuel de la progression et bascule le statut si le but est atteint."""
        self.progression_actuelle = progression
        if progression >= self.valeur_cible:
            self.statut = self.Statut.ATTEINT
        self.save(update_fields=("progression_actuelle", "statut"))

    def __str__(self):
        return self.nom


class Indicateur(models.Model):
    """Modélise une métrique de calcul ou un indicateur clé de performance (KPI)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    objectif = models.ForeignKey(Objectif, on_delete=models.CASCADE, related_name="indicateurs")
    nom = models.CharField(max_length=150)
    type = models.CharField(max_length=80)
    valeur = models.DecimalField(max_digits=18, decimal_places=6)
    unite = models.CharField(max_length=30)
    periode = models.DateTimeField()
    methode_calcul = models.TextField()
    date_calcul = models.DateTimeField()

    def __str__(self):
        return self.nom


class IndicateurObjectif(models.Model):
    """Consolide la progression et compare l'état actuel face aux jalons cibles de l'objectif."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    objectif = models.ForeignKey(Objectif, on_delete=models.CASCADE, related_name="indicateurs_objectifs")
    indicateurs = models.ManyToManyField(Indicateur, related_name="objectifs_associes", blank=True)
    nom = models.CharField(max_length=150)
    unite = models.CharField(max_length=30)
    valeur_initiale = models.DecimalField(max_digits=18, decimal_places=6)
    valeur_cible = models.DecimalField(max_digits=18, decimal_places=6)
    valeur_actuelle = models.DecimalField(max_digits=18, decimal_places=6)
    progression = models.DecimalField(max_digits=8, decimal_places=2)

    def __str__(self):
        return self.nom

class BaremeTarifaire(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    categorie = models.CharField(max_length=50, default="DOMESTIQUE_PETITE_PUISSANCE")
    nom_tranche = models.CharField(max_length=80)
    ordre = models.PositiveIntegerField()
    kwh_min = models.DecimalField(max_digits=10, decimal_places=2)
    kwh_max = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    prix_fcfa_par_kwh = models.DecimalField(max_digits=10, decimal_places=4)
    date_entree_vigueur = models.DateField()
    date_fin_vigueur = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ("categorie", "ordre")


class AchatWoyofal(models.Model):
    class Source(models.TextChoices):
        SAISIE = "SAISIE", "Saisie"
        SMS = "SMS", "SMS"
        PHOTO = "PHOTO", "Photo"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.UUIDField(unique=True)  # généré par le mobile : synchro idempotente
    compteur = models.ForeignKey("organizations.Compteur", on_delete=models.CASCADE, related_name="achats_woyofal")
    montant_fcfa = models.DecimalField(max_digits=12, decimal_places=2)
    kwh_credites = models.DecimalField(max_digits=10, decimal_places=3)
    kwh_predits = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    date_achat = models.DateTimeField()
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.SAISIE)

    class Meta:
        ordering = ("-date_achat",)

class ReleveSolde(models.Model):
    """Solde affiché par le compteur Woyofal à un instant donné (saisi par l'utilisateur)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.UUIDField(unique=True)  # généré par le mobile : synchro idempotente
    compteur = models.ForeignKey("organizations.Compteur", on_delete=models.CASCADE, related_name="releves_solde")
    kwh_restants = models.DecimalField(max_digits=10, decimal_places=3)
    date_releve = models.DateTimeField()

    class Meta:
        ordering = ("-date_releve",)