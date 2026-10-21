"""Génération de télémétrie pour les capteurs simulés."""

import math
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import (
    Capteur,
    CapteurPresence,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    MesurePresence,
)
from .services import synchroniser_etat_equipement


PRECISION_KWH = Decimal("0.000001")
PRECISION_KW = Decimal("0.000001")


def _puissance_simulee_kw(equipement, etat, maintenant):
    """Estime une charge variable mais bornée pour un équipement simulé."""
    etat_simule = etat.etat_souhaite
    if etat_simule == Equipement.Etat.UNKNOWN:
        etat_simule = etat.etat_rapporte
    if etat_simule == Equipement.Etat.UNKNOWN:
        etat_simule = Equipement.Etat.ON

    if etat_simule == Equipement.Etat.OFF:
        return equipement.puissance_veille_kw * equipement.quantite

    puissance_nominale = equipement.puissance_nominale_kw * equipement.quantite
    if puissance_nominale <= 0:
        return puissance_nominale

    phase = (equipement.pk.int % 10000) / 10000 * 2 * math.pi
    secondes = maintenant.timestamp()
    variation = (
        0.055 * math.sin(2 * math.pi * secondes / 90 + phase)
        + 0.025 * math.sin(2 * math.pi * secondes / 240 + phase * 1.7)
    )
    facteur_charge = Decimal(str(1 + variation))
    return (puissance_nominale * facteur_charge).quantize(PRECISION_KW)


def _mesure_energie_kwh(derniere_mesure, equipement, etat, maintenant):
    """Intègre la puissance moyenne simulée depuis le dernier index d'énergie."""
    if derniere_mesure is None:
        return etat.energie_cumulee_kwh

    duree_heures = Decimal(
        str((maintenant - derniere_mesure.date_mesure).total_seconds())
    ) / Decimal("3600")
    energie_precedente = derniere_mesure.valeur
    if derniere_mesure.unite.strip().casefold() == "wh":
        energie_precedente /= Decimal("1000")
    puissance_debut = _puissance_simulee_kw(
        equipement,
        etat,
        derniere_mesure.date_mesure,
    )
    puissance_fin = _puissance_simulee_kw(equipement, etat, maintenant)
    puissance_moyenne = (puissance_debut + puissance_fin) / Decimal("2")
    return (
        energie_precedente
        + puissance_moyenne * duree_heures
    ).quantize(PRECISION_KWH)


@transaction.atomic
def generer_mesure_simulee(capteur_id, maintenant=None):
    """Crée un relevé si le capteur est simulé, actif et arrivé à échéance."""
    maintenant = maintenant or timezone.now()
    capteur = (
        Capteur.objects.select_for_update()
        .select_related("equipement")
        .get(pk=capteur_id)
    )
    if (
        capteur.mode != Capteur.Mode.SIMULATED
        or capteur.statut != Capteur.Statut.ACTIVE
    ):
        return None

    type_capteur = capteur.type.strip().upper()
    if type_capteur not in ("POWER", "ENERGY"):
        return None
    if capteur.frequence_secondes <= 0:
        raise ValueError(
            f"Le capteur {capteur.identifiant} doit avoir une fréquence positive."
        )

    derniere_mesure = (
        MesureCapteur.objects.filter(capteur=capteur)
        .order_by("-date_mesure", "-date_reception")
        .first()
    )
    if derniere_mesure and maintenant < (
        derniere_mesure.date_mesure
        + timedelta(seconds=capteur.frequence_secondes)
    ):
        return None

    equipement = Equipement.objects.select_for_update().get(
        pk=capteur.equipement_id
    )
    etat, _ = EtatEquipement.objects.get_or_create(equipement=equipement)
    if type_capteur == "POWER":
        valeur = _puissance_simulee_kw(equipement, etat, maintenant)
        unite = "kW"
    else:
        valeur = _mesure_energie_kwh(
            derniere_mesure,
            equipement,
            etat,
            maintenant,
        )
        unite = "kWh"

    mesure = MesureCapteur.objects.create(
        capteur=capteur,
        valeur=valeur,
        unite=unite,
        date_mesure=maintenant,
    )
    capteur.derniere_communication = maintenant
    capteur.save(update_fields=("derniere_communication",))
    synchroniser_etat_equipement(mesure)
    return mesure


def generer_mesures_capteurs_simules(maintenant=None, organisation_id=None):
    """Émet un relevé pour chaque capteur simulé arrivé à échéance."""
    maintenant = maintenant or timezone.now()
    capteurs = Capteur.objects.filter(
        mode=Capteur.Mode.SIMULATED,
        statut=Capteur.Statut.ACTIVE,
    )
    if organisation_id:
        capteurs = capteurs.filter(
            equipement__site__organisation_id=organisation_id,
        )
    capteur_ids = capteurs.values_list("id", flat=True)
    mesures_generees = 0

    for capteur_id in capteur_ids.iterator():
        if generer_mesure_simulee(capteur_id, maintenant=maintenant) is not None:
            mesures_generees += 1
    return mesures_generees


@transaction.atomic
def generer_mesure_presence_simulee(capteur_id, maintenant=None):
    """Émet l'état configuré d'un capteur de présence simulé arrivé à échéance."""
    maintenant = maintenant or timezone.now()
    capteur = CapteurPresence.objects.select_for_update().get(pk=capteur_id)
    if (
        capteur.mode != CapteurPresence.Mode.SIMULATED
        or capteur.statut != CapteurPresence.Statut.ACTIVE
        or capteur.occupation_simulee is None
    ):
        return None
    if capteur.frequence_secondes <= 0:
        raise ValueError(
            f"Le capteur {capteur.identifiant} doit avoir une fréquence positive."
        )

    derniere_mesure = (
        MesurePresence.objects.filter(capteur=capteur)
        .order_by("-date_mesure", "-date_reception")
        .first()
    )
    if derniere_mesure and maintenant < (
        derniere_mesure.date_mesure
        + timedelta(seconds=capteur.frequence_secondes)
    ):
        return None

    mesure = MesurePresence.objects.create(
        capteur=capteur,
        occupee=capteur.occupation_simulee,
        date_mesure=maintenant,
    )
    capteur.derniere_communication = maintenant
    capteur.save(update_fields=("derniere_communication",))
    return mesure


def generer_mesures_presence_simulees(maintenant=None, organisation_id=None):
    """Émet les états configurés des capteurs de présence simulés actifs."""
    capteurs = CapteurPresence.objects.filter(
        mode=CapteurPresence.Mode.SIMULATED,
        statut=CapteurPresence.Statut.ACTIVE,
    )
    if organisation_id:
        capteurs = capteurs.filter(zone__site__organisation_id=organisation_id)
    mesures_generees = 0
    for capteur_id in capteurs.values_list("id", flat=True).iterator():
        if generer_mesure_presence_simulee(
            capteur_id,
            maintenant=maintenant,
        ) is not None:
            mesures_generees += 1
    return mesures_generees
