"""
Consommation d'un compteur PRÉPAYÉ (Woyofal) à partir des soldes relevés et des
recharges — le cas que les métriques « à index » ne pouvaient pas traiter.

Chaque intervalle est ensuite réparti au PRORATA DU TEMPS sur les jours qu'il
couvre. Un jour n'est publié que si les intervalles couvrent au moins 80 % de ses
24 h (seuil `completude_min` du registre). La qualité est « fiable » quand tous
les intervalles font 36 h ou moins, « estimée » sinon (consommation lissée).

Les jours sont découpés en UTC, ce qui coïncide avec l'heure de Dakar (GMT+0).

Les fonctions `calculer_intervalles` et `consommation_jour` sont pures (sans base
de données) : elles sont testables isolément.
"""

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from .baselines import calculer_baseline
from .registry import obtenir_definition

logger = logging.getLogger(__name__)

COMPLETUDE_MIN = 0.80
SEUIL_INTERVALLE_FIABLE_S = timedelta(hours=36).total_seconds()
MARGE_CHARGEMENT = timedelta(days=3)
NB_JOURS_BASELINE = 7
MICRO = Decimal("0.000001")


def debut_de_jour(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


# ----------------------------- fonctions pures -----------------------------

def calculer_intervalles(releves, achats):
    """releves : [(date, solde_kwh)] triés ; achats : [(date, kwh_credites)].

    Retourne (intervalles, nombre_incoherents). Un intervalle est incohérent
    (consommation négative) quand une recharge n'a pas été enregistrée ou qu'un
    solde a été mal saisi : il est écarté plutôt que de fausser la mesure."""
    intervalles, incoherents = [], 0
    for (d1, s1), (d2, s2) in zip(releves, releves[1:]):
        duree = (d2 - d1).total_seconds()
        if duree <= 0:
            continue
        recharges = sum((k for (da, k) in achats if d1 < da <= d2), Decimal("0"))
        conso = s1 + recharges - s2
        if conso < 0:
            incoherents += 1
            continue
        intervalles.append({"debut": d1, "fin": d2, "conso": conso, "duree_s": duree})
    return intervalles, incoherents


def consommation_jour(intervalles, jour):
    """Consommation d'un jour, réparti au prorata du temps. Retourne un dict :
    valeur (Decimal|None), completude (0-1), nombre_intervalles, fiable, suffisante."""
    debut = debut_de_jour(jour)
    fin = debut + timedelta(days=1)
    total, couverture, nb, duree_max = Decimal("0"), 0.0, 0, 0.0
    for it in intervalles:
        chevauchement = (min(it["fin"], fin) - max(it["debut"], debut)).total_seconds()
        if chevauchement <= 0:
            continue
        total += it["conso"] * Decimal(str(chevauchement / it["duree_s"]))
        couverture += chevauchement
        nb += 1
        duree_max = max(duree_max, it["duree_s"])
    completude = round(min(couverture / 86400.0, 1.0), 4)
    return {
        "valeur": total.quantize(MICRO) if nb else None,
        "completude": completude,
        "nombre_intervalles": nb,
        "fiable": nb > 0 and duree_max <= SEUIL_INTERVALLE_FIABLE_S,
        "suffisante": nb > 0 and completude >= COMPLETUDE_MIN,
    }


# ------------------------------ accès base ---------------------------------

def charger_intervalles(compteur, debut, fin):
    """Intervalles du compteur entre `debut` et `fin` (une seule lecture en base
    pour toute la fenêtre, quel que soit le nombre de jours calculés)."""
    from energy.models import AchatWoyofal, ReleveSolde

    d0, d1 = debut - MARGE_CHARGEMENT, fin + MARGE_CHARGEMENT
    releves = [
        (r.date_releve, Decimal(str(r.kwh_restants or 0)))
        for r in ReleveSolde.objects.filter(compteur=compteur, date_releve__gte=d0, date_releve__lte=d1).order_by("date_releve")
    ]
    achats = [
        (a.date_achat, Decimal(str(a.kwh_credites or 0)))
        for a in AchatWoyofal.objects.filter(compteur=compteur, date_achat__gte=d0, date_achat__lte=d1)
    ]
    intervalles, _ = calculer_intervalles(releves, achats)
    return intervalles


def est_compteur_woyofal(compteur) -> bool:
    from energy.models import ReleveSolde
    return ReleveSolde.objects.filter(compteur=compteur).exists()


def _persister(organisation, compteur, definition, debut, fin, valeur, completude, fiable, baseline=None):
    from analysis.models import ResultatMetrique

    baseline = baseline or {}
    resultat, _ = ResultatMetrique.objects.update_or_create(
        organisation=organisation,
        compteur=compteur,
        code_metrique=definition.code,
        periode_debut=debut,
        periode_fin=fin,
        version_metrique=definition.version,
        defaults={
            "valeur": valeur,
            "unite": definition.unite,
            "baseline_type": baseline.get("type", ""),
            "baseline_valeur": baseline.get("valeur"),
            "baseline_nombre_observations": baseline.get("nombre_observations", 0),
            "completude": Decimal(str(completude)),
            "statut_qualite": ResultatMetrique.StatutQualite.FIABLE if fiable else ResultatMetrique.StatutQualite.ESTIME,
            "confiance": Decimal(str(completude)),
            "sources": list(definition.sources_requises),
            "limites": list(definition.limites),
        },
    )
    return resultat


def publier_consommation_jour(organisation, compteur, jour, intervalles=None):
    """Calcule et persiste `consommation_journaliere_woyofal`. None si les
    relevés ne couvrent pas assez le jour (donnée insuffisante : rien n'est publié)."""
    definition = obtenir_definition("consommation_journaliere_woyofal")
    debut = debut_de_jour(jour)
    fin = debut + timedelta(days=1)
    if intervalles is None:
        intervalles = charger_intervalles(compteur, debut, fin)
    jr = consommation_jour(intervalles, debut)
    if not jr["suffisante"]:
        return None
    return _persister(organisation, compteur, definition, debut, fin, jr["valeur"], jr["completude"], jr["fiable"])


def publier_variation(organisation, compteur, jour):
    """`variation_woyofal_vs_moyenne_recente` : jour observé vs moyenne des jours
    précédents suffisamment couverts. None si donnée insuffisante."""
    from decimal import InvalidOperation

    definition = obtenir_definition("variation_woyofal_vs_moyenne_recente")
    debut = debut_de_jour(jour)
    fin = debut + timedelta(days=1)
    nb_jours = definition.baseline.parametres.get("nombre_jours", NB_JOURS_BASELINE)

    intervalles = charger_intervalles(compteur, debut - timedelta(days=nb_jours), fin)
    observation = publier_consommation_jour(organisation, compteur, debut, intervalles)
    if observation is None or observation.valeur is None:
        return None

    references = []
    for i in range(1, nb_jours + 1):
        jr = consommation_jour(intervalles, debut - timedelta(days=i))
        if jr["suffisante"]:
            references.append(float(jr["valeur"]))

    baseline = calculer_baseline(definition.baseline.type, references)
    if baseline["valeur"] is None or baseline["nombre_observations"] < definition.qualite_requise.nombre_observations_min:
        return None

    observe = float(observation.valeur)
    ref = baseline["valeur"]
    if ref == 0:
        variation = 0.0 if observe == 0 else 100.0
    else:
        variation = (observe - ref) / ref * 100

    try:
        return _persister(
            organisation, compteur, definition, debut, fin,
            Decimal(str(round(variation, 2))),
            float(observation.completude),
            observation.statut_qualite == "FIABLE",
            baseline={"type": baseline["type"], "valeur": Decimal(str(round(ref, 6))),
                      "nombre_observations": baseline["nombre_observations"]},
        )
    except InvalidOperation:
        logger.exception("Variation Woyofal non enregistrable pour le compteur %s.", compteur)
        return None
