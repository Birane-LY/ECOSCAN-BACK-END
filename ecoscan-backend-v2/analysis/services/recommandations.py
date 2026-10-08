"""
Du diagnostic à l'action, puis au résultat mesuré — la boucle qui manquait :

    anomalie -> hypothèse CONFIRMÉE (humain) -> Recommandation (humain)
             -> Action(s) -> impact réel mesuré (humain) -> mémoire stratégique

Principe inchangé : aucun chiffre n'est inventé par l'IA. L'économie estimée d'une
recommandation, l'impact réellement mesuré d'une action sont SAISIS par une
personne ; ce module valide, relie et propage vers la mémoire (et le RAG).
"""

import logging
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from analysis.models import Action, Anomalie, Hypothese, MemoireStrategique, Recommandation
from analysis.services.memory_service import planifier_reindexation

logger = logging.getLogger(__name__)

UNITES_FCFA = {"FCFA", "XOF", "F CFA", "CFA"}
# Points de départ, pas des vérités : au-delà de 80 % de l'impact attendu, le
# diagnostic est « confirmé » ; entre 40 et 80 % « partiellement » ; en dessous il
# reste « à vérifier ». À ajuster avec l'usage.
SEUIL_CONFIRMEE = Decimal("80")
SEUIL_PARTIELLE = Decimal("40")
PLAFOND = Decimal("1000000000000")  # 10^12 : reste sous max_digits=18


class DonneesInvalidesError(ValueError):
    """Saisie invalide (réponse HTTP 400)."""


class EtatIncompatibleError(ValueError):
    """Opération impossible dans l'état actuel (réponse HTTP 409)."""


def _est_fcfa(unite: str) -> bool:
    return (unite or "").strip().upper() in UNITES_FCFA


def _decimal(valeur, champ: str) -> Decimal:
    if valeur is None or str(valeur).strip() == "":
        raise DonneesInvalidesError(f"{champ} est requis.")
    try:
        d = Decimal(str(valeur).strip().replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise DonneesInvalidesError(f"{champ} doit être un nombre.")
    if not d.is_finite() or d < 0 or d >= PLAFOND:
        raise DonneesInvalidesError(f"{champ} doit être un nombre positif raisonnable.")
    return d


def _objectif_de(organisation, objectif_id):
    from energy.models import Objectif

    if not objectif_id:
        raise DonneesInvalidesError("objectif est requis : à quel objectif cette recommandation contribue-t-elle ?")
    try:
        objectif = Objectif.objects.filter(id=objectif_id, organisation=organisation).first()
    except (ValueError, TypeError, DjangoValidationError):
        objectif = None
    if objectif is None:
        raise DonneesInvalidesError("Objectif introuvable dans votre organisation.")
    return objectif


def _echeance(valeur):
    if not valeur:
        return None
    texte = str(valeur)
    dt = parse_datetime(texte)
    if dt is None:
        d = parse_date(texte)
        if d is None:
            raise DonneesInvalidesError("date_echeance invalide (format AAAA-MM-JJ attendu).")
        dt = datetime.combine(d, time.min)
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _memoire_de(anomalie_id):
    return MemoireStrategique.objects.filter(anomalie_id=anomalie_id).first() if anomalie_id else None


def synchroniser_memoire_recommandations(anomalie_id) -> None:
    """Répercute les recommandations de l'anomalie dans sa mémoire : « action menée »
    et impact attendu (somme des économies exprimées en FCFA)."""
    memoire = _memoire_de(anomalie_id)
    if memoire is None:
        return
    recos = list(Recommandation.objects.filter(anomalie_id=anomalie_id).order_by("titre"))
    if not recos:
        return
    memoire.action_texte = "\n".join(f"- {r.titre} : {r.description}" for r in recos)
    memoire.impact_attendu_fcfa = sum((r.economie_estimee for r in recos if _est_fcfa(r.unite)), Decimal("0")) or None
    memoire.save(update_fields=("action_texte", "impact_attendu_fcfa"))
    planifier_reindexation(memoire)


def creer_recommandation_depuis_anomalie(anomalie: Anomalie, donnees) -> Recommandation:
    """Geste humain : crée une recommandation PROPOSEE à partir d'une anomalie dont
    l'hypothèse a été confirmée."""
    if not anomalie.hypotheses.filter(statut=Hypothese.Statut.CONFIRMEE).exists():
        raise EtatIncompatibleError(
            "Confirmez d'abord une hypothèse pour cette anomalie : une recommandation "
            "ne se crée qu'à partir d'un diagnostic validé par une personne."
        )

    objectif = _objectif_de(anomalie.organisation, donnees.get("objectif"))

    titre = str(donnees.get("titre") or "").strip()
    description = str(donnees.get("description") or "").strip()
    unite = str(donnees.get("unite") or "").strip()
    if not titre or len(titre) > 180:
        raise DonneesInvalidesError("titre est requis (180 caractères maximum).")
    if not description:
        raise DonneesInvalidesError("description est requise.")
    if not unite or len(unite) > 30:
        raise DonneesInvalidesError("unite est requise (ex. kWh ou FCFA).")

    economie = _decimal(donnees.get("economie_estimee"), "economie_estimee")
    impact_brut = donnees.get("impact_estime")
    impact = economie if impact_brut in (None, "") else _decimal(impact_brut, "impact_estime")

    priorite = donnees.get("priorite") or Recommandation.Priorite.MOYENNE
    if priorite not in Recommandation.Priorite.values:
        raise DonneesInvalidesError(f"priorite doit être l'une de : {', '.join(Recommandation.Priorite.values)}.")

    reco = Recommandation.objects.create(
        objectif=objectif, anomalie=anomalie, titre=titre, description=description,
        impact_estime=impact, economie_estimee=economie, unite=unite, priorite=priorite,
        date_echeance=_echeance(donnees.get("date_echeance")),
    )

    if anomalie.statut in (Anomalie.Statut.DETECTED, Anomalie.Statut.NEEDS_CONTEXT, Anomalie.Statut.CONFIRMED):
        anomalie.statut = Anomalie.Statut.ACTION_CREATED
        anomalie.save(update_fields=("statut",))

    synchroniser_memoire_recommandations(anomalie.id)
    return reco


def lier_action_a_memoire(action: Action) -> None:
    """La mémoire pointe vers la première action menée pour son anomalie."""
    memoire = _memoire_de(action.recommandation.anomalie_id)
    if memoire is not None and memoire.action_id is None:
        memoire.action = action
        memoire.save(update_fields=("action",))


def _statut_memoire(taux: Decimal) -> str:
    if taux >= SEUIL_CONFIRMEE:
        return MemoireStrategique.Statut.CONFIRMEE
    if taux >= SEUIL_PARTIELLE:
        return MemoireStrategique.Statut.PARTIELLEMENT_CONFIRMEE
    return MemoireStrategique.Statut.A_VERIFIER


def mesurer_impact_action(action: Action, economie_realisee_fcfa) -> Action:
    """Enregistre l'impact RÉEL d'une action terminée (saisi par une personne) et
    le propage à la mémoire stratégique : impact mesuré, taux de réalisation,
    statut de la mémoire."""
    if action.statut != Action.Statut.TERMINEE:
        raise EtatIncompatibleError("L'impact ne se mesure qu'après la clôture de l'action.")

    realisee = _decimal(economie_realisee_fcfa, "economie_realisee_fcfa")
    reco = action.recommandation
    memoire = _memoire_de(reco.anomalie_id)

    attendu = None
    if memoire is not None and memoire.impact_attendu_fcfa:
        attendu = memoire.impact_attendu_fcfa
    elif _est_fcfa(reco.unite) and reco.economie_estimee:
        attendu = reco.economie_estimee

    taux = None
    if attendu and attendu > 0:
        taux = min((realisee / attendu * 100).quantize(Decimal("0.01")), Decimal("9999.99"))

    action.economie_realisee_fcfa = realisee
    action.taux_realisation_impact = taux
    action.date_mesure_impact = timezone.now()
    action.save(update_fields=("economie_realisee_fcfa", "taux_realisation_impact", "date_mesure_impact"))

    if memoire is not None:
        total = (
            Action.objects.filter(recommandation__anomalie_id=reco.anomalie_id, economie_realisee_fcfa__isnull=False)
            .aggregate(t=Sum("economie_realisee_fcfa"))["t"]
        ) or realisee
        memoire.impact_mesure_fcfa = total
        champs = ["impact_mesure_fcfa"]
        if memoire.impact_attendu_fcfa and memoire.impact_attendu_fcfa > 0:
            taux_global = min((total / memoire.impact_attendu_fcfa * 100).quantize(Decimal("0.01")), Decimal("9999.99"))
            memoire.taux_realisation = taux_global
            memoire.statut = _statut_memoire(taux_global)
            champs += ["taux_realisation", "statut"]
        if memoire.action_id is None:
            memoire.action = action
            champs.append("action")
        memoire.save(update_fields=champs)
        planifier_reindexation(memoire)
    return action
