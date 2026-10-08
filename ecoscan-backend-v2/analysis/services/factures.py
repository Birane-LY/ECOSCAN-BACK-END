"""
Variation facture vs facture précédente -> anomalie -> hypothèse.

Sorti de views.py pour être réutilisable (import d'une facture, recalcul de
rattrapage, commande de gestion) sans import circulaire.

Gestion de la première facture :
  Lorsqu'il n'y a pas de facture précédente, la consommation est comparée au
  seuil Senelec Tranche 3 (500 kWh proratés sur 60 jours). Si la PME dépasse
  ce seuil, un résultat de variation symbolique est créé pour déclencher
  l'analyse anomalie/hypothèse/recommandation.
"""

import logging
from decimal import Decimal

from analysis.models import ResultatMetrique
from .anomaly import detecter_anomalie
from .context import obtenir_contexte
from .hypothesis import generer_hypothese
from .registry import obtenir_definition

logger = logging.getLogger(__name__)


ECART_MAX_JOURS = 75

# Seuil Senelec Tranche 3 pour 60 jours (tarif PMP / PPP)
# Au-delà de 500 kWh/60j la PME paye le tarif le plus cher.
_SEUIL_T3_60J = Decimal("500")


def _analyser_premiere_facture(resultat_actuel: ResultatMetrique):
    """Sur la 1ʳᵉ facture d'une organisation, compare la consommation au seuil
    Senelec Tranche 3 (proraté selon le nombre de jours réel) plutôt qu'à une
    facture précédente. Retourne un ResultatMetrique de variation symbolique si
    la consommation est en Tranche 3, None sinon."""
    if resultat_actuel.valeur is None:
        return None

    conso = resultat_actuel.valeur

    # Prorata sur le nombre de jours réel si disponible
    nb_jours = None
    try:
        if resultat_actuel.periode_debut and resultat_actuel.periode_fin:
            nb_jours = (resultat_actuel.periode_fin - resultat_actuel.periode_debut).days
    except Exception:
        pass
    seuil = _SEUIL_T3_60J * Decimal(str(nb_jours or 60)) / Decimal("60")

    if conso <= seuil:
        logger.debug(
            "Première facture org %s : consommation %s kWh ≤ seuil T3 %s kWh → pas d'anomalie.",
            resultat_actuel.organisation_id,
            conso,
            seuil,
        )
        return None

    # Dépassement T3 : on exprime l'écart en % par rapport au seuil T3
    depassement_pct = ((conso - seuil) / seuil * 100).quantize(Decimal("0.01"))
    logger.info(
        "Première facture org %s : conso %s kWh → dépassement T3 de %s %%.",
        resultat_actuel.organisation_id,
        conso,
        depassement_pct,
    )

    definition = obtenir_definition("variation_facture_vs_facture_precedente")
    variation_resultat, created = ResultatMetrique.objects.update_or_create(
        organisation=resultat_actuel.organisation,
        compteur=resultat_actuel.compteur,
        code_metrique=definition.code,
        periode_debut=resultat_actuel.periode_debut,
        periode_fin=resultat_actuel.periode_fin,
        version_metrique=definition.version,
        defaults={
            "valeur": depassement_pct,
            "unite": definition.unite,
            "baseline_type": "seuil_tranche3_senelec",
            "baseline_valeur": seuil,
            "baseline_nombre_observations": 0,
            "completude": Decimal("0.70"),
            "statut_qualite": ResultatMetrique.StatutQualite.ESTIME,
            "confiance": Decimal("0.70"),
            "sources": list(definition.sources_requises),
            "limites": [
                f"Première facture : comparaison au seuil Senelec Tranche 3 "
                f"({seuil} kWh/{nb_jours or 60} j), aucune facture précédente disponible."
            ],
        },
    )
    return variation_resultat


def calculer_variation_facture(resultat_actuel: ResultatMetrique):
    """Compare la facture à la précédente du même compteur (ou, à défaut de
    compteur, de la même organisation).

    Si aucune facture précédente n'existe (premier import), délègue à
    _analyser_premiere_facture() qui compare au seuil Senelec T3.
    Retourne None si la consommation est jugée acceptable."""
    precedent = (
        ResultatMetrique.objects.filter(
            organisation=resultat_actuel.organisation,
            compteur=resultat_actuel.compteur,
            code_metrique="consommation_facture_periodique",
            periode_fin__lt=resultat_actuel.periode_fin,
        )
        .exclude(id=resultat_actuel.id)
        .order_by("-periode_fin")
        .first()
    )

    # ── Première facture : pas de référence historique ──────────────────────
    if precedent is None or precedent.valeur is None or precedent.valeur == 0:
        return _analyser_premiere_facture(resultat_actuel)

    if resultat_actuel.valeur is None:
        return None
    if (resultat_actuel.periode_fin - precedent.periode_fin).days > ECART_MAX_JOURS:
        return None

    # ── Factures suivantes : variation réelle ───────────────────────────────
    variation_pct = ((resultat_actuel.valeur - precedent.valeur) / precedent.valeur) * 100
    definition = obtenir_definition("variation_facture_vs_facture_precedente")
    variation_resultat, _ = ResultatMetrique.objects.update_or_create(
        organisation=resultat_actuel.organisation,
        compteur=resultat_actuel.compteur,
        code_metrique=definition.code,
        periode_debut=precedent.periode_fin,
        periode_fin=resultat_actuel.periode_fin,
        version_metrique=definition.version,
        defaults={
            "valeur": Decimal(str(round(variation_pct, 2))),
            "unite": definition.unite,
            "baseline_type": "facture_precedente",
            "baseline_valeur": precedent.valeur,
            "baseline_nombre_observations": 1,
            "completude": Decimal("1.0"),
            "statut_qualite": ResultatMetrique.StatutQualite.FIABLE,
            "confiance": Decimal("1.0"),
            "sources": list(definition.sources_requises),
            "limites": list(definition.limites),
        },
    )
    return variation_resultat


def analyser_facture(resultat_actuel: ResultatMetrique):
    """Variation -> anomalie -> hypothèse -> recommandation auto. Idempotent."""
    variation = calculer_variation_facture(resultat_actuel)
    if variation is None:
        return None
    anomalie = detecter_anomalie(variation)
    if anomalie is None:
        return None
    generer_hypothese(anomalie, obtenir_contexte(anomalie))

    # Recommandation automatique : l'utilisateur n'a qu'à accepter ou rejeter
    from .auto_recommandation import generer_recommandation_auto
    generer_recommandation_auto(anomalie)

    return anomalie


def analyser_facture_par_id(resultat_id) -> None:
    """Tâche d'arrière-plan : recharge la facture puis rejoue l'analyse."""
    resultat = ResultatMetrique.objects.filter(id=resultat_id).first()
    if resultat is not None and resultat.valeur is not None:
        analyser_facture(resultat)


def recalculer_variations_factures(organisation) -> int:
    """Rattrapage : rejoue l'analyse sur toutes les factures de l'organisation
    (utile pour les factures importées avant l'existence de ce code, ou dans un
    autre ordre chronologique). Retourne le nombre d'anomalies détectées."""
    factures = ResultatMetrique.objects.filter(
        organisation=organisation,
        code_metrique="consommation_facture_periodique",
        valeur__isnull=False,
    ).order_by("periode_fin")
    nb = 0
    for facture in factures:
        try:
            if analyser_facture(facture) is not None:
                nb += 1
        except Exception:
            logger.exception("Analyse impossible pour la facture %s.", facture.id)
    return nb
