"""Évaluation déterministe et strictement simulée des politiques d'autonomie."""

from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .models import (
    Capteur,
    DecisionAutonome,
    Equipement,
    EtatEquipement,
    PilotageAutonome,
)
from .occupancy import ETAT_INOCCUPEE, obtenir_occupation_zone
from .services import _actualiser_statut_synchronisation


INTERVALLE_EVALUATION_SECONDES = 60


def equipement_pilotable_en_simulation(equipement):
    """Autorise uniquement les équipements suivis par des capteurs simulés."""
    a_puissance_simulee = Capteur.objects.filter(
        equipement=equipement,
        mode=Capteur.Mode.SIMULATED,
        statut=Capteur.Statut.ACTIVE,
        type__iexact="POWER",
    ).exists()
    a_capteur_reel_actif = Capteur.objects.filter(
        equipement=equipement,
        mode=Capteur.Mode.REAL,
        statut=Capteur.Statut.ACTIVE,
    ).exists()
    return a_puissance_simulee and not a_capteur_reel_actif


def _etat_courant(equipement):
    etat = getattr(equipement, "etat", None)
    if etat:
        if etat.etat_rapporte != Equipement.Etat.UNKNOWN:
            return etat.etat_rapporte
        if etat.etat_souhaite != Equipement.Etat.UNKNOWN:
            return etat.etat_souhaite
    return Equipement.Etat.ON


def _puissance_estimee(equipement, etat_cible=None):
    etat = etat_cible or _etat_courant(equipement)
    puissance = (
        equipement.puissance_veille_kw
        if etat == Equipement.Etat.OFF
        else equipement.puissance_nominale_kw
    )
    return puissance * equipement.quantite


def _dans_plage_horaire(configuration, maintenant):
    if not configuration.horaires_actifs:
        return True
    if configuration.heure_debut is None or configuration.heure_fin is None:
        return False
    heure = maintenant.timetz().replace(tzinfo=None)
    debut = configuration.heure_debut
    fin = configuration.heure_fin
    if debut < fin:
        return debut <= heure < fin
    return heure >= debut or heure < fin


def _candidats_autorises(configuration):
    return list(
        configuration.equipements_autorises.filter(
            site=configuration.site,
            monitoring_active=True,
            capteurs__mode=Capteur.Mode.SIMULATED,
            capteurs__statut=Capteur.Statut.ACTIVE,
            capteurs__type__iexact="POWER",
        )
        .exclude(
            criticite__in=(Equipement.Criticite.HAUTE, Equipement.Criticite.CRITIQUE),
        )
        .exclude(
            pk__in=Capteur.objects.filter(
                mode=Capteur.Mode.REAL,
                statut=Capteur.Statut.ACTIVE,
            ).values("equipement_id")
        )
        .select_related("etat")
        .distinct()
        .order_by("-puissance_nominale_kw", "nom")
    )


def _raison_bloquante(configuration, equipements):
    if configuration.arret_urgence:
        return "Arrêt d'urgence activé : aucune décision ne peut être appliquée."
    if not equipements:
        return (
            "Aucun équipement autorisé, surveillé et non critique n'est disponible "
            "pour la simulation."
        )
    if (
        configuration.plafond_puissance_kw is None
        and not configuration.horaires_actifs
        and not configuration.confort_actif
        and not configuration.occupation_active
    ):
        return "Activez et configurez au moins une stratégie avant de lancer l'autonomie."
    if (
        configuration.horaires_actifs
        and (configuration.heure_debut is None or configuration.heure_fin is None)
    ):
        return "Les deux heures de la plage de fonctionnement doivent être configurées."
    if configuration.occupation_active and not configuration.zones_autorisees.exists():
        return "Autorisez au moins une zone avant d'activer la stratégie d'occupation."
    if configuration.confort_actif and (
        configuration.temperature_min_c is None
        or configuration.temperature_max_c is None
        or configuration.temperature_simulee_c is None
    ):
        return "Renseignez la température simulée et les deux limites de confort."
    if (
        configuration.confort_actif
        and configuration.temperature_min_c >= configuration.temperature_max_c
    ):
        return "La température minimale de confort doit être inférieure à la maximale."
    if configuration.confort_actif and not any(
        "CLIMAT" in equipement.categorie.upper() for equipement in equipements
    ):
        return "Le confort climatique requiert un climatiseur autorisé dans la simulation."
    if (
        configuration.strategie_automatique
        and not (
            configuration.horaires_actifs
            or configuration.confort_actif
            or configuration.plafond_puissance_kw is not None
            or configuration.occupation_active
        )
    ):
        return "L'arbitrage automatique nécessite au moins une stratégie configurée."
    return None


def _decision_occupation(configuration, equipements, maintenant):
    if not configuration.occupation_active:
        return None
    for zone in configuration.zones_autorisees.filter(
        site=configuration.site,
        active=True,
    ).order_by("nom"):
        occupation = obtenir_occupation_zone(
            zone,
            maintenant=maintenant,
            simulation_seule=True,
        )
        if occupation["state"] != ETAT_INOCCUPEE:
            continue
        for equipement in equipements:
            if (
                equipement.zone_id == zone.id
                and _etat_courant(equipement) == Equipement.Etat.ON
            ):
                return (
                    DecisionAutonome.Strategie.UNOCCUPIED_ZONE,
                    equipement,
                    Equipement.Etat.OFF,
                    f"La zone « {zone.nom} » est inoccupée selon des capteurs "
                    "simulés récents ; l'équipement non critique est arrêté "
                    "en simulation.",
                )
    return None


def _ajuster_etat(equipement, action, maintenant):
    etat, _ = EtatEquipement.objects.get_or_create(equipement=equipement)
    etat.etat_souhaite = action
    etat.etat_rapporte = action
    etat.date_etat_souhaite = maintenant
    etat.date_etat_rapporte = maintenant
    etat.puissance_actuelle_kw = (
        equipement.puissance_veille_kw
        if action == Equipement.Etat.OFF
        else equipement.puissance_nominale_kw
    ) * equipement.quantite
    _actualiser_statut_synchronisation(etat)
    etat.save(
        update_fields=(
            "etat_souhaite",
            "etat_rapporte",
            "date_etat_souhaite",
            "date_etat_rapporte",
            "puissance_actuelle_kw",
            "statut_synchronisation",
            "date_maj",
        )
    )
    equipement.etat = etat
    return etat


def _puissance_site(equipements):
    return sum(
        (_puissance_estimee(equipement) for equipement in equipements),
        Decimal("0"),
    )


def _decision_horaire(configuration, equipements, puissance_site, dans_horaires):
    if not configuration.horaires_actifs:
        return None
    cible = Equipement.Etat.ON if dans_horaires else Equipement.Etat.OFF
    for equipement in equipements:
        etat_actuel = _etat_courant(equipement)
        if etat_actuel == cible:
            continue
        variation = _puissance_estimee(equipement, cible) - _puissance_estimee(
            equipement,
            etat_actuel,
        )
        puissance_prevue = puissance_site + variation
        if (
            cible == Equipement.Etat.ON
            and configuration.plafond_puissance_kw is not None
            and puissance_prevue > configuration.plafond_puissance_kw
        ):
            continue
        return (
            DecisionAutonome.Strategie.AUTOMATIC
            if configuration.strategie_automatique
            else DecisionAutonome.Strategie.SCHEDULE,
            equipement,
            cible,
            (
                "L'équipement est remis en marche dans la plage horaire "
                f"{configuration.heure_debut}–{configuration.heure_fin}."
                if dans_horaires
                else "L'équipement est arrêté hors de la plage "
                f"{configuration.heure_debut}–{configuration.heure_fin}."
            ),
        )
    if dans_horaires:
        return (
            DecisionAutonome.Strategie.SYSTEM,
            None,
            "",
            "Le plafond de puissance empêche la remise en marche des équipements autorisés.",
        )
    return None


def _choisir_decision(configuration, equipements, maintenant):
    puissance_site = _puissance_site(equipements)
    dans_horaires = _dans_plage_horaire(configuration, maintenant)
    occupation = _decision_occupation(configuration, equipements, maintenant)
    if occupation:
        return occupation
    climatiseurs = [
        equipment
        for equipment in equipements
        if "CLIMAT" in equipment.categorie.upper()
    ]
    hors_confort = None
    if configuration.confort_actif:
        temperature = configuration.temperature_simulee_c
        if temperature > configuration.temperature_max_c:
            hors_confort = Equipement.Etat.ON
        elif temperature < configuration.temperature_min_c:
            hors_confort = Equipement.Etat.OFF

    if configuration.strategie_automatique:
        if (
            configuration.plafond_puissance_kw is not None
            and puissance_site > configuration.plafond_puissance_kw
        ):
            candidates = [
                item
                for item in equipements
                if _etat_courant(item) == Equipement.Etat.ON
            ]
            if candidates:
                return (
                    DecisionAutonome.Strategie.AUTOMATIC,
                    candidates[0],
                    Equipement.Etat.OFF,
                    "L'arbitrage automatique réduit le dépassement du plafond : "
                    f"{puissance_site:.3f} kW estimés pour "
                    f"{configuration.plafond_puissance_kw} kW configurés.",
                )

        if hors_confort and (hors_confort == Equipement.Etat.OFF or dans_horaires):
            candidates = [
                item for item in climatiseurs if _etat_courant(item) != hors_confort
            ]
            if candidates:
                return (
                    DecisionAutonome.Strategie.AUTOMATIC,
                    candidates[0],
                    hors_confort,
                    "L'arbitrage automatique traite le confort climatique : "
                    f"{configuration.temperature_simulee_c} °C est hors de la plage "
                    f"{configuration.temperature_min_c}–{configuration.temperature_max_c} °C.",
                )

        horaire = _decision_horaire(
            configuration,
            equipements,
            puissance_site,
            dans_horaires,
        )
        if horaire:
            return horaire
        return None, None, "", "Aucune stratégie ne requiert d'action."

    if (
        configuration.plafond_puissance_kw is not None
        and puissance_site > configuration.plafond_puissance_kw
    ):
        candidates = [
            item
            for item in equipements
            if _etat_courant(item) == Equipement.Etat.ON
        ]
        if candidates:
            return (
                DecisionAutonome.Strategie.POWER_CAP,
                candidates[0],
                Equipement.Etat.OFF,
                f"La puissance simulée ({puissance_site:.3f} kW) dépasse le plafond "
                f"configuré ({configuration.plafond_puissance_kw} kW).",
            )

    if hors_confort and (hors_confort == Equipement.Etat.OFF or dans_horaires):
        candidates = [
            item for item in climatiseurs if _etat_courant(item) != hors_confort
        ]
        if candidates:
            return (
                DecisionAutonome.Strategie.COMFORT,
                candidates[0],
                hors_confort,
                "La température simulée est hors de la plage de confort configurée "
                f"({configuration.temperature_simulee_c} °C).",
            )

    horaire = _decision_horaire(
        configuration,
        equipements,
        puissance_site,
        dans_horaires,
    )
    if horaire:
        return horaire

    if (
        configuration.plafond_puissance_kw is not None
        and puissance_site > configuration.plafond_puissance_kw
    ):
        return (
            DecisionAutonome.Strategie.SYSTEM,
            None,
            "",
            "Le plafond reste dépassé, mais aucun équipement autorisé ne peut être arrêté.",
        )
    return None, None, "", "La consommation, les horaires et le confort simulés respectent les limites."


@transaction.atomic
def evaluer_pilotage_autonome(site_id, force=False, maintenant=None):
    """Évalue une politique et applique au plus une transition d'état simulée."""
    maintenant = maintenant or timezone.now()
    configuration = (
        PilotageAutonome.objects.select_for_update()
        .select_related("site")
        .get(site_id=site_id)
    )
    if (
        not force
        and configuration.derniere_evaluation
        and (maintenant - configuration.derniere_evaluation).total_seconds()
        < INTERVALLE_EVALUATION_SECONDES
    ):
        return None

    configuration.derniere_evaluation = maintenant
    configuration.save(update_fields=("derniere_evaluation", "date_maj"))
    equipements_site = list(
        Equipement.objects.filter(site_id=site_id, monitoring_active=True)
        .select_related("etat")
        .order_by("nom")
    )
    autorises = _candidats_autorises(configuration)
    puissance_avant = _puissance_site(equipements_site)
    temperature = configuration.temperature_simulee_c

    if configuration.arret_urgence:
        statut = DecisionAutonome.Statut.BLOCKED
        strategie = DecisionAutonome.Strategie.SYSTEM
        equipement = None
        action = ""
        raison = "Arrêt d'urgence activé : aucune action simulée n'est appliquée."
    elif not configuration.actif:
        statut = DecisionAutonome.Statut.BLOCKED
        strategie = DecisionAutonome.Strategie.SYSTEM
        equipement = None
        action = ""
        raison = "L'autonomie est désactivée ; aucune action n'est appliquée."
    else:
        raison_bloquante = _raison_bloquante(configuration, autorises)
        if raison_bloquante:
            statut = DecisionAutonome.Statut.BLOCKED
            strategie = DecisionAutonome.Strategie.SYSTEM
            equipement = None
            action = ""
            raison = raison_bloquante
        else:
            fuseau = ZoneInfo(configuration.site.fuseau_horaire or "UTC")
            heure_locale = maintenant.astimezone(fuseau)
            strategie, equipement, action, raison = _choisir_decision(
                configuration,
                autorises,
                heure_locale,
            )
            if equipement is None:
                statut = (
                    DecisionAutonome.Statut.BLOCKED
                    if strategie == DecisionAutonome.Strategie.SYSTEM
                    else DecisionAutonome.Statut.NO_ACTION
                )
                action = ""
            elif configuration.arret_urgence:
                statut = DecisionAutonome.Statut.BLOCKED
                action = ""
                equipement = None
                raison = "Arrêt d'urgence activé avant l'application de l'action."
            else:
                statut = DecisionAutonome.Statut.APPLIED
                nouvel_etat = _ajuster_etat(equipement, action, maintenant)
                for item in equipements_site:
                    if item.pk == equipement.pk:
                        item.etat = nouvel_etat
                        break

    puissance_apres = _puissance_site(equipements_site)
    return DecisionAutonome.objects.create(
        site_id=site_id,
        equipement=equipement,
        strategie=strategie or DecisionAutonome.Strategie.SYSTEM,
        statut=statut,
        action=action,
        raison=raison,
        puissance_avant_kw=puissance_avant,
        puissance_apres_kw=puissance_apres,
        temperature_simulee_c=temperature,
        date_creation=maintenant,
    )
