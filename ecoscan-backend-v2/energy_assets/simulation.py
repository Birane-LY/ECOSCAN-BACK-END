"""Génération de télémétrie pour les capteurs simulés."""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Capteur, Equipement, EtatEquipement, MesureCapteur
from .services import synchroniser_etat_equipement


PRECISION_KWH = Decimal("0.000001")


def _puissance_simulee_kw(equipement, etat):
    """Déduit la puissance simulée de l'état choisi et de la puissance nominale."""
    etat_simule = etat.etat_souhaite
    if etat_simule == Equipement.Etat.UNKNOWN:
        etat_simule = etat.etat_rapporte
    if etat_simule == Equipement.Etat.UNKNOWN:
        etat_simule = Equipement.Etat.ON

    if etat_simule == Equipement.Etat.OFF:
        puissance_unitaire = equipement.puissance_veille_kw
    else:
        puissance_unitaire = equipement.puissance_nominale_kw
    return puissance_unitaire * equipement.quantite


def _mesure_energie_kwh(derniere_mesure, equipement, etat, maintenant):
    """Intègre la puissance simulée depuis le dernier index d'énergie."""
    if derniere_mesure is None:
        return etat.energie_cumulee_kwh

    duree_heures = Decimal(
        str((maintenant - derniere_mesure.date_mesure).total_seconds())
    ) / Decimal("3600")
    energie_precedente = derniere_mesure.valeur
    if derniere_mesure.unite.strip().casefold() == "wh":
        energie_precedente /= Decimal("1000")
    return (
        energie_precedente
        + _puissance_simulee_kw(equipement, etat) * duree_heures
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
        valeur = _puissance_simulee_kw(equipement, etat)
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


def generer_mesures_capteurs_simules(maintenant=None):
    """Émet un relevé pour chaque capteur simulé arrivé à échéance."""
    maintenant = maintenant or timezone.now()
    capteur_ids = Capteur.objects.filter(
        mode=Capteur.Mode.SIMULATED,
        statut=Capteur.Statut.ACTIVE,
    ).values_list("id", flat=True)
    mesures_generees = 0

    for capteur_id in capteur_ids.iterator():
        if generer_mesure_simulee(capteur_id, maintenant=maintenant) is not None:
            mesures_generees += 1
    return mesures_generees
