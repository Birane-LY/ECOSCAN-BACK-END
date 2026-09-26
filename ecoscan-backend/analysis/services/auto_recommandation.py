"""
Génération automatique d'une recommandation depuis une anomalie + hypothèse.

L'IA propose un titre, une description et une estimation d'économie.
L'utilisateur n'a qu'à valider ou rejeter — il ne saisit rien de zéro.

Règle : on crée UNE recommandation auto par anomalie maximum (idempotent).
Si une recommandation existe déjà, on ne recrée pas.
"""

import json
import logging
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from django.utils import timezone

from analysis.api.ai_client import demander_hypothese
from analysis.models import Anomalie, Recommandation
from analysis.services.recommandations import synchroniser_memoire_recommandations

logger = logging.getLogger(__name__)

# Tarif moyen indicatif Senelec BT PME pour valoriser les kWh en FCFA
TARIF_KWH_FCFA = Decimal("140")


def _obtenir_ou_creer_objectif(organisation):
    """Retourne l'objectif FCFA s'il existe, sinon le premier objectif ACTIF.
    Si aucun objectif n'est actif, crée automatiquement un objectif par défaut
    en FCFA pour que l'IA puisse toujours lier sa recommandation sans blocage."""
    from energy.models import Objectif

    objectif = (
        Objectif.objects.filter(organisation=organisation, statut=Objectif.Statut.ACTIF, unite__in=["FCFA", "XOF"])
        .order_by("date_debut")
        .first()
    )
    if objectif is not None:
        return objectif

    objectif = (
        Objectif.objects.filter(organisation=organisation, statut=Objectif.Statut.ACTIF)
        .order_by("date_debut")
        .first()
    )
    if objectif is not None:
        return objectif

    # Aucun objectif ACTIF : créer l'objectif par défaut
    debut = timezone.now()
    fin = debut + timedelta(days=365)
    try:
        objectif = Objectif.objects.create(
            organisation=organisation,
            nom="Réduire la facture énergétique",
            description="Objectif par défaut : réduire les dépenses liées à l'électricité (Senelec).",
            type="reduction_cout",
            valeur_cible=Decimal("10"),
            unite="FCFA",
            date_debut=debut,
            date_fin=fin,
            statut=Objectif.Statut.ACTIF,
        )
        logger.info(
            "Objectif automatique créé (%s) pour l'organisation %s.",
            objectif.id,
            organisation.id,
        )
        return objectif
    except Exception:
        logger.exception("Impossible de créer l'objectif automatique pour %s.", organisation.id)
        # Fallback de secours : n'importe quel objectif existant
        return Objectif.objects.filter(organisation=organisation).first()


def _economie_depuis_anomalie(anomalie: Anomalie) -> Decimal:
    """Estimation conservatrice en FCFA : 50 % du volume d'écart valorisé à 140 FCFA/kWh."""
    if anomalie.valeur_observee and anomalie.valeur_attendue:
        ecart_kwh = abs(anomalie.valeur_observee - anomalie.valeur_attendue)
        return (ecart_kwh * TARIF_KWH_FCFA * Decimal("0.5")).quantize(Decimal("0.01"))
    # Valeur forfaitaire réaliste si les valeurs absolues manquent
    return Decimal("25000.00")


def generer_recommandation_auto(anomalie: Anomalie) -> Recommandation | None:
    """Génère et persiste une recommandation automatique pour une anomalie.

    - Idempotent : retourne la recommandation existante si elle a déjà été créée.
    - Ne lève jamais d'exception : si l'IA est indisponible, utilise un fallback déterministe.
    - Met à jour le statut de l'anomalie en ACTION_CREATED pour que l'interface
      affiche le badge 'Recommandation créée' et ne demande plus de saisie manuelle.
    - La recommandation créée est PROPOSEE, pas DECIDEE : l'humain reste décisionnaire.
    """
    existante = Recommandation.objects.filter(anomalie=anomalie).first()
    if existante is not None:
        if anomalie.statut != Anomalie.Statut.ACTION_CREATED:
            anomalie.statut = Anomalie.Statut.ACTION_CREATED
            anomalie.save(update_fields=("statut",))
        return existante

    objectif = _obtenir_ou_creer_objectif(anomalie.organisation)
    if objectif is None:
        logger.warning(
            "Aucun objectif disponible pour l'organisation %s — recommandation auto impossible.",
            anomalie.organisation_id,
        )
        return None

    sens = "hausse" if anomalie.type == "consumption_spike" else "baisse"
    hypothese_texte = (
        anomalie.hypotheses.order_by("-date_creation").values_list("texte", flat=True).first()
        or "Variation inhabituelle de consommation constatée sur la facture."
    )

    question = (
        f"Une anomalie de consommation a été détectée pour une PME au Sénégal : {sens} de {anomalie.ecart_pourcentage}% "
        f"(sévérité : {anomalie.severite}).\n"
        f"Diagnostic retenu : {hypothese_texte}\n\n"
        "Propose UNE recommandation d'action concrète, opérationnelle et immédiatement réalisable par le gérant ou technicien.\n"
        "Réponds STRICTEMENT sous ce format JSON (aucun texte avant ni après) :\n"
        '{"titre": "...", "description": "...", "economie_estimee_fcfa": 35000}\n'
        "- titre : impératif, clair, max 80 caractères (ex: « Vérifier l'étalonnage et les index du compteur principal »)\n"
        "- description : 2 phrases explicatives sur les actions précises à entreprendre sur site\n"
        "- economie_estimee_fcfa : montant estimé en FCFA (nombre entier positif)"
    )

    reponse = demander_hypothese(question, anomalie.organisation_id)

    titre = None
    description = None
    economie = _economie_depuis_anomalie(anomalie)

    if "_error" not in reponse:
        texte_brut = (reponse.get("answer") or "").strip()
        try:
            debut = texte_brut.index("{")
            fin = texte_brut.rindex("}") + 1
            data = json.loads(texte_brut[debut:fin])
            titre = str(data.get("titre") or "").strip()[:180] or None
            description = str(data.get("description") or "").strip() or None
            val = data.get("economie_estimee_fcfa")
            if val is not None:
                economie = Decimal(str(val)).quantize(Decimal("0.01"))
        except (ValueError, KeyError, InvalidOperation, json.JSONDecodeError):
            logger.warning("Réponse IA non parseable en JSON pour recommandation auto, anomalie %s.", anomalie.id)

    if not titre:
        if anomalie.type == "consumption_drop":
            titre = f"Contrôler le compteur et les équipements suite à la baisse de {abs(float(anomalie.ecart_pourcentage))}%"
            description = (
                f"La consommation a chuté de {anomalie.ecart_pourcentage}%. "
                "Vérifier le bon fonctionnement des compteurs divisionnaires et s'assurer qu'aucun équipement clé n'a été arrêté anormalement."
            )
        else:
            titre = f"Traiter la hausse de consommation (+{anomalie.ecart_pourcentage}%)"
            description = (
                f"Une hausse de {anomalie.ecart_pourcentage}% a été constatée. "
                "Identifier les appareils fonctionnant en continu (froid, climatisation, moteurs) et poser des consignes d'extinction."
            )

    try:
        reco = Recommandation.objects.create(
            objectif=objectif,
            anomalie=anomalie,
            titre=titre,
            description=description,
            impact_estime=economie,
            economie_estimee=economie,
            unite="FCFA",
            priorite=(
                Recommandation.Priorite.CRITIQUE
                if anomalie.severite == Anomalie.Severite.INVESTIGATION_PRIORITAIRE
                else Recommandation.Priorite.HAUTE
                if anomalie.severite == Anomalie.Severite.ALERTE
                else Recommandation.Priorite.MOYENNE
            ),
            statut=Recommandation.Statut.PROPOSEE,
        )

        # Mettre à jour l'anomalie pour refléter la création de la recommandation
        anomalie.statut = Anomalie.Statut.ACTION_CREATED
        anomalie.save(update_fields=("statut",))

        # Répercuter dans la mémoire stratégique
        synchroniser_memoire_recommandations(anomalie.id)

        logger.info("Recommandation auto créée (%s) pour anomalie %s.", reco.id, anomalie.id)
        return reco
    except Exception:
        logger.exception("Erreur création recommandation auto pour anomalie %s.", anomalie.id)
        return None