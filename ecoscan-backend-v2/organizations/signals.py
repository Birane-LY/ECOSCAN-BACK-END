import logging
from datetime import timedelta

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from organizations.models import Organisation

logger = logging.getLogger(__name__)

OBJECTIFS_PAR_DEFAUT = [
    {
        "nom": "Réduire la facture énergétique",
        "description": "Objectif par défaut : réduire les dépenses liées à la consommation d'électricité (Senelec).",
        "type": "reduction_cout",
        "valeur_cible": 10,
        "unite": "FCFA",
    },
    {
        "nom": "Réduire la consommation d'électricité",
        "description": "Objectif par défaut : réduire la consommation en kWh sur l'ensemble des compteurs.",
        "type": "reduction_energie",
        "valeur_cible": 10,
        "unite": "kWh",
    },
]


@receiver(post_save, sender=Organisation)
def creer_objectifs_par_defaut(sender, instance, created, **kwargs):
    """Crée 2 objectifs de départ pour toute nouvelle organisation.
    Idempotent : si des objectifs existent déjà, on ne touche à rien.
    """
    if not created:
        return
    from energy.models import Objectif  # import différé pour éviter les imports circulaires

    # Si l'organisation possède déjà au moins un objectif ACTIF, on ne touche à rien
    if Objectif.objects.filter(organisation=instance, statut=Objectif.Statut.ACTIF).exists():
        return

    debut = timezone.now()
    fin = debut + timedelta(days=365)

    for data in OBJECTIFS_PAR_DEFAUT:
        try:
            Objectif.objects.create(
                organisation=instance,
                nom=data["nom"],
                description=data["description"],
                type=data["type"],
                valeur_cible=data["valeur_cible"],
                unite=data["unite"],
                date_debut=debut,
                date_fin=fin,
                statut=Objectif.Statut.ACTIF,
            )
        except Exception:
            logger.exception(
                "Impossible de créer l'objectif par défaut '%s' pour l'organisation %s.",
                data["nom"],
                instance.id,
            )