from datetime import date
from decimal import Decimal
from typing import Optional

from django.db import models
from django.utils import timezone

from energy.models import BaremeTarifaire


def bareme_actuel(
    categorie: Optional[str],
    mode_paiement: Optional[str],
    a_la_date: Optional[date] = None,
):
    if not categorie or mode_paiement not in BaremeTarifaire.ModePaiement.values:
        return []
    a_la_date = a_la_date or timezone.localdate()
    return list(
        BaremeTarifaire.objects.filter(
            categorie=categorie,
            mode_paiement=mode_paiement,
            date_entree_vigueur__lte=a_la_date,
        )
        .filter(models.Q(date_fin_vigueur__isnull=True) | models.Q(date_fin_vigueur__gte=a_la_date))
        .order_by("ordre")
    )


def etat_tranche(compteur) -> dict:
    categorie = getattr(compteur, "categorie_tarifaire", "")
    mode_paiement = getattr(compteur, "mode_paiement", "")
    bareme = bareme_actuel(categorie, mode_paiement)
    if not bareme:
        return {
            "erreur": (
                "Catégorie tarifaire et mode de paiement vérifiés requis."
                if not categorie or not mode_paiement
                else f"Aucun barème actif pour {categorie} en mode {mode_paiement}."
            ),
            "categorie_tarifaire": categorie or None,
            "mode_paiement": mode_paiement or None,
        }
    return {
        "erreur": (
            "La période de référence et les règles de remise à zéro des tranches "
            "ne sont pas confirmées; aucun seuil courant n'est calculé."
        ),
        "categorie_tarifaire": categorie,
        "mode_paiement": mode_paiement,
        "bareme": [
            {
                "tranche": palier.nom_tranche,
                "kwh_min": float(palier.kwh_min),
                "kwh_max": float(palier.kwh_max) if palier.kwh_max is not None else None,
                "prix_fcfa_par_kwh": float(palier.prix_fcfa_par_kwh),
            }
            for palier in bareme
        ],
    }


def predire_kwh(compteur, montant_fcfa: Decimal) -> dict:
    categorie = getattr(compteur, "categorie_tarifaire", "")
    mode_paiement = getattr(compteur, "mode_paiement", "")
    if mode_paiement != BaremeTarifaire.ModePaiement.PREPAYE:
        return {
            "kwh_estimes": None,
            "detail_paliers": [],
            "categorie_tarifaire": categorie or None,
            "mode_paiement": mode_paiement or None,
            "erreur": "La prédiction d'un achat est réservée à un compteur configuré en prépaiement.",
        }
    bareme = bareme_actuel(categorie, mode_paiement)
    if not bareme:
        return {
            "kwh_estimes": None,
            "detail_paliers": [],
            "categorie_tarifaire": categorie or None,
            "mode_paiement": mode_paiement or None,
            "erreur": (
                "Catégorie tarifaire et mode de paiement vérifiés requis."
                if not categorie or not mode_paiement
                else f"Aucun barème actif pour {categorie} en mode {mode_paiement}."
            ),
        }
    return {
        "kwh_estimes": None,
        "detail_paliers": [],
        "categorie_tarifaire": categorie,
        "mode_paiement": mode_paiement,
        "erreur": (
            "Le montant payé ne permet pas de déduire les kWh crédités sans les "
            "retenues applicables au ticket. Utilisez les kWh affichés sur le ticket "
            "Woyofal; les achats ne représentent pas la consommation."
        ),
    }