"""Détection idempotente des consommations au-dessus de la veille en zone vide."""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from analysis.models import Anomalie, Recommandation, ResultatMetrique
from energy.models import Objectif

from .models import Capteur, CapteurPresence, Equipement, MesureCapteur
from .occupancy import delai_fraicheur_secondes
from .services import convertir_mesure


PREFIX_METRIQUE_OCCUPATION_ZONE = "energy_use_unoccupied_zone_"
SEUIL_SURVEILLANCE = Decimal("10")
ECART_MINIMUM_KWH = Decimal("0.01")
ECART_MAXIMUM_RELEVES_SECONDES = 900
DUREE_MINIMUM_ANALYSE_SECONDES = Decimal("3600")


def code_metrique_occupation_zone(zone):
    return f"{PREFIX_METRIQUE_OCCUPATION_ZONE}{zone.pk.hex}"


def _periode_journee(jour):
    debut = timezone.make_aware(datetime.combine(jour, time.min))
    return debut, timezone.make_aware(
        datetime.combine(jour + timedelta(days=1), time.min)
    )


def _occupation_confirmee_inoccupee(capteurs, releves, debut, fin):
    for capteur in capteurs:
        mesures = releves.get(capteur.id, ())
        precedent = next(
            (mesure for mesure in reversed(mesures) if mesure.date_mesure <= debut),
            None,
        )
        if (
            precedent is None
            or precedent.occupee
            or (debut - precedent.date_mesure).total_seconds()
            > delai_fraicheur_secondes(capteur.frequence_secondes)
        ):
            return False
        if any(
            mesure.occupee
            for mesure in mesures
            if debut < mesure.date_mesure <= fin
        ):
            return False
        derniere = next(
            (mesure for mesure in reversed(mesures) if mesure.date_mesure <= fin),
            precedent,
        )
        if (
            fin - derniere.date_mesure
        ).total_seconds() > delai_fraicheur_secondes(capteur.frequence_secondes):
            return False
    return bool(capteurs)


def _puissance_kw(mesure, capteur):
    conversion = convertir_mesure(capteur.type, mesure.valeur, mesure.unite)
    return conversion[1] if conversion else None


def _integrer_equipement(equipement, presence_capteurs, presence_releves, debut, fin):
    capteurs = list(
        Capteur.objects.filter(
            equipement=equipement,
            statut=Capteur.Statut.ACTIVE,
            type__iexact="POWER",
        )
    )
    choix = []
    for capteur in capteurs:
        mesures = list(
            MesureCapteur.objects.filter(
                capteur=capteur,
                date_mesure__gte=debut - timedelta(seconds=capteur.frequence_secondes),
                date_mesure__lt=fin,
            ).order_by("date_mesure", "date_reception")
        )
        if len(mesures) >= 2:
            choix.append((len(mesures), capteur.id, capteur, mesures))
    if not choix:
        return None

    _, _, capteur, mesures = max(choix, key=lambda item: (item[0], str(item[1])))
    puissance_mesures = []
    for mesure in mesures:
        try:
            puissance = _puissance_kw(mesure, capteur)
        except ValueError:
            return None
        if puissance is None:
            return None
        puissance_mesures.append((mesure.date_mesure, puissance))

    energie_observee = Decimal("0")
    energie_attendue = Decimal("0")
    intervalles_valides = []
    intervalle_max = min(
        max(capteur.frequence_secondes * 3, 60),
        ECART_MAXIMUM_RELEVES_SECONDES,
    )
    for (debut_mesure, puissance_debut), (fin_mesure, puissance_fin) in zip(
        puissance_mesures,
        puissance_mesures[1:],
    ):
        duree_secondes = Decimal(
            str((fin_mesure - debut_mesure).total_seconds())
        )
        if (
            debut_mesure < debut
            or fin_mesure > fin
            or duree_secondes <= 0
            or duree_secondes > intervalle_max
            or not _occupation_confirmee_inoccupee(
                presence_capteurs,
                presence_releves,
                debut_mesure,
                fin_mesure,
            )
        ):
            continue
        heures = duree_secondes / Decimal("3600")
        puissance_moyenne = (puissance_debut + puissance_fin) / Decimal("2")
        veille_kw = equipement.puissance_veille_kw * equipement.quantite
        energie_observee += puissance_moyenne * heures
        energie_attendue += veille_kw * heures
        intervalles_valides.append((debut_mesure, fin_mesure))

    if not intervalles_valides:
        return None
    return energie_observee, energie_attendue, intervalles_valides, capteur


def _duree_couverte_secondes(intervalles):
    fusionnes = []
    for debut, fin in sorted(intervalles):
        if fusionnes and debut <= fusionnes[-1][1]:
            fusionnes[-1] = (fusionnes[-1][0], max(fusionnes[-1][1], fin))
        else:
            fusionnes.append((debut, fin))
    return sum(
        (
            Decimal(str((fin - debut).total_seconds()))
            for debut, fin in fusionnes
        ),
        Decimal("0"),
    )


def _severite(ecart):
    if ecart < Decimal("20"):
        return Anomalie.Severite.SURVEILLANCE
    if ecart < Decimal("40"):
        return Anomalie.Severite.ALERTE
    return Anomalie.Severite.INVESTIGATION_PRIORITAIRE


def _objectif_kwh(organisation, valeur):
    objectif = Objectif.objects.filter(
        organisation=organisation,
        statut=Objectif.Statut.ACTIF,
        unite__iexact="kWh",
    ).order_by("date_debut").first()
    if objectif:
        return objectif
    debut = timezone.now()
    return Objectif.objects.create(
        organisation=organisation,
        nom="Réduire les consommations hors occupation",
        description=(
            "Objectif indicatif lié aux consommations mesurées au-dessus de la "
            "puissance de veille en zones récemment confirmées inoccupées."
        ),
        type="reduction_energie",
        valeur_cible=max(valeur, Decimal("0.001")),
        unite="kWh",
        date_debut=debut,
        date_fin=debut + timedelta(days=365),
        statut=Objectif.Statut.ACTIF,
    )


def _mettre_a_jour_anomalie(resultat, ecart, observe, attendu):
    anomalie = Anomalie.objects.filter(resultat_metrique=resultat).first()
    if ecart < SEUIL_SURVEILLANCE or observe - attendu < ECART_MINIMUM_KWH:
        if anomalie and anomalie.statut in (
            Anomalie.Statut.DETECTED,
            Anomalie.Statut.ACTION_CREATED,
        ):
            anomalie.statut = Anomalie.Statut.RESOLVED
            anomalie.save(update_fields=("statut",))
        return anomalie

    valeurs = {
        "type": "ENERGY_USE_IN_UNOCCUPIED_ZONE",
        "severite": _severite(ecart),
        "valeur_observee": observe,
        "valeur_attendue": attendu,
        "ecart_pourcentage": ecart,
    }
    if anomalie is None:
        anomalie = Anomalie.objects.create(
            organisation=resultat.organisation,
            resultat_metrique=resultat,
            statut=Anomalie.Statut.DETECTED,
            **valeurs,
        )
    else:
        for field, value in valeurs.items():
            setattr(anomalie, field, value)
        if anomalie.statut == Anomalie.Statut.RESOLVED:
            anomalie.statut = Anomalie.Statut.DETECTED
        anomalie.save(update_fields=(*valeurs, "statut"))

    recommendation = anomalie.recommandations.first()
    if recommendation is None:
        economie = max(observe - attendu, Decimal("0"))
        modes = {source.get("mode") for source in resultat.sources}
        source = (
            "simulées"
            if modes == {Capteur.Mode.SIMULATED}
            else "réelles"
            if modes == {Capteur.Mode.REAL}
            else "mixtes"
        )
        Recommandation.objects.create(
            objectif=_objectif_kwh(resultat.organisation, economie),
            anomalie=anomalie,
            titre="Examiner la consommation en zone inoccupée",
            description=(
                "La puissance intégrée dépasse la veille nominale pendant des "
                "intervalles où tous les capteurs de présence actifs ont confirmé "
                f"une absence récente. Sources de puissance {source}. Vérifier les mesures et les besoins métier "
                "avant toute action ; l'estimation n'est pas une économie garantie."
            ),
            impact_estime=economie,
            economie_estimee=economie,
            unite="kWh",
            priorite=(
                Recommandation.Priorite.CRITIQUE
                if anomalie.severite == Anomalie.Severite.INVESTIGATION_PRIORITAIRE
                else Recommandation.Priorite.HAUTE
            ),
            statut=Recommandation.Statut.PROPOSEE,
        )
    return anomalie


@transaction.atomic
def analyser_occupation_site(site, jour=None):
    """Persiste les anomalies de surconsommation uniquement sur données complètes."""
    with timezone.override(site.fuseau_horaire):
        maintenant = timezone.now()
        jour = jour or (timezone.localdate(maintenant) - timedelta(days=1))
        debut, fin = _periode_journee(jour)
        if fin > timezone.localtime(maintenant):
            return {"status": "insufficient_data", "zones_analyzed": 0}

        zones_analysees = 0
        anomalies_detectees = 0
        for zone in site.zones.filter(active=True).order_by("id"):
            presence_capteurs = list(
                CapteurPresence.objects.filter(
                    zone=zone,
                    statut=CapteurPresence.Statut.ACTIVE,
                )
            )
            if not presence_capteurs:
                continue
            presence_releves = {
                capteur.id: list(
                    capteur.mesures.filter(
                        date_mesure__gte=debut
                        - timedelta(
                            seconds=delai_fraicheur_secondes(
                                capteur.frequence_secondes
                            )
                        ),
                        date_mesure__lt=fin,
                    ).order_by("date_mesure", "date_reception")
                )
                for capteur in presence_capteurs
            }
            observe = Decimal("0")
            attendu = Decimal("0")
            intervalles_valides = []
            sources = []
            for equipement in Equipement.objects.filter(
                site=site,
                zone=zone,
                monitoring_active=True,
            ).order_by("id"):
                resultat = _integrer_equipement(
                    equipement,
                    presence_capteurs,
                    presence_releves,
                    debut,
                    fin,
                )
                if resultat is None:
                    continue
                observe += resultat[0]
                attendu += resultat[1]
                intervalles_valides.extend(resultat[2])
                sources.append(
                    {
                        "sensor_id": str(resultat[3].id),
                        "equipment_id": str(equipement.id),
                        "mode": resultat[3].mode,
                    }
                )
            duree_valide = _duree_couverte_secondes(intervalles_valides)
            if duree_valide < DUREE_MINIMUM_ANALYSE_SECONDES:
                continue

            ecart = (
                Decimal("0")
                if attendu == 0 and observe == 0
                else Decimal("100")
                if attendu == 0
                else max(observe - attendu, Decimal("0"))
                / attendu
                * Decimal("100")
            ).quantize(Decimal("0.01"))
            completude = min(duree_valide / Decimal("86400"), Decimal("1"))
            resultat, _ = ResultatMetrique.objects.update_or_create(
                organisation=site.organisation,
                compteur=None,
                code_metrique=code_metrique_occupation_zone(zone),
                version_metrique="1.0",
                periode_debut=debut,
                periode_fin=fin,
                defaults={
                    "valeur": ecart,
                    "unite": "%",
                    "baseline_type": "puissance_de_veille",
                    "baseline_valeur": attendu,
                    "baseline_nombre_observations": len(sources),
                    "completude": completude.quantize(Decimal("0.0001")),
                    "statut_qualite": ResultatMetrique.StatutQualite.ESTIME,
                    "confiance": completude.quantize(Decimal("0.0001")),
                    "sources": sources,
                    "limites": [
                        "Intégration trapézoïdale des relevés POWER entre deux mesures.",
                        "Les périodes avec présence absente, périmée ou occupée sont exclues.",
                        "L'énergie évitable est estimée au-dessus de la veille nominale.",
                    ],
                },
            )
            anomalie = _mettre_a_jour_anomalie(resultat, ecart, observe, attendu)
            if anomalie and anomalie.statut != Anomalie.Statut.RESOLVED:
                from analysis.services.orchestrator import planifier_suivi_anomalie

                planifier_suivi_anomalie(anomalie)
            zones_analysees += 1
            anomalies_detectees += bool(
                anomalie and anomalie.statut != Anomalie.Statut.RESOLVED
            )
        return {
            "status": "complete" if zones_analysees else "insufficient_data",
            "zones_analyzed": zones_analysees,
            "anomalies_detected": anomalies_detectees,
        }
