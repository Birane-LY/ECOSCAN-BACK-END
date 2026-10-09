"""Estimation virtuelle de l'impact énergétique d'un changement d'état."""

from decimal import Decimal

from django.db import transaction

from .models import ActionVirtuelle, Equipement, EtatEquipement


@transaction.atomic
def simuler_action_virtuelle(
    recommandation,
    equipement,
    etat_cible,
    duree_heures,
    utilisateur,
):
    """Enregistre un scénario sans modifier l'état ni créer de commande matérielle."""
    organisation_recommandation_id = recommandation.objectif.organisation_id
    if equipement.site.organisation_id != organisation_recommandation_id:
        raise ValueError(
            "L'équipement et la recommandation doivent appartenir à la même organisation."
        )

    try:
        etat = EtatEquipement.objects.select_for_update().get(equipement=equipement)
    except EtatEquipement.DoesNotExist as exc:
        raise ValueError(
            "L'équipement doit avoir un état connu avant de simuler une action."
        ) from exc

    if etat.etat_rapporte == Equipement.Etat.ON:
        if etat.puissance_actuelle_kw > 0:
            puissance_reference = etat.puissance_actuelle_kw
            source_reference = ActionVirtuelle.SourcePuissance.MESURE
        else:
            puissance_reference = (
                equipement.puissance_nominale_kw * equipement.quantite
            )
            source_reference = ActionVirtuelle.SourcePuissance.NOMINALE
    elif etat.etat_rapporte == Equipement.Etat.OFF:
        puissance_reference = equipement.puissance_veille_kw * equipement.quantite
        source_reference = ActionVirtuelle.SourcePuissance.VEILLE
    else:
        raise ValueError(
            "La simulation exige un état rapporté ON ou OFF pour l'équipement."
        )

    if etat_cible == etat.etat_rapporte:
        puissance_scenario = puissance_reference
    elif etat_cible == ActionVirtuelle.EtatCible.ON:
        puissance_scenario = (
            equipement.puissance_nominale_kw * equipement.quantite
        )
    else:
        puissance_scenario = equipement.puissance_veille_kw * equipement.quantite

    variation_energie = (
        (puissance_reference - puissance_scenario) * duree_heures
    ).quantize(Decimal("0.000001"))

    return ActionVirtuelle.objects.create(
        recommandation=recommandation,
        equipement=equipement,
        etat_cible=etat_cible,
        duree_heures=duree_heures,
        puissance_reference_kw=puissance_reference,
        puissance_scenario_kw=puissance_scenario,
        variation_energie_kwh=variation_energie,
        source_puissance_reference=source_reference,
        cree_par=utilisateur,
    )
