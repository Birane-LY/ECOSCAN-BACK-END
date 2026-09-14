"""
Stratégies déterministes de calcul des baselines de consommation.
"""

from statistics import mean
from typing import Callable, Sequence


def _baseline_moyenne_simple(valeurs: Sequence[float], **_params) -> dict:
    if not valeurs:
        return {"valeur": None, "nombre_observations": 0}
    return {"valeur": mean(valeurs), "nombre_observations": len(valeurs)}


def _baseline_moyenne_jour_semaine(valeurs: Sequence[float], **_params) -> dict:
    return _baseline_moyenne_simple(valeurs)


def _baseline_moyenne_creneau(valeurs: Sequence[float], **_params) -> dict:
    return _baseline_moyenne_simple(valeurs)


STRATEGIES_BASELINE: dict = {
    "moyenne_simple": _baseline_moyenne_simple,
    "moyenne_jour_semaine": _baseline_moyenne_jour_semaine,
    "moyenne_creneau": _baseline_moyenne_creneau,
}


def calculer_baseline(type_baseline: str, valeurs: Sequence[float], **parametres) -> dict:
    """Exécute de façon déterministe la stratégie de baseline référencée."""
    try:
        strategie: Callable = STRATEGIES_BASELINE[type_baseline]
    except KeyError:
        raise KeyError(f"Type de baseline '{type_baseline}' non implémenté. Disponibles : {sorted(STRATEGIES_BASELINE)}")

    resultat = strategie(valeurs, **parametres)
    resultat["type"] = type_baseline
    return resultat
