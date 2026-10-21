"""
Variation facture vs facture précédente -> anomalie -> hypothèse.

Sorti de views.py pour être réutilisable (import d'une facture, recalcul de
rattrapage, commande de gestion) sans import circulaire.

Gestion de la première facture :
  Une première facture n'est comparée à un seuil tarifaire que si son compteur
  porte une catégorie explicitement configurée et qu'un barème correspondant
  est actif. Aucune catégorie ni valeur domestique n'est supposée.
"""

import logging
from decimal import Decimal

from analysis.models import ResultatMetrique
from django.utils import timezone
from energy.services.prediction_tranches import bareme_actuel
from .anomaly import detecter_anomalie
from .context import obtenir_contexte
from .hypothesis import generer_hypothese
from .registry import obtenir_definition

logger = logging.getLogger(__name__)


ECART_MAX_JOURS = 75

def _analyser_premiere_facture(resultat_actuel: ResultatMetrique):
    """Compare une première facture au seuil haut du barème déclaré, s'il existe."""
    compteur = resultat_actuel.compteur
    if resultat_actuel.valeur is None or compteur is None:
        return None

    categorie = getattr(compteur, "categorie_tarifaire", "")
    mode_paiement = getattr(compteur, "mode_paiement", "")
    if mode_paiement != "POSTPAYE":
        return None
    date_barreme = timezone.localdate(resultat_actuel.periode_fin)
    tranches = bareme_actuel(
        categorie,
        mode_paiement,
        a_la_date=date_barreme,
    )
    if len(tranches) < 2 or tranches[-1].kwh_min <= 0:
        return None

    nb_jours = max(
        1,
        (resultat_actuel.periode_fin.date() - resultat_actuel.periode_debut.date()).days + 1,
    )
    if nb_jours != 30:
        logger.info(
            "Première facture org %s : seuil tarifaire ignoré pour une période de %s jours; "
            "la règle de proratisation n'est pas confirmée.",
            resultat_actuel.organisation_id,
            nb_jours,
        )
        return None
    seuil = tranches[-1].kwh_min
    conso = resultat_actuel.valeur

    if conso <= seuil:
        logger.debug(
            "Première facture org %s : consommation %s kWh ≤ seuil tarifaire %s kWh → pas d'anomalie.",
            resultat_actuel.organisation_id,
            conso,
            seuil,
        )
        return None

    # Le tarif et la catégorie proviennent du barème explicitement affecté au compteur.
    depassement_pct = ((conso - seuil) / seuil * 100).quantize(Decimal("0.01"))
    logger.info(
        "Première facture org %s : conso %s kWh → dépassement du seuil tarifaire de %s %%.",
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
            "baseline_type": "seuil_tranche_configure",
            "baseline_valeur": seuil,
            "baseline_nombre_observations": 0,
            "completude": Decimal("0.70"),
            "statut_qualite": ResultatMetrique.StatutQualite.ESTIME,
            "confiance": Decimal("0.70"),
            "sources": list(definition.sources_requises),
            "limites": [
                f"Première facture : comparaison au seuil du barème postpayé {categorie} "
                f"({seuil} kWh sur une période de 30 jours), aucune facture précédente disponible."
            ],
        },
    )
    return variation_resultat


def calculer_variation_facture(resultat_actuel: ResultatMetrique):
    """Compare la facture à la précédente du même compteur (ou, à défaut de
    compteur, de la même organisation).

    Si aucune facture précédente n'existe (premier import), délègue à
    _analyser_premiere_facture(), qui ne compare qu'à un barème explicitement
    affecté au compteur.
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
