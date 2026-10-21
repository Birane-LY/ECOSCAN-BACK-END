"""Calcule et notifie les alertes énergétiques avec provenance explicite."""

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from accounts.api.n8n_client import _post_event
from analysis.models import ResultatMetrique
from analysis.services.progression_objectifs import calculer_progression_mesuree
from audit.models import JournalAudit
from audit.services import enregistrer_evenement
from energy.models import Objectif, ReleveSolde
from energy.services.autonomie import estimer_autonomie
from energy.services.prediction_tranches import etat_tranche
from energy_assets.models import Capteur, MesureCapteur
from organizations.models import Compteur

logger = logging.getLogger(__name__)

JOURS_MAX_RELEVE_WOYOFAL = 3
JOURS_ALERTE_AUTONOMIE = 3
SEUIL_SOLDE_BAS_KWH = Decimal("5")
SEUIL_ECART_FACTURE_POURCENT = 15.0
ECART_MAX_BORDURE_JOURS = 1


def _cle_evenement(organisation, type_alerte, reference):
    brut = f"{organisation.pk}:{type_alerte}:{reference}"
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


def _notifier(organisation, type_alerte, reference, titre, message, severite, contexte):
    """Envoie une fois par état d'alerte; un échec reste réessayable."""
    cle = _cle_evenement(organisation, type_alerte, reference)
    if JournalAudit.objects.filter(
        action="NOTIFICATION_ALERTE_ENERGETIQUE",
        ressource="AlerteEnergetique",
        identifiant_ressource=cle,
        resultat=JournalAudit.Resultat.SUCCES,
    ).exists():
        return "deja_notifiee"

    emails_admin = list(
        organisation.membres.filter(
            utilisateur__role="ADMIN_ORGANISATION",
            utilisateur__actif=True,
        )
        .order_by("utilisateur__email")
        .values_list("utilisateur__email", flat=True)
        .distinct()
    )
    if not emails_admin:
        logger.warning(
            "Aucun administrateur actif pour l'alerte %s de l'organisation %s.",
            type_alerte,
            organisation.pk,
        )
        return "sans_destinataire"

    payload = {
        "alerte": {
            "id": cle,
            "type": type_alerte,
            "titre": titre,
            "message": message,
            "severite": severite,
            "contexte": contexte,
        },
        "organisation": {
            "id": str(organisation.pk),
            "nom": organisation.nom,
            "emails_admin": emails_admin,
        },
    }
    succes = _post_event("/alerte-energetique", payload)
    enregistrer_evenement(
        action="NOTIFICATION_ALERTE_ENERGETIQUE",
        ressource="AlerteEnergetique",
        identifiant_ressource=cle,
        organisation=organisation,
        resultat=(
            JournalAudit.Resultat.SUCCES
            if succes else JournalAudit.Resultat.ECHEC
        ),
        details={
            "type": type_alerte,
            "reference": str(reference),
            "severite": severite,
            "notification_envoyee": succes,
        },
    )
    return "notifiee" if succes else "echec_notification"


def _alerter_woyofal(date_aujourd_hui):
    resultats = {"notifiees": 0, "ignorees": 0}
    compteurs_ids = (
        ReleveSolde.objects.filter(
            date_releve__gte=timezone.now() - timedelta(days=JOURS_MAX_RELEVE_WOYOFAL)
        )
        .values_list("compteur_id", flat=True)
        .distinct()
    )
    for compteur in Compteur.objects.filter(id__in=compteurs_ids).select_related(
        "site__organisation"
    ):
        dernier = ReleveSolde.objects.filter(compteur=compteur).order_by("-date_releve").first()
        if dernier is None or dernier.date_releve < timezone.now() - timedelta(
            days=JOURS_MAX_RELEVE_WOYOFAL
        ):
            resultats["ignorees"] += 1
            continue

        autonomie = estimer_autonomie(compteur)
        jours = autonomie.get("jours_restants")
        solde = Decimal(str(autonomie.get("solde_kwh", dernier.kwh_restants)))
        if not (
            solde <= SEUIL_SOLDE_BAS_KWH
            or (jours is not None and jours <= JOURS_ALERTE_AUTONOMIE)
        ):
            continue

        etat = "solde-bas" if solde <= SEUIL_SOLDE_BAS_KWH else "epuisement-proche"
        jour = date_aujourd_hui.isoformat()
        confiance = autonomie.get("fiabilite", "insuffisante")
        message = (
            f"Le dernier solde Woyofal déclaré est de {solde} kWh. "
            f"Épuisement estimé dans {jours} jour(s). "
            f"Fiabilité de l'estimation : {confiance}. "
            "Cette estimation dépend de relevés déclarés et ne constitue pas une mesure directe."
            if jours is not None else
            f"Le dernier solde Woyofal déclaré est de {solde} kWh. "
            "Le solde est bas; les relevés disponibles ne permettent pas encore d'estimer "
            "la date d'épuisement."
        )
        statut = _notifier(
            compteur.site.organisation,
            "WOYOFAL_AUTONOMIE",
            f"{compteur.pk}:{etat}:{jour}",
            "Solde Woyofal à surveiller",
            message,
            "AVERTISSEMENT",
            {
                "compteur_id": str(compteur.pk),
                "site_id": str(compteur.site_id),
                "solde_kwh": str(solde),
                "jours_restants_estimes": jours,
                "fiabilite": confiance,
                "date_dernier_releve": dernier.date_releve.isoformat(),
                "provenance": "releves_de_solde_declares",
            },
        )
        resultats["notifiees"] += statut == "notifiee"
    return resultats


def _alerter_tranches(date_aujourd_hui):
    resultats = {"notifiees": 0, "ignorees": 0}
    compteurs = Compteur.objects.filter(
        categorie_tarifaire__gt="",
    ).select_related("site__organisation").distinct()
    for compteur in compteurs:
        if not compteur.mode_paiement:
            resultats["ignorees"] += 1
            continue
        etat = etat_tranche(compteur)
        if etat.get("erreur"):
            resultats["ignorees"] += 1
            continue

        restant = etat.get("kwh_restants_dans_tranche")
        if (
            restant is None
            or etat.get("prochain_prix") is None
            or restant > SEUIL_PROXIMITE_TRANCHE_KWH
        ):
            resultats["ignorees"] += 1
            continue

        jour = date_aujourd_hui.isoformat()
        type_alerte = f"{compteur.mode_paiement}_PROXIMITE_TRANCHE"
        provenance = etat.get("provenance", "non_certifiee")
        message = (
            f"Les mesures du compteur approchent la limite de {etat['tranche']} "
            f"({restant:.1f} kWh estimés avant le seuil). Le tarif suivant est "
            f"{etat['prochain_prix']} FCFA/kWh pour le mode {compteur.mode_paiement}. "
            "Vérifiez le contrat et le barème actif du compteur. "
            + (
                "Les relevés proviennent d'un scénario simulé."
                if provenance == "SIMULATED" else
                "La source est déclarée mais n'est pas certifiée par cette alerte."
            )
        )
        statut = _notifier(
            compteur.site.organisation,
            type_alerte,
            f"{compteur.pk}:{etat['tranche']}:{provenance}:{jour}",
            "Approche d'un seuil de tranche énergétique",
            message,
            "SCENARIO" if provenance == "SIMULATED" else "AVERTISSEMENT",
            {
                "compteur_id": str(compteur.pk),
                "categorie_tarifaire": etat.get("categorie_tarifaire"),
                "mode_paiement": compteur.mode_paiement,
                "tranche": etat["tranche"],
                "kwh_restants_estimes": restant,
                "prix_tranche_suivante_fcfa_kwh": etat["prochain_prix"],
                "cumul_kwh_credites_mois": etat.get("cumul_kwh_credites_mois"),
                "cumul_kwh_mesures_mois": etat.get("cumul_kwh_mesures_mois"),
                "provenance": provenance,
            },
        )
        resultats["notifiees"] += statut == "notifiee"
    return resultats


def comparer_facture_telemetrie(resultat):
    """Compare une facture et un seul capteur cumulatif sur sa période exacte."""
    if (
        resultat.code_metrique != "consommation_facture_periodique"
        or resultat.valeur is None
        or resultat.statut_qualite != ResultatMetrique.StatutQualite.FIABLE
        or resultat.compteur_id is None
    ):
        return {"statut": "donnees_insuffisantes", "raison": "Facture fiable ou compteur attribué manquant."}
    fichier_source = resultat.fichier_source
    try:
        import_instance = fichier_source.import_donnees if fichier_source else None
    except ObjectDoesNotExist:
        import_instance = None
    rapport = getattr(import_instance, "rapport_analyse", {}) or {}
    controle = rapport.get("controle_document", {}) if isinstance(rapport, dict) else {}
    if not isinstance(controle, dict) or controle.get("emetteur_confirme") != "SENELEC":
        return {
            "statut": "donnees_insuffisantes",
            "raison": "La comparaison facture-monitoring est réservée aux factures SENELEC revues.",
        }
    if resultat.compteur.site.compteurs.count() != 1:
        return {
            "statut": "donnees_insuffisantes",
            "raison": "Plusieurs compteurs sur le site empêchent d'attribuer le capteur à cette facture.",
        }

    capteur, raison = _capteur_energy_unique(resultat.compteur.site_id)
    if capteur is None:
        return {"statut": "donnees_insuffisantes", "raison": raison}

    debut = resultat.periode_debut
    fin = resultat.periode_fin
    if fin <= debut:
        return {"statut": "donnees_insuffisantes", "raison": "Période de facture incohérente."}
    bordure = timedelta(days=ECART_MAX_BORDURE_JOURS)
    mesures = list(
        MesureCapteur.objects.filter(
            capteur=capteur,
            date_mesure__gte=debut - bordure,
            date_mesure__lte=fin + bordure,
        ).order_by("date_mesure")
    )
    avant = [m for m in mesures if m.date_mesure <= debut]
    apres = [m for m in mesures if m.date_mesure >= fin]
    dedans = [m for m in mesures if debut <= m.date_mesure <= fin]
    if not avant or not apres or len(dedans) < 3:
        return {
            "statut": "donnees_insuffisantes",
            "raison": "Couverture insuffisante : index avant/après et au moins trois mesures sur la période requis.",
        }

    mesure_debut, mesure_fin = avant[-1], apres[0]
    if debut - mesure_debut.date_mesure > bordure or mesure_fin.date_mesure - fin > bordure:
        return {"statut": "donnees_insuffisantes", "raison": "Index de bordure trop éloignés de la période facturée."}
    duree = fin - debut
    duree_couverte = dedans[-1].date_mesure - dedans[0].date_mesure
    if duree_couverte < duree * 0.8:
        return {
            "statut": "donnees_insuffisantes",
            "raison": "Les mesures disponibles ne couvrent pas suffisamment la durée facturée.",
        }
    points_couverture = [mesure_debut, *dedans, mesure_fin]
    if any(
        suivant.date_mesure - precedent.date_mesure > timedelta(days=7)
        for precedent, suivant in zip(points_couverture, points_couverture[1:])
    ):
        return {
            "statut": "donnees_insuffisantes",
            "raison": "Un intervalle de plus de sept jours sans mesure empêche une comparaison fiable.",
        }

    def valeur_kwh(mesure):
        unite = (mesure.unite or "").strip().lower()
        if unite == "kwh":
            return Decimal(mesure.valeur)
        if unite == "wh":
            return Decimal(mesure.valeur) / Decimal("1000")
        return None

    index_debut, index_fin = valeur_kwh(mesure_debut), valeur_kwh(mesure_fin)
    if index_debut is None or index_fin is None:
        return {"statut": "donnees_insuffisantes", "raison": "Unité de capteur non reconnue (kWh ou Wh requis)."}
    if index_fin < index_debut:
        return {"statut": "donnees_incoherentes", "raison": "Index cumulatif décroissant sur la période."}

    mesure_kwh = index_fin - index_debut
    ecart_kwh = mesure_kwh - Decimal(resultat.valeur)
    ecart_pct = abs(ecart_kwh) / Decimal(resultat.valeur) * 100 if resultat.valeur else None
    return {
        "statut": "mesure_reelle" if capteur.mode == Capteur.Mode.REAL else "scenario_simule",
        "provenance": capteur.mode,
        "capteur_id": str(capteur.pk),
        "periode_debut": debut.isoformat(),
        "periode_fin": fin.isoformat(),
        "facture_kwh": float(resultat.valeur),
        "telemetrie_kwh": float(mesure_kwh),
        "ecart_kwh": float(ecart_kwh),
        "ecart_pourcentage": float(ecart_pct) if ecart_pct is not None else None,
        "mesures_periode": len(dedans),
        "raison": None,
    }


def _capteur_energy_unique(site_id):
    capteurs = list(
        Capteur.objects.filter(
            equipement__site_id=site_id,
            type__iexact="ENERGY",
            statut=Capteur.Statut.ACTIVE,
        ).order_by("mode", "id")
    )
    reels = [capteur for capteur in capteurs if capteur.mode == Capteur.Mode.REAL]
    selection = reels or [capteur for capteur in capteurs if capteur.mode == Capteur.Mode.SIMULATED]
    if len(selection) != 1:
        raison = (
            "Plusieurs capteurs ENERGY réels empêchent une agrégation sans double comptage."
            if len(selection) > 1 else "Aucun capteur ENERGY unique n'est disponible pour le site."
        )
        return None, raison
    return selection[0], None


def _alerter_ecarts_facture():
    resultats = {"notifiees": 0, "insuffisantes": 0}
    factures = ResultatMetrique.objects.filter(
        code_metrique="consommation_facture_periodique",
        statut_qualite=ResultatMetrique.StatutQualite.FIABLE,
        valeur__isnull=False,
        compteur__isnull=False,
        periode_fin__gte=timezone.now() - timedelta(days=365),
    ).select_related(
        "organisation",
        "compteur__site",
        "fichier_source__import_donnees",
    )
    for facture in factures.iterator():
        comparaison = comparer_facture_telemetrie(facture)
        if comparaison.get("statut") not in ("mesure_reelle", "scenario_simule"):
            resultats["insuffisantes"] += 1
            continue
        ecart = comparaison["ecart_pourcentage"]
        if ecart is None or ecart < SEUIL_ECART_FACTURE_POURCENT:
            continue

        est_simule = comparaison["provenance"] == Capteur.Mode.SIMULATED
        reference = f"{facture.pk}:{comparaison['capteur_id']}:{comparaison['provenance']}"
        statut = _notifier(
            facture.organisation,
            "ECART_FACTURE_TELEMETRIE",
            reference,
            "Écart entre facture et monitoring",
            (
                f"La facture indique {comparaison['facture_kwh']:.2f} kWh; "
                f"le monitoring indique {comparaison['telemetrie_kwh']:.2f} kWh "
                f"({comparaison['ecart_pourcentage']:.1f}% d'écart). "
                + (
                    "Cette comparaison est un scénario simulé, pas une mesure réelle."
                    if est_simule else
                    "La comparaison repose sur un capteur déclaré réel et une couverture temporelle suffisante; "
                    "elle ne certifie pas l'authenticité de la facture."
                )
            ),
            "SCENARIO" if est_simule else "AVERTISSEMENT",
            comparaison,
        )
        resultats["notifiees"] += statut == "notifiee"
    return resultats


def _alerter_objectifs():
    resultats = {"notifiees": 0, "insuffisantes": 0}
    maintenant = timezone.now()
    for objectif in Objectif.objects.filter(statut=Objectif.Statut.ACTIF).select_related("organisation"):
        cible = Decimal(objectif.valeur_cible)
        if cible <= 0 or objectif.date_fin <= objectif.date_debut:
            resultats["insuffisantes"] += 1
            continue

        progression = calculer_progression_mesuree(objectif)
        mesuree = progression.get("mesuree")
        fiabilite = str(progression.get("fiabilite") or "").lower()
        if mesuree is None or fiabilite in ("", "faible", "initiale"):
            resultats["insuffisantes"] += 1
            continue

        valeur_mesuree = Decimal(str(mesuree))
        elapsed = max(
            Decimal("0"),
            min(
                Decimal("1"),
                Decimal(str((min(maintenant, objectif.date_fin) - objectif.date_debut).total_seconds()))
                / Decimal(str((objectif.date_fin - objectif.date_debut).total_seconds())),
            ),
        )
        expected = cible * elapsed
        if valeur_mesuree >= cible:
            state = "objectif-atteint"
            titre = f"Objectif atteint : {objectif.nom}"
            severite = "SUCCES"
            message = (
                f"La progression mesurée de l'objectif « {objectif.nom} » a atteint "
                f"{valeur_mesuree} {objectif.unite}, pour une cible de {cible} {objectif.unite}."
            )
        elif elapsed < Decimal("0.30") or valeur_mesuree >= expected - cible * Decimal("0.15"):
            continue
        else:
            retard_pct = int(max(0, (expected - valeur_mesuree) / cible * 100) // 10 * 10)
            phase = int(elapsed * 10)
            state = f"retard-{phase}-{retard_pct}"
            titre = f"Progression à surveiller : {objectif.nom}"
            severite = "AVERTISSEMENT"
            message = (
                f"À {elapsed * 100:.0f}% de la durée, la progression mesurée de l'objectif "
                f"« {objectif.nom} » est de {valeur_mesuree} {objectif.unite}, contre "
                f"{expected:.2f} {objectif.unite} attendus selon une trajectoire linéaire. "
                "La trajectoire linéaire est un repère de suivi, pas une prédiction."
            )
        source = str(progression.get("source") or "inconnue")
        simulation = "simulat" in fiabilite or "simulat" in source
        if simulation:
            message += " Les données sont simulées : ceci est une alerte de scénario."
            severite = "SCENARIO"

        statut = _notifier(
            objectif.organisation,
            "PROGRESSION_OBJECTIF",
            f"{objectif.pk}:{state}:{source}:{'scenario' if simulation else 'declare'}",
            titre,
            message,
            severite,
            {
                "objectif_id": str(objectif.pk),
                "mesuree": str(valeur_mesuree),
                "cible": str(cible),
                "unite": objectif.unite,
                "progression_attendue": str(expected),
                "source": source,
                "fiabilite": progression.get("fiabilite"),
                "detail": progression.get("detail"),
            },
        )
        resultats["notifiees"] += statut == "notifiee"
    return resultats


def executer_alertes_energetiques():
    """Évalue les alertes Woyofal, les factures/mesures et les objectifs actifs."""
    aujourd_hui = timezone.localdate()
    return {
        "woyofal": _alerter_woyofal(aujourd_hui),
        "tranches": _alerter_tranches(aujourd_hui),
        "factures": _alerter_ecarts_facture(),
        "objectifs": _alerter_objectifs(),
    }
