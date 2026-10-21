"""Diagnostics d'occupation et de consommation par zone."""

from decimal import Decimal

from django.utils import timezone

from .models import Capteur, CapteurPresence, Equipement
from .services import convertir_mesure


ETAT_OCCUPEE = "OCCUPIED"
ETAT_INOCCUPEE = "UNOCCUPIED"
ETAT_INCONNUE = "UNKNOWN"


def delai_fraicheur_secondes(frequence_secondes):
    return max(frequence_secondes * 3, 60)


def obtenir_occupation_zone(zone, maintenant=None, simulation_seule=False):
    """Retourne un état prudent : une zone n'est libre que si tous ses capteurs sont à jour."""
    maintenant = maintenant or timezone.now()
    capteurs = list(
        CapteurPresence.objects.filter(
            zone=zone,
            statut=CapteurPresence.Statut.ACTIVE,
        ).order_by("identifiant")
    )
    modes = {capteur.mode for capteur in capteurs}
    source = next(iter(modes)) if len(modes) == 1 else ("MIXED" if modes else "NONE")

    if not capteurs:
        return {
            "state": ETAT_INCONNUE,
            "source": source,
            "observed_at": None,
            "reason": "NO_ACTIVE_PRESENCE_SENSOR",
        }
    if simulation_seule and CapteurPresence.Mode.REAL in modes:
        return {
            "state": ETAT_INCONNUE,
            "source": source,
            "observed_at": None,
            "reason": "REAL_PRESENCE_SENSOR_NOT_AUTOMATED",
        }

    observations = []
    for capteur in capteurs:
        mesure = (
            capteur.mesures.filter(date_mesure__lte=maintenant)
            .order_by("-date_mesure", "-date_reception")
            .first()
        )
        if mesure is None:
            observations.append((capteur, None, False))
            continue
        age = (maintenant - mesure.date_mesure).total_seconds()
        fraiche = age <= delai_fraicheur_secondes(capteur.frequence_secondes)
        observations.append((capteur, mesure, fraiche))

    dates_fraiches = [
        mesure.date_mesure
        for _, mesure, fraiche in observations
        if mesure is not None and fraiche
    ]
    if any(mesure and fraiche and mesure.occupee for _, mesure, fraiche in observations):
        state = ETAT_OCCUPEE
        reason = "RECENT_OCCUPANCY_SIGNAL"
    elif all(mesure is not None and fraiche and not mesure.occupee for _, mesure, fraiche in observations):
        state = ETAT_INOCCUPEE
        reason = "ALL_ACTIVE_SENSORS_REPORT_EMPTY"
    else:
        state = ETAT_INCONNUE
        reason = "MISSING_OR_STALE_PRESENCE_DATA"

    return {
        "state": state,
        "source": source,
        "observed_at": max(dates_fraiches) if dates_fraiches else None,
        "reason": reason,
    }


def _puissance_courante(equipement, maintenant):
    capteurs = Capteur.objects.filter(
        equipement=equipement,
        type__iexact="POWER",
        statut=Capteur.Statut.ACTIVE,
    ).order_by("identifiant")
    mesures = []
    for capteur in capteurs:
        mesure = (
            capteur.mesures.filter(date_mesure__lte=maintenant)
            .order_by("-date_mesure", "-date_reception")
            .first()
        )
        if mesure is not None:
            mesures.append((capteur, mesure))
    if not mesures:
        if not capteurs.exists():
            return None, None, None, "NO_ACTIVE_POWER_SENSOR"
        return None, None, None, "NO_POWER_MEASUREMENT"

    capteur, mesure = max(
        mesures,
        key=lambda item: (item[1].date_mesure, item[1].date_reception),
    )
    if (
        (maintenant - mesure.date_mesure).total_seconds()
        > delai_fraicheur_secondes(capteur.frequence_secondes)
    ):
        return None, capteur.mode, mesure.date_mesure, "STALE_POWER_MEASUREMENT"
    try:
        conversion = convertir_mesure(capteur.type, mesure.valeur, mesure.unite)
    except ValueError:
        return None, capteur.mode, mesure.date_mesure, "INVALID_POWER_MEASUREMENT"
    if conversion is None:
        return None, capteur.mode, mesure.date_mesure, "INVALID_POWER_MEASUREMENT"
    return conversion[1], capteur.mode, mesure.date_mesure, "OK"


def diagnostic_occupation_site(site, maintenant=None):
    """Construit un diagnostic en lecture seule, sans créer d'anomalie ni de commande."""
    maintenant = maintenant or timezone.now()
    zones = []
    puissance_inoccupee_kw = Decimal("0")
    puissance_evitable_kw = Decimal("0")

    for zone in site.zones.filter(active=True).order_by("nom"):
        occupation = obtenir_occupation_zone(zone, maintenant=maintenant)
        equipements = []
        zone_power = Decimal("0")
        zone_excess = Decimal("0")
        for equipement in Equipement.objects.filter(
            site=site,
            zone=zone,
            monitoring_active=True,
        ).order_by("nom"):
            puissance, source, measured_at, telemetry_status = _puissance_courante(
                equipement,
                maintenant,
            )
            excedent = None
            if puissance is not None and occupation["state"] == ETAT_INOCCUPEE:
                veille = equipement.puissance_veille_kw * equipement.quantite
                excedent = max(puissance - veille, Decimal("0"))
                zone_power += puissance
                zone_excess += excedent
            equipements.append(
                {
                    "equipment_id": str(equipement.id),
                    "name": equipement.nom,
                    "category": equipement.categorie,
                    "criticality": equipement.criticite,
                    "reported_state": (
                        equipement.etat.etat_rapporte
                        if hasattr(equipement, "etat")
                        else Equipement.Etat.UNKNOWN
                    ),
                    "power_kw": puissance,
                    "power_source": source,
                    "power_measured_at": measured_at,
                    "telemetry_status": telemetry_status,
                    "avoidable_power_kw": excedent,
                }
            )
        if occupation["state"] == ETAT_INOCCUPEE:
            puissance_inoccupee_kw += zone_power
            puissance_evitable_kw += zone_excess
        zones.append(
            {
                "zone_id": str(zone.id),
                "zone_name": zone.nom,
                "occupancy_state": occupation["state"],
                "occupancy_source": occupation["source"],
                "occupancy_observed_at": occupation["observed_at"],
                "occupancy_status": occupation["reason"],
                "unoccupied_power_kw": (
                    zone_power
                    if occupation["state"] == ETAT_INOCCUPEE
                    else None
                ),
                "avoidable_power_kw": (
                    zone_excess
                    if occupation["state"] == ETAT_INOCCUPEE
                    else None
                ),
                "equipment": equipements,
            }
        )

    return {
        "site_id": str(site.id),
        "site_name": site.nom,
        "evaluated_at": maintenant,
        "mode": "READ_ONLY_DIAGNOSTIC",
        "unoccupied_power_kw": puissance_inoccupee_kw,
        "avoidable_power_kw": puissance_evitable_kw,
        "zones": zones,
    }
