"""
Détection d'anomalies — purement déterministe, aucune IA ici.

Les seuils (10/20/40 %) viennent du document de justification du projet. Ce sont
des points de départ, PAS des valeurs calibrées par organisation (personnalisation
non implémentée : nécessiterait un modèle de configuration par organisation).
"""

from decimal import Decimal
from typing import Optional

from analysis.models import Anomalie, ResultatMetrique

SEUIL_SURVEILLANCE = Decimal("10.0")
SEUIL_ALERTE = Decimal("20.0")
SEUIL_INVESTIGATION_PRIORITAIRE = Decimal("40.0")

CODES_METRIQUES_COMPATIBLES = (
    "variation_vs_baseline",
    "variation_facture_vs_facture_precedente",
    "variation_woyofal_vs_moyenne_recente",
    "variation_woyofal_rituelle_vs_moyenne_recente",
)


def _classer_severite(ecart_absolu: Decimal) -> Optional[str]:
    """None si l'écart est dans la variation normale (< 10 %) : aucune anomalie ne
    doit alors être créée, sinon les vraies anomalies seraient noyées dans le bruit."""
    if ecart_absolu < SEUIL_SURVEILLANCE:
        return None
    if ecart_absolu < SEUIL_ALERTE:
        return Anomalie.Severite.SURVEILLANCE
    if ecart_absolu < SEUIL_INVESTIGATION_PRIORITAIRE:
        return Anomalie.Severite.ALERTE
    return Anomalie.Severite.INVESTIGATION_PRIORITAIRE


def detecter_anomalie(resultat_variation: ResultatMetrique) -> Optional[Anomalie]:
    """Crée/met à jour l'Anomalie correspondant à un ResultatMetrique de variation
    si l'écart dépasse le seuil de surveillance."""
    if resultat_variation.code_metrique not in CODES_METRIQUES_COMPATIBLES:
        raise ValueError(
            f"detecter_anomalie attend un ResultatMetrique parmi {CODES_METRIQUES_COMPATIBLES}, "
            f"reçu '{resultat_variation.code_metrique}'."
        )
    if resultat_variation.valeur is None:
        return None

    ecart_pourcentage = resultat_variation.valeur
    severite = _classer_severite(abs(ecart_pourcentage))
    if severite is None:
        return None

    valeur_attendue = resultat_variation.baseline_valeur
    valeur_observee = None
    if valeur_attendue is not None:
        valeur_observee = valeur_attendue * (Decimal("1") + ecart_pourcentage / Decimal("100"))

    champs = {
        "type": "consumption_spike" if ecart_pourcentage > 0 else "consumption_drop",
        "severite": severite,
        "valeur_observee": valeur_observee,
        "valeur_attendue": valeur_attendue,
        "ecart_pourcentage": ecart_pourcentage,
    }

    anomalie = Anomalie.objects.filter(
        organisation=resultat_variation.organisation, resultat_metrique=resultat_variation
    ).first()
    if anomalie is None:
        return Anomalie.objects.create(
            organisation=resultat_variation.organisation,
            resultat_metrique=resultat_variation,
            statut=Anomalie.Statut.DETECTED,
            **champs,
        )

    for cle, valeur in champs.items():
        setattr(anomalie, cle, valeur)
    anomalie.save(update_fields=tuple(champs))
    return anomalie
