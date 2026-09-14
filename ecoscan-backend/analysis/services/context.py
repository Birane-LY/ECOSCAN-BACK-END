"""
Relie une anomalie à son contexte opérationnel — uniquement des observations
VALIDÉES (jamais une note brute non confirmée), dans une fenêtre temporelle
raisonnable autour de la détection.
"""

from datetime import timedelta

from analysis.models import Anomalie, ObservationOperationnelle

FENETRE_CONTEXTE = timedelta(hours=6)


def obtenir_contexte(anomalie: Anomalie) -> list:
    """Retourne les observations opérationnelles validées de l'organisation dans
    une fenêtre de ±6h autour de la détection de l'anomalie.

    La fenêtre est volontairement large (pas juste "le même créneau exact") car
    une cause opérationnelle (redémarrage de machines, panne) peut précéder le
    pic mesuré de plusieurs heures.
    """
    if anomalie.resultat_metrique is None:
        return []

    centre = anomalie.resultat_metrique.periode_fin
    debut = centre - FENETRE_CONTEXTE
    fin = centre + FENETRE_CONTEXTE

    return list(
        ObservationOperationnelle.objects.filter(
            organisation=anomalie.organisation,
            valide=True,
            date_observation__gte=debut,
            date_observation__lte=fin,
        ).order_by("date_observation")
    )