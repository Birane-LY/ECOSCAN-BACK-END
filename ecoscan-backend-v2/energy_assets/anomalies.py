"""Détection déterministe des écarts de consommation des sites."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import reduce
from operator import or_

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from analysis.models import Anomalie, ResultatMetrique
from analysis.services.anomaly import (
    PREFIXE_METRIQUE_SITE_TELEMETRIE,
    detecter_anomalie,
)

from .models import Capteur, MesureCapteur
from .services import convertir_mesure


JOURS_BASELINE = 4
MINIMUM_RELEVES_JOUR = 4


def code_metrique_site(site):
    """Construit un code de métrique stable et unique pour le site."""
    return f"{PREFIXE_METRIQUE_SITE_TELEMETRIE}{site.pk.hex}"


def _debut_journee(jour):
    """Retourne le début d'une journée dans le fuseau courant."""
    return timezone.make_aware(datetime.combine(jour, time.min))


def _jours_reference(jour):
    """Retourne les quatre journées précédentes correspondant au même jour."""
    return tuple(jour - timedelta(days=7 * semaine) for semaine in range(1, 5))


def _construire_jours_mesures(capteurs, jours, fuseau_horaire):
    """Calcule les deltas journaliers pour les capteurs ayant assez de relevés."""
    if not capteurs:
        return {}, 0

    periodes = {
        jour: (_debut_journee(jour), _debut_journee(jour + timedelta(days=1)))
        for jour in jours
    }
    filtre_periodes = reduce(
        or_,
        (
            Q(date_mesure__gte=debut, date_mesure__lt=fin)
            for debut, fin in periodes.values()
        ),
    )
    totaux = {jour: Decimal("0") for jour in jours}
    capteurs_complets = {jour: 0 for jour in jours}

    with timezone.override(fuseau_horaire):
        for capteur in capteurs:
            mesures_par_jour = {jour: [] for jour in jours}
            mesures = (
                MesureCapteur.objects.filter(
                    capteur=capteur,
                )
                .filter(filtre_periodes)
                .order_by("date_mesure", "date_reception")
            )
            for mesure in mesures.iterator():
                jour_mesure = timezone.localtime(mesure.date_mesure).date()
                if jour_mesure in mesures_par_jour:
                    mesures_par_jour[jour_mesure].append(mesure)

            for jour, releves in mesures_par_jour.items():
                if len(releves) < MINIMUM_RELEVES_JOUR:
                    continue
                indices = [
                    convertir_mesure(
                        capteur.type,
                        releve.valeur,
                        releve.unite,
                    )[1]
                    for releve in releves
                ]
                if any(
                    actuel < precedent
                    for precedent, actuel in zip(indices, indices[1:])
                ):
                    continue
                consommation = indices[-1] - indices[0]
                totaux[jour] += consommation
                capteurs_complets[jour] += 1

    nombres_capteurs = len(capteurs)
    jours_complets = {
        jour: valeur
        for jour, valeur in totaux.items()
        if capteurs_complets[jour] == nombres_capteurs
    }
    return jours_complets, nombres_capteurs


@transaction.atomic
def analyser_anomalies_site(site, jour=None, maintenant=None):
    """Compare la consommation journalière du site à quatre semaines de référence."""
    maintenant = maintenant or timezone.now()
    with timezone.override(site.fuseau_horaire):
        if jour is None:
            jour = timezone.localdate(maintenant) - timedelta(days=1)
        elif isinstance(jour, datetime):
            jour = timezone.localtime(jour).date()
        elif not isinstance(jour, date):
            raise TypeError("jour doit être une date ou un datetime.")

        debut = _debut_journee(jour)
        fin = _debut_journee(jour + timedelta(days=1))
        if fin > timezone.localtime(maintenant):
            return {"status": "insufficient_data", "anomaly": None}

        capteurs = list(
            Capteur.objects.filter(
                equipement__site=site,
                equipement__monitoring_active=True,
                statut=Capteur.Statut.ACTIVE,
                type__iexact="ENERGY",
            ).order_by("id")
        )
        jours_reference = _jours_reference(jour)
        mesures_journalieres, nombre_capteurs = _construire_jours_mesures(
            capteurs,
            (*jours_reference, jour),
            timezone.get_current_timezone(),
        )

    dates_manquantes = [
        date_reference
        for date_reference in (*jours_reference, jour)
        if date_reference not in mesures_journalieres
    ]
    if dates_manquantes or nombre_capteurs == 0:
        return {
            "status": "insufficient_data",
            "anomaly": None,
            "missing_days": dates_manquantes,
            "baseline_days": sum(
                date_reference in mesures_journalieres
                for date_reference in jours_reference
            ),
            "required_baseline_days": JOURS_BASELINE,
            "sensor_count": nombre_capteurs,
        }

    valeur_observee = mesures_journalieres[jour]
    valeur_attendue = sum(
        (mesures_journalieres[date_reference] for date_reference in jours_reference),
        Decimal("0"),
    ) / Decimal(JOURS_BASELINE)
    ecart_pourcentage = (
        Decimal("0")
        if valeur_attendue == 0 and valeur_observee == 0
        else Decimal("100")
        if valeur_attendue == 0
        else (valeur_observee - valeur_attendue)
        / valeur_attendue
        * Decimal("100")
    ).quantize(Decimal("0.01"))
    metric_defaults = {
        "valeur": ecart_pourcentage,
        "unite": "%",
        "baseline_type": "moyenne_jour_semaine",
        "baseline_valeur": valeur_attendue,
        "baseline_nombre_observations": JOURS_BASELINE,
        "completude": Decimal("1.0000"),
        "statut_qualite": ResultatMetrique.StatutQualite.FIABLE,
        "confiance": Decimal("1.0000"),
        "sources": [
            {
                "sensor_id": str(capteur.id),
                "equipment_id": str(capteur.equipement_id),
            }
            for capteur in capteurs
        ],
        "limites": [
            "Consommation calculée depuis le premier et le dernier relevé de la journée."
        ],
    }
    resultat_metrique, _ = ResultatMetrique.objects.update_or_create(
        organisation=site.organisation,
        compteur=None,
        code_metrique=code_metrique_site(site),
        version_metrique="1.0",
        periode_debut=debut,
        periode_fin=fin,
        defaults=metric_defaults,
    )
    anomalie = detecter_anomalie(resultat_metrique)

    if anomalie is None:
        anomalie = Anomalie.objects.filter(
            organisation=site.organisation,
            resultat_metrique=resultat_metrique,
        ).first()
        if anomalie and anomalie.statut == Anomalie.Statut.DETECTED:
            anomalie.statut = Anomalie.Statut.RESOLVED
            anomalie.save(update_fields=("statut",))
        return {
            "status": "normal",
            "anomaly": anomalie,
            "observed_kwh": valeur_observee,
            "expected_kwh": valeur_attendue,
            "variation_percent": ecart_pourcentage,
        }

    champs = {
        "valeur_observee": valeur_observee,
        "valeur_attendue": valeur_attendue,
    }
    for champ, valeur in champs.items():
        setattr(anomalie, champ, valeur)
    if anomalie.statut == Anomalie.Statut.RESOLVED:
        anomalie.statut = Anomalie.Statut.DETECTED
    anomalie.save(update_fields=(*champs, "statut"))
    return {
        "status": "anomaly_detected",
        "anomaly": anomalie,
        "observed_kwh": valeur_observee,
        "expected_kwh": valeur_attendue,
        "variation_percent": ecart_pourcentage,
    }


def analyser_anomalies_sites(jour=None, site_id=None, maintenant=None):
    """Analyse tous les sites suivis, ou un seul site si son identifiant est fourni."""
    from organizations.models import Site

    sites = Site.objects.filter(
        equipements__monitoring_active=True,
        equipements__capteurs__statut=Capteur.Statut.ACTIVE,
        equipements__capteurs__type__iexact="ENERGY",
    ).distinct().order_by("id")
    if site_id is not None:
        sites = sites.filter(pk=site_id)

    resultats = []
    for site in sites.iterator():
        resultats.append(
            analyser_anomalies_site(site, jour=jour, maintenant=maintenant)
        )
    return resultats
