"""Calcul des indicateurs de monitoring et de consommation des sites."""

from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from .models import Capteur, Equipement, EtatEquipement
from .services import convertir_mesure


DELAI_CAPTEUR_HORS_LIGNE = timedelta(minutes=5)
ZERO_KW = Decimal("0.000")


def _maintenant():
    """Retourne l'heure courante, isolée pour faciliter les tests."""
    return timezone.now()


def _equipements_suivis(site):
    """Retourne les équipements suivis du site avec leurs relations utiles."""
    return (
        Equipement.objects.filter(site=site, monitoring_active=True)
        .select_related("zone", "etat")
        .prefetch_related("capteurs")
    )


def _est_hors_ligne(equipement, date_limite):
    """Indique si aucun capteur actif de l'équipement n'a communiqué récemment."""
    capteurs_actifs = [
        capteur
        for capteur in equipement.capteurs.all()
        if capteur.statut == Capteur.Statut.ACTIVE
    ]
    return not any(
        capteur.derniere_communication
        and capteur.derniere_communication >= date_limite
        for capteur in capteurs_actifs
    )


def _puissance_actuelle(equipement):
    """Retourne la puissance courante de l'équipement, ou zéro si inconnue."""
    etat = _etat_equipement(equipement)
    return etat.puissance_actuelle_kw if etat else ZERO_KW


def _etat_equipement(equipement):
    """Retourne l'état associé à l'équipement, s'il existe."""
    try:
        return equipement.etat
    except EtatEquipement.DoesNotExist:
        return None


def _debut_journee(jour):
    """Construit l'instant local correspondant au début du jour donné."""
    debut = datetime.combine(jour, datetime.min.time())
    return timezone.make_aware(debut, timezone.get_current_timezone())


def _consommation_periode(capteurs, debut, fin):
    """Calcule l'énergie consommée à partir des index cumulés des capteurs."""
    total = Decimal("0")
    total_capteurs = len(capteurs)
    capteurs_complets = 0
    debut_couverture = debut

    for capteur in capteurs:
        if capteur.mode == Capteur.Mode.SIMULATED:
            baseline = (
                capteur.mesures.filter(
                    date_mesure__gte=debut,
                    date_mesure__lt=fin,
                )
                .order_by("date_mesure", "date_reception")
                .first()
            )
            if baseline:
                debut_couverture = max(debut_couverture, baseline.date_mesure)
        else:
            baseline = (
                capteur.mesures.filter(date_mesure__lt=debut)
                .order_by("-date_mesure", "-date_reception")
                .first()
            )
        derniere = (
            capteur.mesures.filter(date_mesure__lt=fin)
            .order_by("-date_mesure", "-date_reception")
            .first()
        )
        if (
            baseline is None
            or derniere is None
            or derniere.date_mesure < debut
        ):
            continue

        _, baseline_kwh = convertir_mesure(
            baseline.capteur.type,
            baseline.valeur,
            baseline.unite,
        )
        _, derniere_kwh = convertir_mesure(
            derniere.capteur.type,
            derniere.valeur,
            derniere.unite,
        )
        delta = derniere_kwh - baseline_kwh
        if delta < 0:
            continue
        total += delta
        capteurs_complets += 1

    if total_capteurs == 0 or capteurs_complets != total_capteurs:
        return None, {
            "complete_sensors": capteurs_complets,
            "total_sensors": total_capteurs,
        }, None
    return total, {
        "complete_sensors": capteurs_complets,
        "total_sensors": total_capteurs,
    }, debut_couverture


def _mode_telemetrie(capteurs):
    modes = {
        capteur.mode
        for capteur in capteurs
        if capteur.statut == Capteur.Statut.ACTIVE
        and capteur.type.strip().upper() in ("POWER", "ENERGY")
    }
    if not modes:
        return "NONE"
    if modes == {Capteur.Mode.SIMULATED}:
        return "SIMULATED"
    if modes == {Capteur.Mode.REAL}:
        return "REAL"
    return "MIXED"


def obtenir_indicateurs_site(site):
    """Agrège les indicateurs de monitoring et les consommateurs du site."""
    maintenant = _maintenant()
    aujourd_hui = timezone.localdate(maintenant)
    debut_aujourd_hui = _debut_journee(aujourd_hui)
    debut_hier = debut_aujourd_hui - timedelta(days=1)
    debut_mois = _debut_journee(aujourd_hui.replace(day=1))
    date_limite = maintenant - DELAI_CAPTEUR_HORS_LIGNE
    equipements = list(_equipements_suivis(site))
    capteurs_energie = list(
        Capteur.objects.filter(
            equipement__in=equipements,
            statut=Capteur.Statut.ACTIVE,
            type__iexact="ENERGY",
        )
    )
    (
        energie_aujourd_hui,
        couverture_aujourd_hui,
        energie_aujourd_hui_depuis,
    ) = _consommation_periode(
        capteurs_energie,
        debut_aujourd_hui,
        maintenant,
    )
    energie_hier, couverture_hier, _ = _consommation_periode(
        capteurs_energie,
        debut_hier,
        debut_aujourd_hui,
    )
    energie_mois, couverture_mois, _ = _consommation_periode(
        capteurs_energie,
        debut_mois,
        maintenant,
    )
    capteurs_site = [
        capteur
        for equipement in equipements
        for capteur in equipement.capteurs.all()
    ]
    modes_par_equipement = {
        equipement.id: _mode_telemetrie(equipement.capteurs.all())
        for equipement in equipements
    }
    puissances = {
        equipement.id: _puissance_actuelle(equipement)
        for equipement in equipements
    }
    puissance_totale = sum(puissances.values(), ZERO_KW)

    charge_equipements = []
    for equipement in equipements:
        puissance = puissances[equipement.id]
        etat = _etat_equipement(equipement)
        charge_equipements.append(
            {
                "equipment_id": equipement.id,
                "name": equipement.nom,
                "zone_id": equipement.zone_id,
                "zone_name": equipement.zone.nom if equipement.zone else None,
                "power_kw": puissance,
                "reported_state": (
                    etat.etat_rapporte if etat else Equipement.Etat.UNKNOWN
                ),
                "is_online": not _est_hors_ligne(equipement, date_limite),
                "telemetry_mode": modes_par_equipement[equipement.id],
            }
        )

    consommateurs = []
    for item in charge_equipements:
        if item["power_kw"] <= 0:
            continue
        consommateurs.append(
            {
                **item,
                "share_percent": (
                    (
                        item["power_kw"]
                        * Decimal("100")
                        / puissance_totale
                    ).quantize(
                        Decimal("0.1")
                    )
                    if puissance_totale
                    else Decimal("0.0")
                ),
            }
        )
    consommateurs.sort(key=lambda item: (-item["power_kw"], item["name"]))

    return {
        "site_id": site.id,
        "site_name": site.nom,
        "current_power_kw": puissance_totale,
        "today_energy_kwh": energie_aujourd_hui,
        "today_energy_since": energie_aujourd_hui_depuis,
        "telemetry_mode": _mode_telemetrie(capteurs_site),
        "yesterday_energy_kwh": energie_hier,
        "month_energy_kwh": energie_mois,
        "energy_coverage": {
            "today": couverture_aujourd_hui,
            "yesterday": couverture_hier,
            "month": couverture_mois,
        },
        "monitored_equipment_count": len(equipements),
        "active_equipment_count": sum(
            1
            for equipement in equipements
            if (etat := _etat_equipement(equipement))
            and etat.etat_rapporte == Equipement.Etat.ON
        ),
        "offline_equipment_count": sum(
            _est_hors_ligne(equipement, date_limite)
            for equipement in equipements
        ),
        "equipment": charge_equipements,
        "top_consumer": consommateurs[0] if consommateurs else None,
        "latest_measurement_at": max(
            (
                equipement.etat.date_etat_rapporte
                for equipement in equipements
                if _etat_equipement(equipement)
                and equipement.etat.date_etat_rapporte is not None
            ),
            default=None,
        ),
    }, consommateurs
