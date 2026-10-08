"""
Génération d'hypothèses d'audit énergétique contextuelles et concrètes pour PME.
Adapté aux spécificités tarifaires de la Senelec (tranches, saisonnalité, puissance).
"""

import logging
from typing import Optional

from analysis.models import Anomalie, Hypothese

logger = logging.getLogger(__name__)


def generer_hypothese(anomalie: Anomalie, observations: list) -> Optional[Hypothese]:
    """Génère une analyse technique contextualisée de l'anomalie pour toute PME."""
    from analysis.api.ai_client import demander_hypothese

    existante = Hypothese.objects.filter(anomalie=anomalie).order_by("-date_creation").first()
    if existante is not None:
        return existante

    sens = "hausse" if anomalie.type == "consumption_spike" else "baisse"
    try:
        pct = abs(float(anomalie.ecart_pourcentage))
    except (ValueError, TypeError):
        pct = 0.0

    # Contexte PME et notes terrain
    nom_pme = anomalie.organisation.nom
    secteur_pme = anomalie.organisation.secteur or "PME / Commerce / Activité tertiaire"
    
    obs_txt = "\n".join(f"- {o.texte}" for o in observations) if observations else "Aucun événement particulier signalé sur la période."

    compteur_ref = ""
    if anomalie.resultat_metrique and anomalie.resultat_metrique.compteur:
        compteur_ref = anomalie.resultat_metrique.compteur.reference

    val_obs = anomalie.valeur_observee or "Non précisée"
    val_att = anomalie.valeur_attendue or "Non précisée"

    question = (
        f"Tu es un ingénieur expert en audit énergétique au Sénégal, spécialisé dans l'optimisation des factures Senelec pour les entreprises.\n"
        f"Analyse l'anomalie de facturation constatée pour l'entreprise '{nom_pme}' (Secteur : {secteur_pme}) :\n"
        f"- Variation : {sens.upper()} anormale de {pct:.1f} % par rapport au cycle précédent\n"
        f"- Sévérité : {anomalie.severite}\n"
        f"- Volume consommé : {val_obs} kWh (attendue : {val_att} kWh)\n"
        f"- Compteur concerné : {compteur_ref or 'Compteur principal'}\n"
        f"- Événements terrain connus : {obs_txt}\n\n"
        f"Directives strictes pour ton diagnostic :\n"
        f"1. Ne dis JAMAIS 'aucune observation disponible' ni 'erreur de saisie probable'. Agis en vrai auditeur.\n"
        f"2. Explique l'impact financier Senelec : au Sénégal, une forte consommation fait basculer la majorité des kWh en Tranche 3 (> 210 FCFA/kWh hors taxe + 18% de TVA en tarif pro), ce qui démultiplie le montant net à payer.\n"
        f"3. Propose 2 ou 3 pistes d'investigation techniques universelles adaptées aux entreprises (ex : dérive de climatisation ou consigne trop basse, installations de froid, compresseurs, marche à vide les week-ends ou la nuit, dérive de puissance souscrite).\n"
        f"4. Termine par UNE action concrète prioritaire à vérifier sur place.\n"
        f"Rédige une réponse claire, directe et professionnelle en 3 à 4 phrases maximum."
    )

    reponse = demander_hypothese(question, anomalie.organisation_id)
    if "_error" in reponse:
        logger.warning("Hypothèse non générée pour l'anomalie %s : %s", anomalie.id, reponse["_error"])
        return None

    texte = (reponse.get("answer") or "").strip()
    if not texte:
        return None

    return Hypothese.objects.create(
        anomalie=anomalie,
        texte=texte,
        preuves=[obs.texte for obs in observations],
        confiance=None,
        statut=Hypothese.Statut.PROPOSEE,
        genere_par_ia=True,
    )