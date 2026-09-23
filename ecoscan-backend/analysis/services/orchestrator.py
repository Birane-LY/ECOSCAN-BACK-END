"""
Orchestrateur de l'analyse — relie les services entre eux pour une organisation
et une période données.

Ne va PAS jusqu'à créer automatiquement une Recommandation ou une Action . `analyser_compteur` s'arrête donc à
la génération d'hypothèses — la création de Recommandation reste un geste
humain, déclenché depuis l'API avec l'objectif à associer (voir
RecommandationViewSet), jamais depuis ce module.
"""

from datetime import timedelta

from analysis.services.anomaly import detecter_anomalie
from analysis.services.context import obtenir_contexte
from analysis.services.hypothesis import generer_hypothese
from .metrics_service import MetricsService


def analyser_compteur(organisation, compteur, date_jour) -> dict:
    """Exécute la chaîne automatisable complète pour un compteur et un jour
    donnés : calcul de la variation vs baseline -> détection d'anomalie ->
    contexte -> hypothèse. S'arrête là ; la suite est humaine.
    """
    service = MetricsService(organisation)
    variation = service.calculer_variation_vs_baseline(compteur, date_jour)

    if variation.get("value") is None:
        return {
            "statut": "donnees_insuffisantes",
            "detail": variation,
            "anomalie": None,
            "hypothese": None,
        }

    debut_jour = date_jour.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_jour = debut_jour + timedelta(days=1)

    from analysis.models import ResultatMetrique
    resultat_persiste = ResultatMetrique.objects.filter(
        organisation=organisation,
        compteur=compteur,
        code_metrique="variation_vs_baseline",
        periode_debut=debut_jour,
        periode_fin=fin_jour,
    ).order_by("-date_calcul").first()

    if resultat_persiste is None:
        return {"statut": "erreur", "detail": "Résultat de variation calculé mais introuvable en base."}

    anomalie = detecter_anomalie(resultat_persiste)
    if anomalie is None:
        return {"statut": "normal", "variation": variation, "anomalie": None, "hypothese": None}

    contexte = obtenir_contexte(anomalie)
    hypothese = generer_hypothese(anomalie, contexte)

    return {
        "statut": "anomalie_detectee",
        "variation": variation,
        "anomalie": {
            "id": str(anomalie.id),
            "type": anomalie.type,
            "severite": anomalie.severite,
            "ecart_pourcentage": float(anomalie.ecart_pourcentage),
        },
        "hypothese": {
            "id": str(hypothese.id),
            "texte": hypothese.texte,
            "statut": hypothese.statut,
            "requires_human_confirmation": True,
        },
        "next_step": "confirm_hypothesis",
    }