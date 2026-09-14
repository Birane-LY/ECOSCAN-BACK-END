"""
Détection d'anomalies — purement déterministe, aucune IA ici.

Les seuils (10/20/40 %) viennent du document de justification partagé pour ce
projet. Ce sont des points de départ, PAS des valeurs calibrées par
organisation — le document lui-même le dit : "Ces seuils doivent ensuite être
personnalisés selon l'organisation." Personnalisation non implémentée ici
(nécessiterait un modèle de configuration par organisation, hors scope de cette
passe) — à ajouter si le besoin se confirme en usage réel.
"""

from decimal import Decimal
from typing import Optional

from analysis.models import Anomalie, ResultatMetrique

SEUIL_SURVEILLANCE = Decimal("10.0")
SEUIL_ALERTE = Decimal("20.0")
SEUIL_INVESTIGATION_PRIORITAIRE = Decimal("40.0")


def _classer_severite(ecart_absolu: Decimal) -> Optional[str]:
    """Retourne None si l'écart est dans la variation normale (< 10 %) — dans ce
    cas, AUCUNE anomalie ne doit être créée : une variation normale n'est pas un
    signal, la créer quand même noierait les vraies anomalies dans le bruit."""
    if ecart_absolu < SEUIL_SURVEILLANCE:
        return None
    if ecart_absolu < SEUIL_ALERTE:
        return Anomalie.Severite.SURVEILLANCE
    if ecart_absolu < SEUIL_INVESTIGATION_PRIORITAIRE:
        return Anomalie.Severite.ALERTE
    return Anomalie.Severite.INVESTIGATION_PRIORITAIRE


def detecter_anomalie(resultat_variation: ResultatMetrique) -> Optional[Anomalie]:
    """Analyse un ResultatMetrique de type 'variation_vs_baseline' et crée (ou met
    à jour) l'Anomalie correspondante si l'écart dépasse le seuil de surveillance.

    Idempotent par construction (update_or_create sur resultat_metrique) :
    relancer la détection sur le même résultat ne crée jamais de doublon.
    """
    if resultat_variation.code_metrique != "variation_vs_baseline":
        raise ValueError(
            f"detecter_anomalie attend un ResultatMetrique 'variation_vs_baseline', "
            f"reçu '{resultat_variation.code_metrique}'."
        )
    if resultat_variation.valeur is None:
        return None  # métrique insuffisante (déjà signalé en amont par MetricsService)

    ecart_pourcentage = resultat_variation.valeur
    severite = _classer_severite(abs(ecart_pourcentage))
    if severite is None:
        return None

    # La consommation observée n'est pas stockée séparément sur ResultatMetrique
    # (seul le pourcentage de variation l'est) — elle se déduit de la baseline :
    # observee = baseline * (1 + variation / 100).
    valeur_attendue = resultat_variation.baseline_valeur
    valeur_observee = None
    if valeur_attendue is not None:
        valeur_observee = valeur_attendue * (Decimal("1") + ecart_pourcentage / Decimal("100"))

    type_anomalie = "consumption_spike" if ecart_pourcentage > 0 else "consumption_drop"

    anomalie, _cree = Anomalie.objects.update_or_create(
        organisation=resultat_variation.organisation,
        resultat_metrique=resultat_variation,
        defaults={
            "type": type_anomalie,
            "severite": severite,
            "valeur_observee": valeur_observee,
            "valeur_attendue": valeur_attendue,
            "ecart_pourcentage": ecart_pourcentage,
            "statut": Anomalie.Statut.DETECTED,
        },
    )
    return anomalie