from decimal import Decimal

from django.db import transaction

from .models import Capteur, Equipement, EtatEquipement, MesureCapteur


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
        if etat.etat_souhaite == Equipement.Etat.UNKNOWN:
            etat.statut_synchronisation = EtatEquipement.StatutSynchronisation.UNKNOWN
        elif etat.etat_souhaite == etat.etat_rapporte:
            etat.statut_synchronisation = (
                EtatEquipement.StatutSynchronisation.SYNCHRONIZED
            )
        else:
            etat.statut_synchronisation = (
                EtatEquipement.StatutSynchronisation.OUT_OF_SYNC
            )
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
