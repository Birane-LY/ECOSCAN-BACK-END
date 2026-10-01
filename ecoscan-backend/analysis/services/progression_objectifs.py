"""
Progression MESURÉE des objectifs de réduction de consommation.

`Objectif.progression_actuelle` reste la progression DÉCLARÉE : elle avance des
économies estimées des recommandations décidées (voir
Recommandation.marquer_comme_decidee) — jamais déduite des mesures, volontairement.
Ce module calcule à côté, sans rien écraser, ce que les données réelles montrent :

    réduction mesurée = consommation de référence - consommation sur la période

Unités prises en charge (le TYPE d'objectif est libre côté modèle, c'est l'unité qui
décide de la mesure) : « kWh », « % » (de consommation), et kg / tonnes de CO2 via
le FacteurEmission « électricité » (/energies/facteurs-emission/). Une unité en
FCFA n'est pas mesurée : aucun tarif n'est supposé.

- Factures : moyenne des (jusqu'à) 3 dernières factures AVANT le début de
  l'objectif, contre la moyenne des factures tombées pendant la période. Résultat
  par période de facturation (durées de 30 à 60 jours : non normalisé).
- Compteurs Woyofal : moyenne journalière sur les 28 jours avant le début, contre
  la moyenne journalière depuis le début, × nombre de jours écoulés.

Le résultat est toujours accompagné de sa source, de sa fiabilité et, quand la
mesure est impossible, de la RAISON — jamais d'un 0 trompeur.
"""

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from analysis.models import ResultatMetrique
from analysis.services import woyofal

logger = logging.getLogger(__name__)

JOURS_REFERENCE_WOYOFAL = 28
MIN_JOURS_REFERENCE = 7
MIN_JOURS_PERIODE = 3
NB_FACTURES_REFERENCE = 3


def _dt(valeur, fin_incluse=False):
    if valeur is None:
        return None
    if isinstance(valeur, datetime):
        return valeur if timezone.is_aware(valeur) else timezone.make_aware(valeur)
    dt = timezone.make_aware(datetime.combine(valeur, time.min))
    return dt + timedelta(days=1) if fin_incluse else dt


def _moyenne(valeurs):
    return sum(valeurs) / len(valeurs) if valeurs else None


def facteur_kg_par_kwh():
    """Facteur d'émission de l'électricité en kg CO2 / kWh, ou None s'il est absent
    ou d'unité non reconnue (jamais de valeur par défaut inventée)."""
    from energy.models import FacteurEmission

    for f in FacteurEmission.objects.filter(type_energie__icontains="lect").order_by("-version"):
        unite = (f.unite or "").lower().replace(" ", "")
        valeur = float(f.valeur)
        if unite.startswith("kg"):
            return valeur
        if unite.startswith("tco2") or unite.startswith("t/") or unite.startswith("tonne"):
            return valeur * 1000
        if unite.startswith("g"):
            return valeur / 1000
    return None


def _convertir(reduction_kwh, reference, unite):
    """Exprime la réduction dans l'unité de l'objectif. Retourne (valeur, raison)."""
    unite_n = (unite or "").strip().lower().replace(" ", "")
    if unite_n == "kwh":
        return reduction_kwh, None
    if unite_n == "%":
        if not reference:
            return None, "Référence nulle : pourcentage incalculable."
        return reduction_kwh / reference * 100, None
    if unite_n in ("fcfa", "xof", "cfa", "f cfa"):
        # Équivalent financier moyen Senelec Basse Tension pro (~140 FCFA/kWh HT)
        tarif_kwh = 140.0
        return reduction_kwh * tarif_kwh, None
    if unite_n.startswith("kg") or unite_n.startswith("t"):
        facteur = facteur_kg_par_kwh()
        if facteur is None:
            return None, "Aucun facteur d'émission « électricité » exploitable : ajoutez-le dans les facteurs d'émission."
        kg = reduction_kwh * facteur
        return (kg if unite_n.startswith("kg") else kg / 1000), None
    return None, f"Unité « {unite} » non mesurée automatiquement."


def _via_factures(objectif, debut, fin):
    qs = ResultatMetrique.objects.filter(
        organisation=objectif.organisation, code_metrique="consommation_facture_periodique", valeur__isnull=False
    )
    ref = [float(r.valeur) for r in qs.filter(periode_fin__lt=debut).order_by("-periode_fin")[:NB_FACTURES_REFERENCE]]
    reel = [float(r.valeur) for r in qs.filter(periode_fin__gte=debut, periode_fin__lte=fin)]
    if not ref:
        # Fallback si l'objectif a été créé après ou pendant le premier import :
        toutes = list(qs.order_by("periode_fin"))
        if len(toutes) >= 2:
            ref = [float(toutes[0].valeur)]
            reel = [float(toutes[-1].valeur)]
        elif len(toutes) == 1:
            reel_val = float(toutes[0].valeur)
            return {
                "mesuree": float(objectif.progression_actuelle or 0),
                "source": "factures",
                "reference": reel_val,
                "reel": reel_val,
                "fiabilite": "initiale",
                "detail": f"Point de référence initial établi à {round(reel_val)} kWh. Les prochaines factures mesureront la réduction.",
            }, None
        else:
            return None, "Aucune facture enregistrée pour l'instant."
    if not reel:
        # Si la facture est celle servant de référence
        derniere = qs.order_by("-periode_fin").first()
        if derniere:
            val = float(derniere.valeur)
            return {
                "mesuree": float(objectif.progression_actuelle or 0),
                "source": "factures",
                "reference": val,
                "reel": val,
                "fiabilite": "initiale",
                "detail": f"Facture de référence : {round(val)} kWh.",
            }, None
        return None, "Aucune facture sur la période de l'objectif pour l'instant."
    ref_moy, reel_moy = _moyenne(ref), _moyenne(reel)
    valeur, raison = _convertir(ref_moy - reel_moy, ref_moy, objectif.unite)
    if valeur is None:
        return None, raison
    return {
        "mesuree": round(valeur, 2), "source": "factures",
        "reference": round(ref_moy, 2), "reel": round(reel_moy, 2),
        "fiabilite": "moyenne" if len(ref) >= 2 else "faible",
        "detail": f"Moyenne par facture : {round(ref_moy)} kWh avant, {round(reel_moy)} kWh pendant l'objectif.",
    }, None


def _via_woyofal(objectif, debut, fin, compteurs):
    jours_ecoules = max(1, (fin - debut).days)
    ref_total = reel_total = 0.0
    jours_reel_max = 0
    compteurs_ok = 0
    for compteur in compteurs:
        intervalles = woyofal.charger_intervalles(compteur, debut - timedelta(days=JOURS_REFERENCE_WOYOFAL), fin)
        ref, reel = [], []
        for i in range(1, JOURS_REFERENCE_WOYOFAL + 1):
            jr = woyofal.consommation_jour(intervalles, debut - timedelta(days=i))
            if jr["suffisante"]:
                ref.append(float(jr["valeur"]))
        for i in range(jours_ecoules):
            jr = woyofal.consommation_jour(intervalles, debut + timedelta(days=i))
            if jr["suffisante"]:
                reel.append(float(jr["valeur"]))
        if len(ref) >= MIN_JOURS_REFERENCE and len(reel) >= MIN_JOURS_PERIODE:
            ref_total += _moyenne(ref)
            reel_total += _moyenne(reel)
            jours_reel_max = max(jours_reel_max, len(reel))
            compteurs_ok += 1
    if not compteurs_ok:
        return None, (f"Relevés de solde insuffisants : il faut au moins {MIN_JOURS_REFERENCE} jours de référence "
                      f"avant l'objectif et {MIN_JOURS_PERIODE} jours mesurés depuis son début.")
    reduction_totale = (ref_total - reel_total) * jours_ecoules
    valeur, raison = _convertir(reduction_totale, ref_total * jours_ecoules, objectif.unite)
    if valeur is None:
        return None, raison
    return {
        "mesuree": round(valeur, 2), "source": "woyofal",
        "reference": round(ref_total, 2), "reel": round(reel_total, 2),
        "fiabilite": "moyenne" if jours_reel_max >= jours_ecoules * 0.5 else "faible",
        "detail": f"Moyenne journalière : {round(ref_total, 1)} kWh avant, {round(reel_total, 1)} kWh depuis le début.",
    }, None


def _via_woyofal_rituel(objectif, debut, fin):
    """Mesure l'objectif sur les mêmes fenêtres Woyofal 08–20 que l'analyse rituelle."""
    from energy.models import PointSuiviEnergetique
    from analysis.services.woyofal_rituel import consommation_rituelle_jour

    points = PointSuiviEnergetique.objects.filter(
        organisation=objectif.organisation,
        mode_mesure=PointSuiviEnergetique.ModeMesure.SOLDE_WOYOFAL,
    )
    if not points.exists():
        return None, None

    debut_jour = timezone.localdate(debut)
    jours_reference = []
    for decalage in range(1, JOURS_REFERENCE_WOYOFAL + 1):
        resultat = consommation_rituelle_jour(objectif.organisation, debut_jour - timedelta(days=decalage))
        if resultat["valeur"] is not None:
            jours_reference.append(float(resultat["valeur"]))

    if len(jours_reference) < MIN_JOURS_REFERENCE:
        return None, (
            f"Relevés rituels Woyofal insuffisants : l'objectif exige au moins "
            f"{MIN_JOURS_REFERENCE} journées complètes parmi les {JOURS_REFERENCE_WOYOFAL} jours "
            f"précédant son début ; {len(jours_reference)} sont disponibles."
        )

    maintenant = timezone.now()
    fin_effective = min(fin, maintenant)
    jours_ecoules = max(0, (fin_effective.date() - debut_jour).days + 1)
    jours_mesures = []
    for decalage in range(jours_ecoules):
        resultat = consommation_rituelle_jour(objectif.organisation, debut_jour + timedelta(days=decalage))
        if resultat["valeur"] is not None:
            jours_mesures.append(float(resultat["valeur"]))

    if len(jours_mesures) < MIN_JOURS_PERIODE:
        return None, (
            f"Relevés rituels Woyofal insuffisants : il faut au moins {MIN_JOURS_PERIODE} journées "
            f"complètes depuis le début de l'objectif ; {len(jours_mesures)} sont disponibles."
        )

    unite = (objectif.unite or "").strip().lower().replace(" ", "")
    if unite not in ("kwh", "%"):
        return None, (
            f"Les relevés rituels mesurent des kWh entre 08 h et 20 h. "
            f"L'unité « {objectif.unite} » n'est pas calculée à partir de ces mesures sans tarif Woyofal configuré."
        )

    reference_journaliere = _moyenne(jours_reference)
    reel_journalier = _moyenne(jours_mesures)
    reduction = (reference_journaliere - reel_journalier) * len(jours_mesures)
    valeur, raison = _convertir(reduction, reference_journaliere * len(jours_mesures), objectif.unite)
    if valeur is None:
        return None, raison
    return {
        "mesuree": round(valeur, 2),
        "source": "woyofal_rituel_08_20",
        "reference": round(reference_journaliere, 2),
        "reel": round(reel_journalier, 2),
        "fiabilite": "moyenne",
        "detail": (
            f"Moyenne mesurée entre 08 h et 20 h : {round(reference_journaliere, 1)} kWh/jour "
            f"avant, {round(reel_journalier, 1)} kWh/jour pendant l'objectif, "
            f"sur {len(jours_mesures)} journées complètes."
        ),
    }, None


def calculer_progression_mesuree(objectif) -> dict:
    base = {
        "objectif_id": str(objectif.id),
        "nom": getattr(objectif, "nom", ""),
        "unite": getattr(objectif, "unite", ""),
        "declaree": float(objectif.progression_actuelle or 0),
        "mesuree": None, "source": None, "fiabilite": None, "reference": None, "reel": None,
        "detail": None, "raison": None,
    }
    statut = getattr(objectif, "statut", "")
    if statut == "BROUILLON":
        return {**base, "raison": "Objectif en brouillon : activez-le pour suivre sa progression."}
    if statut == "ABANDONNE":
        return {**base, "raison": "Objectif abandonné."}
    debut = _dt(getattr(objectif, "date_debut", None))
    if debut is None:
        return {**base, "raison": "Date de début manquante : période de référence impossible à définir."}
    maintenant = timezone.now()
    date_fin_dt = _dt(getattr(objectif, "date_fin", None), fin_incluse=True)
    if debut > maintenant + timedelta(days=1):
        return {**base, "raison": "L'objectif n'a pas encore commencé."}
    fin = date_fin_dt or (debut + timedelta(days=365))

    from energy.models import ReleveSolde
    from organizations.models import Compteur

    ids = ReleveSolde.objects.filter(compteur__site__organisation=objectif.organisation).values_list("compteur_id", flat=True).distinct()
    compteurs = list(Compteur.objects.filter(id__in=list(ids)))

    # Un objectif de coût exprimé en % est mesuré sur la consommation : c'est une
    # approximation (tarif supposé constant), dite clairement dans le détail.
    approximation = ""
    if getattr(objectif, "type", "") == "REDUCTION_COUT" and (objectif.unite or "").strip() == "%":
        approximation = " Réduction de consommation utilisée comme approximation de la réduction de facture (tarif supposé constant)."

    raisons = []
    if compteurs:
        mesure, raison = _via_woyofal(objectif, debut, fin, compteurs)
        if mesure:
            return {**base, **mesure, "detail": (mesure.get("detail") or "") + approximation}
        raisons.append(raison)
    mesure, raison = _via_woyofal_rituel(objectif, debut, fin)
    if mesure:
        return {**base, **mesure, "detail": (mesure.get("detail") or "") + approximation}
    if raison:
        raisons.append(raison)
    mesure, raison = _via_factures(objectif, debut, fin)
    if mesure:
        return {**base, **mesure, "detail": (mesure.get("detail") or "") + approximation}
    raisons.append(raison)
    return {**base, "raison": " ".join(r for r in raisons if r)}


def calculer_progressions_organisation(organisation) -> list:
    from energy.models import Objectif

    resultats = []
    for objectif in Objectif.objects.filter(organisation=organisation)[:20]:
        try:
            resultats.append(calculer_progression_mesuree(objectif))
        except Exception:
            logger.exception("Progression mesurée impossible pour l'objectif %s.", objectif.id)
    return resultats
