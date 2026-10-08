"""Analyse agrégée des relevés rituels Woyofal sur la fenêtre observée 08 h–20 h."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from analysis.models import Anomalie, ResultatMetrique
from analysis.services.anomaly import detecter_anomalie
from analysis.services.context import obtenir_contexte
from analysis.services.hypothesis import generer_hypothese
from analysis.services.registry import obtenir_definition
from organizations.models import Organisation
from energy.models import (
    PointSuiviEnergetique,
    RechargeRituelWoyofal,
    ReleveRituelEnergetique,
)

CRENEAUX = ("08:00", "12:00", "16:00", "20:00")
NB_JOURS_REFERENCE = 7
NB_JOURS_REFERENCE_MIN = 4
MICRO = Decimal("0.000001")


def _debut_jour(jour: date):
    return timezone.make_aware(datetime.combine(jour, time.min))


def consommation_rituelle_jour(organisation, jour: date) -> dict:
    """Retourne la somme 08–20 de tous les points Woyofal, ou une raison explicite."""
    points = list(
        PointSuiviEnergetique.objects.filter(
            organisation=organisation,
            mode_mesure=PointSuiviEnergetique.ModeMesure.SOLDE_WOYOFAL,
        ).order_by("id")
    )
    if not points:
        return {"valeur": None, "raison": "Aucun point de suivi Woyofal n'est configuré."}

    total = Decimal("0")
    for point in points:
        readings = {
            reading.creneau: reading
            for reading in ReleveRituelEnergetique.objects.filter(
                point_suivi=point,
                date_releve=jour,
                creneau__in=CRENEAUX,
            )
        }
        manquants = [slot for slot in CRENEAUX if slot not in readings]
        if manquants:
            return {
                "valeur": None,
                "raison": (
                    f"Relevés Woyofal incomplets le {jour:%d/%m/%Y} pour « {point.nom} » "
                    f"(créneaux manquants : {', '.join(manquants)})."
                ),
            }

        debut = _debut_jour(jour)
        fin = debut + timedelta(days=1)
        recharges = RechargeRituelWoyofal.objects.filter(
            point_suivi=point,
            effectuee_le__gte=debut,
            effectuee_le__lt=fin,
        )
        for start_slot, end_slot in zip(CRENEAUX, CRENEAUX[1:]):
            start_value = Decimal(readings[start_slot].valeur_kwh)
            end_value = Decimal(readings[end_slot].valeur_kwh)
            start_hour, start_minute = map(int, start_slot.split(":"))
            end_hour, end_minute = map(int, end_slot.split(":"))
            interval_start = timezone.make_aware(datetime.combine(jour, time(start_hour, start_minute)))
            interval_end = timezone.make_aware(datetime.combine(jour, time(end_hour, end_minute)))
            credits = Decimal("0")
            for recharge in recharges.filter(effectuee_le__gt=interval_start, effectuee_le__lte=interval_end):
                credits += Decimal(recharge.kwh_credites)

            consumption = start_value + credits - end_value
            if consumption < 0:
                return {
                    "valeur": None,
                    "raison": (
                        f"Intervalle Woyofal incohérent pour « {point.nom} » entre "
                        f"{start_slot} et {end_slot} : vérifiez les soldes et les recharges."
                    ),
                }
            total += consumption

    return {"valeur": total.quantize(MICRO), "raison": None}


def _persister(organisation, definition, debut, fin, valeur, baseline=None):
    baseline = baseline or {}
    resultat, _ = ResultatMetrique.objects.update_or_create(
        organisation=organisation,
        compteur=None,
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
            "completude": Decimal("1"),
            "statut_qualite": ResultatMetrique.StatutQualite.FIABLE,
            "confiance": Decimal("1"),
            "sources": list(definition.sources_requises),
            "limites": list(definition.limites),
        },
    )
    return resultat


def analyser_woyofal_rituel(organisation_id, jour_iso: str) -> dict:
    """Persiste la consommation 08–20 puis compare au moins quatre jours sur les sept précédents."""
    organisation = Organisation.objects.filter(id=organisation_id).first()
    if organisation is None:
        raise Organisation.DoesNotExist(f"Organisation introuvable : {organisation_id}")
    try:
        jour = date.fromisoformat(jour_iso)
    except (TypeError, ValueError) as exc:
        raise ValueError("jour_iso doit respecter le format AAAA-MM-JJ.") from exc

    observation = consommation_rituelle_jour(organisation, jour)
    if observation["valeur"] is None:
        return {"statut": "donnees_insuffisantes", "detail": observation["raison"]}

    debut = _debut_jour(jour)
    fin = debut + timedelta(days=1)
    definition_conso = obtenir_definition("consommation_woyofal_rituelle_08_20")
    definition_variation = obtenir_definition("variation_woyofal_rituelle_vs_moyenne_recente")
    with transaction.atomic():
        _persister(organisation, definition_conso, debut, fin, observation["valeur"])

        references = []
        raisons = []
        for decalage in range(1, NB_JOURS_REFERENCE + 1):
            jour_reference = jour - timedelta(days=decalage)
            resultat_jour = consommation_rituelle_jour(organisation, jour_reference)
            if resultat_jour["valeur"] is not None:
                references.append(resultat_jour["valeur"])
            else:
                raisons.append(resultat_jour["raison"])

        if len(references) < NB_JOURS_REFERENCE_MIN:
            return {
                "statut": "reference_insuffisante",
                "consommation_kwh": float(observation["valeur"]),
                "jours_reference": len(references),
                "jours_reference_requis": NB_JOURS_REFERENCE_MIN,
                "detail": (
                    f"La consommation 08 h–20 h du {jour:%d/%m/%Y} est de "
                    f"{observation['valeur']} kWh. Il faut au moins "
                    f"{NB_JOURS_REFERENCE_MIN} jours complets parmi les {NB_JOURS_REFERENCE} précédents "
                    f"pour comparer et détecter une dérive ; {len(references)} sont disponibles."
                ),
            }

        baseline = sum(references, Decimal("0")) / Decimal(len(references))
        if baseline == 0:
            variation = Decimal("0") if observation["valeur"] == 0 else Decimal("100")
        else:
            variation = (
                (observation["valeur"] - baseline) / baseline * Decimal("100")
            ).quantize(Decimal("0.01"))
        resultat = _persister(
            organisation,
            definition_variation,
            debut,
            fin,
            variation,
            baseline={
                "type": "moyenne_simple",
                "valeur": baseline.quantize(MICRO),
                "nombre_observations": len(references),
            },
        )

    anomalie = detecter_anomalie(resultat)
    if anomalie is None:
        return {
            "statut": "normal",
            "consommation_kwh": float(observation["valeur"]),
            "baseline_kwh": float(baseline),
            "variation_pct": float(variation),
            "jours_reference": len(references),
            "anomalie": None,
        }

    hypothese = anomalie.hypotheses.order_by("-date_creation").first()
    if hypothese is None:
        hypothese = generer_hypothese(anomalie, obtenir_contexte(anomalie))
    return {
        "statut": "anomalie_detectee",
        "consommation_kwh": float(observation["valeur"]),
        "baseline_kwh": float(baseline),
        "variation_pct": float(variation),
        "jours_reference": len(references),
        "anomalie": str(anomalie.id),
        "hypothese": str(hypothese.id) if hypothese else None,
    }
