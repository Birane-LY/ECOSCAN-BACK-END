"""Mesure l'impact énergétique d'une action depuis les index de capteurs réels."""

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from energy_assets.models import Capteur, MesureCapteur
from energy_assets.services import MINIMUM_RELEVES_JOUR, convertir_mesure

JOURS_PERIODE = 7


def _debut_journee(jour):
    return timezone.make_aware(datetime.combine(jour, time.min))


def calculer_impact_telemetrie_action(action, inclure_simules=False):
    """Compare sept jours complets avant et après la date de réalisation.

    La journée de réalisation est exclue. Chaque journée doit contenir au moins
    quatre index cohérents issus d'un capteur ENERGY actif. Par défaut, seuls
    les capteurs réels sont acceptés; `inclure_simules` permet un indicateur de
    scénario explicitement marqué. La fonction ne modifie pas l'action.
    """
    if action.economie_energie_kwh is not None:
        if action.energie_reference_kwh is None or action.energie_post_action_kwh is None:
            return {
                "statut": "donnees_invalides",
                "raison": "L'impact énergétique enregistré est incomplet.",
            }
        return {
            "statut": "mesure",
            "capteur_id": action.capteur_impact_id,
            "reference_kwh": action.energie_reference_kwh,
            "consommation_kwh": action.energie_post_action_kwh,
            "economie_kwh": action.economie_energie_kwh,
            "provenance": (
                action.capteur_impact.mode
                if action.capteur_impact_id
                else Capteur.Mode.REAL
            ),
        }
    if action.statut != action.Statut.TERMINEE or action.date_realisation is None:
        return {"statut": "indisponible", "raison": "L'action doit être terminée pour mesurer son impact."}
    if action.equipement_id is None:
        return {"statut": "indisponible", "raison": "Aucun équipement n'est associé à l'action."}

    modes_acceptes = [Capteur.Mode.REAL]
    if inclure_simules:
        modes_acceptes.append(Capteur.Mode.SIMULATED)
    capteurs = list(
        Capteur.objects.filter(
            equipement_id=action.equipement_id,
            statut=Capteur.Statut.ACTIVE,
            mode__in=modes_acceptes,
            type__iexact="ENERGY",
        ).order_by("id")
    )
    if not capteurs:
        return {
            "statut": "indisponible",
            "raison": (
                "Aucun capteur ENERGY actif n'est associé à l'équipement."
                if inclure_simules
                else "Aucun capteur ENERGY réel actif n'est associé à l'équipement."
            ),
        }
    if len(capteurs) != 1:
        return {
            "statut": "indisponible",
            "raison": "La mesure est ambiguë : l'équipement doit avoir un seul capteur ENERGY réel actif.",
        }

    site = action.equipement.site
    with timezone.override(site.fuseau_horaire):
        jour_realisation = timezone.localtime(action.date_realisation).date()
        jours_reference = tuple(
            jour_realisation - timedelta(days=decalage)
            for decalage in range(JOURS_PERIODE, 0, -1)
        )
        jours_apres = tuple(
            jour_realisation + timedelta(days=decalage)
            for decalage in range(1, JOURS_PERIODE + 1)
        )
        date_fin = jours_apres[-1] + timedelta(days=1)
        if _debut_journee(date_fin) > timezone.localtime(timezone.now()):
            return {
                "statut": "en_attente",
                "raison": "Les sept journées complètes après la réalisation ne sont pas encore écoulées.",
            }

        mesures = (
            MesureCapteur.objects.filter(
                capteur=capteurs[0],
                date_mesure__gte=_debut_journee(jours_reference[0]),
                date_mesure__lt=_debut_journee(date_fin),
            )
            .order_by("date_mesure", "date_reception")
        )
        par_jour = defaultdict(list)
        index_precedent = None
        for mesure in mesures.iterator():
            jour_mesure = timezone.localtime(mesure.date_mesure).date()
            try:
                conversion = convertir_mesure(capteurs[0].type, mesure.valeur, mesure.unite)
            except ValueError as exc:
                return {"statut": "indisponible", "raison": f"Un relevé ENERGY est invalide : {exc}"}
            if conversion is not None:
                index = conversion[1]
                if index_precedent is not None and index < index_precedent:
                    return {
                        "statut": "donnees_invalides",
                        "raison": "Un index cumulatif ENERGY a diminué pendant la période mesurée.",
                    }
                par_jour[jour_mesure].append(index)
                index_precedent = index

    jours_requis = (*jours_reference, *jours_apres)
    jours_incomplets = [
        jour
        for jour in jours_requis
        if len(par_jour[jour]) < MINIMUM_RELEVES_JOUR
    ]
    if jours_incomplets:
        return {
            "statut": "donnees_insuffisantes",
            "raison": (
                f"Relevés ENERGY insuffisants : {len(jours_incomplets)} journée(s) sur 14 "
                f"ont moins de {MINIMUM_RELEVES_JOUR} relevés."
            ),
        }

    reference_kwh = sum(
        (par_jour[jour][-1] - par_jour[jour][0] for jour in jours_reference),
        Decimal("0"),
    )
    consommation_kwh = sum(
        (par_jour[jour][-1] - par_jour[jour][0] for jour in jours_apres),
        Decimal("0"),
    )
    economie_kwh = reference_kwh - consommation_kwh

    return {
        "statut": "mesure",
        "capteur_id": capteurs[0].id,
        "provenance": capteurs[0].mode,
        "reference_kwh": reference_kwh,
        "consommation_kwh": consommation_kwh,
        "economie_kwh": economie_kwh,
    }


def enregistrer_impact_telemetrie_action(action):
    """Persiste le résultat quand un traitement planifié trouve des données complètes."""
    resultat = calculer_impact_telemetrie_action(action)
    if resultat["statut"] != "mesure" or action.economie_energie_kwh is not None:
        return resultat

    action.energie_reference_kwh = resultat["reference_kwh"]
    action.energie_post_action_kwh = resultat["consommation_kwh"]
    action.economie_energie_kwh = resultat["economie_kwh"]
    action.capteur_impact_id = resultat["capteur_id"]
    action.date_mesure_energie = timezone.now()
    action.save(
        update_fields=(
            "energie_reference_kwh",
            "energie_post_action_kwh",
            "economie_energie_kwh",
            "capteur_impact",
            "date_mesure_energie",
        )
    )
    return resultat
