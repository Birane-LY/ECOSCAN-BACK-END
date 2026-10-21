"""
Relie une anomalie à son contexte opérationnel — uniquement des observations
VALIDÉES (jamais une note brute non confirmée), dans une fenêtre temporelle
raisonnable autour de la détection.
"""

from datetime import timedelta
from types import SimpleNamespace

from analysis.models import Anomalie, ObservationOperationnelle

FENETRE_CONTEXTE = timedelta(hours=6)


def obtenir_contexte(anomalie: Anomalie) -> list:
    """Retourne le contexte de terrain autour de l'anomalie avec sa provenance.

    La fenêtre est volontairement large (pas juste "le même créneau exact") car
    une cause opérationnelle (redémarrage de machines, panne) peut précéder le
    pic mesuré de plusieurs heures. Les observations validées et les relevés
    rituels déclaratifs restent explicitement distingués.
    """
    if anomalie.resultat_metrique is None:
        return []

    centre = anomalie.resultat_metrique.periode_fin
    debut = centre - FENETRE_CONTEXTE
    fin = centre + FENETRE_CONTEXTE

    observations = list(
        ObservationOperationnelle.objects.filter(
            organisation=anomalie.organisation,
            valide=True,
            date_observation__gte=debut,
            date_observation__lte=fin,
        ).order_by("date_observation")
    )[:20]
    contexte = [
        SimpleNamespace(
            texte=f"[OBSERVATION TERRAIN VALIDÉE] {observation.texte[:1000]}"
        )
        for observation in observations
    ]

    from energy.models import ReleveRituelEnergetique

    releves_rituels = ReleveRituelEnergetique.objects.filter(
        point_suivi__organisation=anomalie.organisation,
        observe_le__gte=debut,
        observe_le__lte=fin,
    ).select_related("point_suivi").order_by("observe_le")
    for releve in releves_rituels[:20]:
        note = f" Note déclarée : {releve.note}" if releve.note else ""
        contexte.append(
            SimpleNamespace(
                texte=(
                    "[RITUEL ÉNERGÉTIQUE DÉCLARATIF, NON VÉRIFIÉ] "
                    f"{releve.point_suivi.nom}, {releve.date_releve} "
                    f"{releve.creneau} : {releve.valeur_kwh} kWh.{note}"
                )
            )
        )
    return contexte