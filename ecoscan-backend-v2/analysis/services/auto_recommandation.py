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

def _est_woyofal_rituel(anomalie):
    resultat = anomalie.resultat_metrique
    return bool(
        resultat
        and resultat.code_metrique == "variation_woyofal_rituelle_vs_moyenne_recente"
    )


def _contexte_provenance(anomalie):
    resultat = anomalie.resultat_metrique
    if resultat and resultat.code_metrique == "variation_woyofal_rituelle_vs_moyenne_recente":
        return "relevés Woyofal saisis manuellement"

    modes = {
        source.get("mode")
        for source in (resultat.sources if resultat else [])
        if isinstance(source, dict) and source.get("mode")
    }
    if modes == {"SIMULATED"}:
        return "télémétrie simulée"
    if modes == {"REAL"}:
        return "télémétrie réelle"
    if modes:
        return "télémétrie mixte (réelle et simulée)"
    return "provenance non précisée"


def _obtenir_ou_creer_objectif(organisation, woyofal_rituel=False, anomalie=None):
    """Retourne ou crée un objectif énergétique compatible avec l'anomalie."""
    from energy.models import Objectif

    if woyofal_rituel:
        objectif = Objectif.objects.filter(
            organisation=organisation,
            statut=Objectif.Statut.ACTIF,
            unite__iexact="kWh",
        ).order_by("date_debut").first()
        if objectif:
            return objectif
        if anomalie and anomalie.type != "consumption_spike":
            return None
        reference = anomalie.valeur_attendue if anomalie else None
        if reference is None or reference <= 0:
            logger.warning(
                "Objectif Woyofal non créé pour l'anomalie %s : référence kWh absente ou nulle.",
                getattr(anomalie, "id", None),
            )
            return None
        debut = timezone.now()
        return Objectif.objects.create(
            organisation=organisation,
            nom="Réduire la consommation Woyofal",
            description=(
                "Objectif par défaut de réduction de 10 % de la référence Woyofal, "
                "mesurée entre 08 h et 20 h."
            ),
            type="reduction_energie",
            valeur_cible=max(
                Decimal("0.001"),
                (Decimal(reference) * Decimal("0.10")).quantize(Decimal("0.001")),
            ),
            unite="kWh",
            date_debut=debut,
            date_fin=debut + timedelta(days=365),
            statut=Objectif.Statut.ACTIF,
        )

    objectif = Objectif.objects.filter(
        organisation=organisation,
        statut=Objectif.Statut.ACTIF,
        unite__iexact="kWh",
    ).order_by("date_debut").first()
    if objectif is not None:
        return objectif

    if anomalie and anomalie.type != "consumption_spike":
        return None
    reference = anomalie.valeur_attendue if anomalie else None
    if reference is None or reference <= 0:
        logger.warning(
            "Objectif kWh non créé pour l'anomalie %s : référence absente ou nulle.",
            getattr(anomalie, "id", None),
        )
        return None

    debut = timezone.now()
    objectif = Objectif.objects.create(
        organisation=organisation,
        nom="Réduire la consommation électrique",
        description=(
            "Objectif automatiquement proposé à partir de la consommation de référence "
            "observée; la mesure conserve la provenance des données."
        ),
        type="REDUCTION_CONSOMMATION",
        valeur_cible=max(
            Decimal("0.001"),
            (Decimal(reference) * Decimal("0.10")).quantize(Decimal("0.001")),
        ),
        unite="kWh",
        date_debut=debut,
        date_fin=debut + timedelta(days=365),
        statut=Objectif.Statut.ACTIF,
    )
    logger.info(
        "Objectif énergétique automatique créé (%s) pour l'organisation %s.",
        objectif.id,
        organisation.id,
    )
    return objectif


def _economie_depuis_anomalie(anomalie: Anomalie) -> Decimal:
    """Écart énergétique à investiguer, sans le présenter comme une économie acquise."""
    if (
        anomalie.type != "consumption_spike"
        or anomalie.valeur_observee is None
        or anomalie.valeur_attendue is None
    ):
        return Decimal("0.00")
    ecart_kwh = max(
        Decimal("0"),
        Decimal(anomalie.valeur_observee) - Decimal(anomalie.valeur_attendue),
    )
    return ecart_kwh.quantize(Decimal("0.01"))


def _recommandation_woyofal(anomalie):
    """Construit une piste en kWh observés sans inventer de tarif Woyofal."""
    if anomalie.type != "consumption_spike":
        return (
            f"Vérifier la baisse Woyofal de {abs(float(anomalie.ecart_pourcentage))}% "
            "et confirmer que les relevés, recharges et équipements sont cohérents.",
            "Comparer les soldes saisis et les recharges du jour, puis vérifier les équipements "
            "qui auraient pu être arrêtés. Cette anomalie porte uniquement sur la fenêtre mesurée 08 h–20 h.",
            Decimal("0"),
        )

    ecart_kwh = max(
        Decimal("0"),
        Decimal(anomalie.valeur_observee or 0) - Decimal(anomalie.valeur_attendue or 0),
    )
    variation = float(anomalie.ecart_pourcentage)
    return (
        f"Examiner la hausse Woyofal de {variation:.1f}% entre 08 h et 20 h",
        "Comparer les relevés et recharges de chaque créneau, puis vérifier les équipements actifs "
        "pendant la période. Le potentiel indicatif correspond à l'écart observé par rapport à la "
        "référence 08 h–20 h ; il ne garantit pas une économie.",
        ecart_kwh.quantize(Decimal("0.001")),
    )


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

    woyofal_rituel = _est_woyofal_rituel(anomalie)
    objectif = _obtenir_ou_creer_objectif(anomalie.organisation, woyofal_rituel, anomalie)
    if objectif is None:
        logger.warning(
            "Aucun objectif disponible pour l'organisation %s — recommandation auto impossible.",
            anomalie.organisation_id,
        )
        return None

    sens = "hausse" if anomalie.type == "consumption_spike" else "baisse"
    provenance = _contexte_provenance(anomalie)
    hypothese_texte = (
        anomalie.hypotheses.order_by("-date_creation").values_list("texte", flat=True).first()
        or "Variation inhabituelle de consommation constatée sur la facture."
    )

    question = (
        f"Une anomalie de consommation a été détectée via {provenance} : "
        f"{sens} de {anomalie.ecart_pourcentage}% "
        f"(sévérité : {anomalie.severite}).\n"
        f"Diagnostic retenu : {hypothese_texte}\n\n"
        "Propose UNE recommandation d'action concrète, opérationnelle et immédiatement réalisable par le gérant ou technicien.\n"
        "Ne déduis pas de montant financier ni d'économie garantie à partir de l'écart.\n"
        "Si la télémétrie est simulée ou mixte, formule une piste à tester et ne la présente jamais comme un fait réel.\n"
        "Réponds STRICTEMENT sous ce format JSON (aucun texte avant ni après) :\n"
        '{"titre": "...", "description": "..."}\n'
        "- titre : impératif, clair, max 80 caractères (ex: « Vérifier l'étalonnage et les index du compteur principal »)\n"
        "- description : 2 phrases explicatives sur les actions précises à entreprendre sur site"
    )

    if woyofal_rituel:
        titre, description, economie = _recommandation_woyofal(anomalie)
    else:
        reponse = demander_hypothese(question, anomalie.organisation_id)
        titre = None
        description = None
        economie = _economie_depuis_anomalie(anomalie)

    if not woyofal_rituel and "_error" not in reponse:
        texte_brut = (reponse.get("answer") or "").strip()
        try:
            debut = texte_brut.index("{")
            fin = texte_brut.rindex("}") + 1
            data = json.loads(texte_brut[debut:fin])
            titre = str(data.get("titre") or "").strip()[:180] or None
            description = str(data.get("description") or "").strip() or None
        except (ValueError, KeyError, InvalidOperation, json.JSONDecodeError):
            logger.warning("Réponse IA non parseable en JSON pour recommandation auto, anomalie %s.", anomalie.id)

    if not titre or not description:
        if anomalie.type == "consumption_drop":
            titre = titre or f"Contrôler le compteur et les équipements suite à la baisse de {abs(float(anomalie.ecart_pourcentage))}%"
            description = description or (
                f"La consommation a chuté de {anomalie.ecart_pourcentage}%. "
                "Vérifier le bon fonctionnement des compteurs divisionnaires et s'assurer qu'aucun équipement clé n'a été arrêté anormalement."
            )
        else:
            titre = titre or f"Traiter la hausse de consommation (+{anomalie.ecart_pourcentage}%)"
            description = description or (
                f"Une hausse de {anomalie.ecart_pourcentage}% a été constatée. "
                "Identifier les appareils fonctionnant en continu (froid, climatisation, moteurs) et poser des consignes d'extinction."
            )

    if provenance == "télémétrie simulée":
        description += "\n\nProvenance : simulation de télémétrie, à confirmer sur le terrain."
    elif provenance == "télémétrie mixte (réelle et simulée)":
        description += "\n\nProvenance : télémétrie mixte; vérifier les capteurs simulés sur le terrain."
    elif provenance == "relevés Woyofal saisis manuellement":
        description += "\n\nProvenance : relevés rituels saisis manuellement."

    try:
        reco = Recommandation.objects.create(
            objectif=objectif,
            anomalie=anomalie,
            titre=titre,
            description=description,
            impact_estime=economie,
            economie_estimee=economie,
            unite="kWh",
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