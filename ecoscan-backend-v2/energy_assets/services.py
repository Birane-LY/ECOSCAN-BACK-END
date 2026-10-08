from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import (
    Capteur,
    CommandeEquipement,
    Equipement,
    EtatEquipement,
    MesureCapteur,
)


def _actualiser_statut_synchronisation(etat):
    if etat.etat_rapporte == Equipement.Etat.UNKNOWN:
        etat.statut_synchronisation = EtatEquipement.StatutSynchronisation.UNKNOWN
    elif etat.etat_rapporte == etat.etat_souhaite:
        etat.statut_synchronisation = (
            EtatEquipement.StatutSynchronisation.SYNCHRONIZED
        )
    else:
        etat.statut_synchronisation = (
            EtatEquipement.StatutSynchronisation.OUT_OF_SYNC
        )


def convertir_mesure(type_capteur, valeur, unite):
    type_capteur = type_capteur.strip().upper()
    unite = unite.strip().casefold()

    if type_capteur == "POWER":
        if valeur < 0:
            raise ValueError("Une mesure de puissance ne peut pas être négative.")
        if unite == "w":
            return "POWER", valeur / Decimal("1000")
        if unite == "kw":
            return "POWER", valeur
        raise ValueError("Un capteur POWER doit utiliser W ou kW.")

    if type_capteur == "ENERGY":
        if valeur < 0:
            raise ValueError("Une mesure d'énergie ne peut pas être négative.")
        if unite == "wh":
            return "ENERGY", valeur / Decimal("1000")
        if unite == "kwh":
            return "ENERGY", valeur
        raise ValueError("Un capteur ENERGY doit utiliser Wh ou kWh.")

    return None


@transaction.atomic
def synchroniser_etat_equipement(mesure):
    conversion = convertir_mesure(
        mesure.capteur.type,
        mesure.valeur,
        mesure.unite,
    )
    if conversion is None:
        return

    type_mesure, valeur_standard = conversion
    equipement = Equipement.objects.select_for_update().get(
        pk=mesure.capteur.equipement_id
    )
    etat, _ = EtatEquipement.objects.get_or_create(equipement=equipement)
    derniere_mesure = (
        MesureCapteur.objects.filter(
            capteur__equipement_id=equipement.id,
            capteur__type__iexact=mesure.capteur.type,
        )
        .exclude(pk=mesure.pk)
        .order_by("-date_mesure", "-date_reception")
        .first()
    )
    if derniere_mesure and (
        derniere_mesure.date_mesure,
        derniere_mesure.date_reception,
    ) >= (mesure.date_mesure, mesure.date_reception):
        return

    fields_to_update = []
    if type_mesure == "POWER":
        etat.date_etat_rapporte = mesure.date_mesure
        etat.puissance_actuelle_kw = valeur_standard
        etat.etat_rapporte = (
            Equipement.Etat.ON if valeur_standard > 0 else Equipement.Etat.OFF
        )
        _actualiser_statut_synchronisation(etat)
        fields_to_update.extend(
            (
                "date_etat_rapporte",
                "puissance_actuelle_kw",
                "etat_rapporte",
                "statut_synchronisation",
            )
        )
    else:
        etat.energie_cumulee_kwh = valeur_standard
        fields_to_update.append("energie_cumulee_kwh")

    etat.save(update_fields=(*fields_to_update, "date_maj"))


@transaction.atomic
def demander_commande(equipement, action, utilisateur):
    equipement = Equipement.objects.select_for_update().get(pk=equipement.pk)
    etat, _ = EtatEquipement.objects.get_or_create(equipement=equipement)
    maintenant = timezone.now()

    etat.etat_souhaite = action
    etat.date_etat_souhaite = maintenant
    _actualiser_statut_synchronisation(etat)
    etat.save(
        update_fields=(
            "etat_souhaite",
            "date_etat_souhaite",
            "statut_synchronisation",
            "date_maj",
        )
    )
    return CommandeEquipement.objects.create(
        equipement=equipement,
        action=action,
        demande_par=utilisateur,
    )


@transaction.atomic
def prendre_prochaine_commande():
    commande = (
        CommandeEquipement.objects.select_for_update()
        .filter(statut=CommandeEquipement.Statut.PENDING)
        .order_by("date_creation", "id")
        .first()
    )
    if commande is None:
        return None
    return marquer_commande_envoyee(commande)


@transaction.atomic
def marquer_commande_envoyee(commande):
    commande = CommandeEquipement.objects.select_for_update().get(pk=commande.pk)
    if commande.statut != CommandeEquipement.Statut.PENDING:
        raise ValueError("Seule une commande en attente peut être envoyée.")

    commande.statut = CommandeEquipement.Statut.SENT
    commande.date_envoi = timezone.now()
    commande.save(update_fields=("statut", "date_envoi"))
    return commande


@transaction.atomic
def confirmer_commande(commande):
    commande = CommandeEquipement.objects.select_for_update().get(pk=commande.pk)
    if commande.statut != CommandeEquipement.Statut.SENT:
        raise ValueError("Seule une commande envoyée peut être confirmée.")

    maintenant = timezone.now()
    commande.statut = CommandeEquipement.Statut.CONFIRMED
    commande.date_finalisation = maintenant
    commande.save(update_fields=("statut", "date_finalisation"))

    etat = EtatEquipement.objects.select_for_update().get(
        equipement=commande.equipement
    )
    etat.etat_rapporte = commande.action
    etat.date_etat_rapporte = maintenant
    _actualiser_statut_synchronisation(etat)
    etat.save(
        update_fields=(
            "etat_rapporte",
            "date_etat_rapporte",
            "statut_synchronisation",
            "date_maj",
        )
    )
    return commande


@transaction.atomic
def echouer_commande(commande, detail):
    commande = CommandeEquipement.objects.select_for_update().get(pk=commande.pk)
    if commande.statut not in (
        CommandeEquipement.Statut.PENDING,
        CommandeEquipement.Statut.SENT,
    ):
        raise ValueError("Seule une commande en attente ou envoyée peut échouer.")
    if not detail or not detail.strip():
        raise ValueError("Le motif de l'échec est obligatoire.")

    commande.statut = CommandeEquipement.Statut.FAILED
    commande.date_finalisation = timezone.now()
    commande.detail_echec = detail.strip()
    commande.save(
        update_fields=("statut", "date_finalisation", "detail_echec")
    )
    return commande
