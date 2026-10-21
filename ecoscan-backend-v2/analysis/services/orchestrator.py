"""
Orchestrateur de l'analyse — relie les services pour une organisation et un jour.

Génère un diagnostic et une recommandation PROPOSÉE après détection. L'adoption
de la recommandation et la création d'une action restent des décisions humaines.

Deux voies de mesure selon le type de compteur :
- compteur à index relevé : variation vs baseline « même jour de la semaine » ;
- compteur prépayé Woyofal (des relevés de solde existent) : consommation déduite
  des soldes et recharges, variation vs moyenne des 7 jours précédents.

Déclenchement : commande `analyser_compteurs` (planifiée) et, pour Woyofal,
automatiquement après chaque nouveau relevé de solde (en arrière-plan).
"""

import logging
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from analysis.models import Anomalie, ResultatMetrique
from analysis.services import woyofal
from analysis.services.anomaly import detecter_anomalie
from analysis.services.context import obtenir_contexte
from analysis.services.hypothesis import generer_hypothese
from analysis.services.auto_recommandation import generer_recommandation_auto
from .metrics_service import MetricsService

logger = logging.getLogger(__name__)


def traiter_suivi_anomalie(anomalie_id):
    """Crée le diagnostic et la piste d'action idempotents après une détection."""
    anomalie = (
        Anomalie.objects.select_related("organisation", "resultat_metrique")
        .filter(id=anomalie_id)
        .first()
    )
    if anomalie is None:
        logger.warning("Suivi automatique ignoré : anomalie %s introuvable.", anomalie_id)
        return None, None

    hypothese = (
        anomalie.hypotheses.order_by("-date_creation").first()
        or generer_hypothese(anomalie, obtenir_contexte(anomalie))
    )
    recommandation = generer_recommandation_auto(anomalie)
    return hypothese, recommandation


def planifier_suivi_anomalie(anomalie):
    """Planifie le suivi seulement si le diagnostic ou la recommandation manque."""
    if anomalie.statut == Anomalie.Statut.RESOLVED:
        return False
    if anomalie.hypotheses.exists() and anomalie.recommandations.exists():
        return False

    from analysis.services.taches import lancer_en_arriere_plan

    lancer_en_arriere_plan(traiter_suivi_anomalie, str(anomalie.id))
    return True


def _en_datetime(date_jour) -> datetime:
    if isinstance(date_jour, datetime):
        return date_jour if timezone.is_aware(date_jour) else timezone.make_aware(date_jour)
    if isinstance(date_jour, date):
        return timezone.make_aware(datetime.combine(date_jour, time.min))
    raise TypeError("date_jour doit être une date ou un datetime.")


def _conclure(resultat_persiste, variation: dict) -> dict:
    """Anomalie -> contexte -> hypothèse, à partir d'un résultat de variation persisté."""
    anomalie = detecter_anomalie(resultat_persiste)
    if anomalie is None:
        return {"statut": "normal", "variation": variation, "anomalie": None, "hypothese": None}

    hypothese, recommandation = traiter_suivi_anomalie(anomalie.id)
    return {
        "statut": "anomalie_detectee",
        "variation": variation,
        "anomalie": {
            "id": str(anomalie.id),
            "type": anomalie.type,
            "severite": anomalie.severite,
            "ecart_pourcentage": float(anomalie.ecart_pourcentage),
        },
        # None si l'IA est indisponible : l'anomalie reste détectée et sera réessayée.
        "hypothese": (
            {"id": str(hypothese.id), "texte": hypothese.texte, "statut": hypothese.statut,
             "requires_human_confirmation": True}
            if hypothese is not None else None
        ),
        "recommandation": (
            {
                "id": str(recommandation.id),
                "statut": recommandation.statut,
                "requires_human_decision": True,
            }
            if recommandation is not None else None
        ),
        "next_step": (
            "review_recommendation"
            if recommandation is not None
            else "retry_diagnostic_and_recommendation"
        ),
    }


def _analyser_woyofal(organisation, compteur, date_jour: datetime) -> dict:
    resultat = woyofal.publier_variation(organisation, compteur, date_jour)
    if resultat is None:
        return {"statut": "donnees_insuffisantes", "anomalie": None, "hypothese": None,
                "detail": "Relevés de solde insuffisants (couverture du jour < 80 % ou moins de 4 jours de référence)."}
    variation = {"value": float(resultat.valeur), "unit": resultat.unite, "metric": resultat.code_metrique}
    return _conclure(resultat, variation)


def analyser_compteur(organisation, compteur, date_jour) -> dict:
    """Variation -> anomalie -> contexte -> hypothèse. S'arrête là."""
    date_jour = _en_datetime(date_jour)

    if woyofal.est_compteur_woyofal(compteur):
        return _analyser_woyofal(organisation, compteur, date_jour)

    variation = MetricsService(organisation).calculer_variation_vs_baseline(compteur, date_jour)
    if variation.get("value") is None:
        return {"statut": "donnees_insuffisantes", "detail": variation, "anomalie": None, "hypothese": None}

    debut_jour = date_jour.replace(hour=0, minute=0, second=0, microsecond=0)
    resultat_persiste = (
        ResultatMetrique.objects.filter(
            organisation=organisation, compteur=compteur, code_metrique="variation_vs_baseline",
            periode_debut=debut_jour, periode_fin=debut_jour + timedelta(days=1),
        )
        .order_by("-date_calcul")
        .first()
    )
    if resultat_persiste is None:
        return {"statut": "erreur", "detail": "Résultat de variation calculé mais introuvable en base.",
                "anomalie": None, "hypothese": None}
    return _conclure(resultat_persiste, variation)


def analyser_compteur_apres_saisie(compteur_id) -> None:
    """Tâche d'arrière-plan lancée après un nouveau relevé de solde : analyse
    hier (jour complet) puis aujourd'hui (souvent partiel : « insuffisant »)."""
    from organizations.models import Compteur

    compteur = Compteur.objects.select_related("site__organisation").filter(id=compteur_id).first()
    if compteur is None:
        return
    organisation = compteur.site.organisation
    maintenant = timezone.now()
    for decalage in (1, 0):
        try:
            analyser_compteur(organisation, compteur, maintenant - timedelta(days=decalage))
        except Exception:
            logger.exception("Analyse impossible pour %s (J-%s).", compteur, decalage)


def regenerer_hypotheses_manquantes(organisation) -> int:
    """Génère les hypothèses des anomalies qui n'en ont pas (service IA tombé
    lors de la détection). Retourne le nombre d'hypothèses créées."""
    anomalies = Anomalie.objects.filter(
        organisation=organisation, hypotheses__isnull=True,
        statut__in=(
            Anomalie.Statut.DETECTED,
            Anomalie.Statut.NEEDS_CONTEXT,
            Anomalie.Statut.ACTION_CREATED,
        ),
    )
    crees = 0
    for anomalie in anomalies:
        try:
            if generer_hypothese(anomalie, obtenir_contexte(anomalie)) is not None:
                crees += 1
        except Exception:
            logger.exception("Hypothèse impossible pour l'anomalie %s.", anomalie.id)
    return crees
