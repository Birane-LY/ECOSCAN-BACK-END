"""
Génération d'hypothèses — l'IA intervient ICI, jamais avant. L'anomalie et son
contexte ont déjà été calculés de façon déterministe (anomaly_service,
context_service) ; ce module ne fait qu'interpréter ce qui est déjà établi.

Règle non négociable, reprise de plusieurs documents partagés dans ce projet :
l'IA ne doit JAMAIS présenter une hypothèse comme une certitude. Le prompt
l'impose explicitement, et le modèle Hypothese lui-même n'a pas de statut
"certaine" — seulement PROPOSEE / CONFIRMEE / REJETEE, où CONFIRMEE ne peut être
posé que par un humain (voir views.py), jamais par ce service.
"""

from analysis.api.ai_client import demander_hypothese
from analysis.models import Anomalie, Hypothese


def generer_hypothese(anomalie: Anomalie, observations: list) -> Hypothese:
    """Construit le prompt à partir de l'anomalie + son contexte validé, appelle
    le service IA, et enregistre le résultat comme Hypothese PROPOSEE (jamais
    CONFIRMEE automatiquement).

    Si le service IA est indisponible, retourne quand même une Hypothese — avec
    un texte qui le dit explicitement, plutôt que de faire échouer tout le flux
    d'analyse pour une panne d'un service tiers.
    """
    contexte_texte = (
        "\n".join(f"- {obs.date_observation:%Y-%m-%d %H:%M} : {obs.texte}" for obs in observations)
        if observations
        else "Aucune observation opérationnelle validée disponible pour cette période."
    )

    question = (
        f"Une anomalie de consommation énergétique a été détectée : "
        f"écart de {anomalie.ecart_pourcentage}% par rapport à la baseline "
        f"(sévérité : {anomalie.severite}). "
        f"Valeur observée : {anomalie.valeur_observee}, valeur attendue : {anomalie.valeur_attendue}.\n\n"
        f"Observations opérationnelles validées disponibles :\n{contexte_texte}\n\n"
        f"Formule UNIQUEMENT une hypothèse PROBABLE, jamais une cause certaine. "
        f"Si les observations disponibles ne permettent pas de formuler une hypothèse "
        f"raisonnable, dis-le explicitement plutôt que d'en inventer une."
    )

    reponse = demander_hypothese(question, anomalie.organisation_id)

    if "_error" in reponse:
        texte = f"Hypothèse indisponible : {reponse['_error']}"
        preuves = []
    else:
        texte = reponse.get("answer", "Aucune réponse du service IA.")
        preuves = [obs.texte for obs in observations]

    return Hypothese.objects.create(
        anomalie=anomalie,
        texte=texte,
        preuves=preuves,
        confiance=None,  # jamais déduite automatiquement d'un texte libre — un humain évalue
        statut=Hypothese.Statut.PROPOSEE,
        genere_par_ia=True,
    )