"""
Transforme une Hypothese CONFIRMEE en MemoireStrategique — la seule voie
légitime de création de ce modèle. Appelé depuis HypotheseViewSet.confirmer().

La mémoire raconte l'histoire complète signal -> hypothèse -> action -> résultat.
Elle est indexée dans le RAG (contenu déjà validé par un humain) et RÉ-INDEXÉE
à chaque enrichissement (recommandation créée, impact mesuré) : le RAG remplace
l'ancienne version du même document. Indexation en arrière-plan : elle ne bloque
plus la requête.
"""

import logging

from analysis.api.ai_client import indexer_document
from analysis.models import Hypothese, MemoireStrategique
from analysis.services.taches import lancer_en_arriere_plan

logger = logging.getLogger(__name__)


def texte_indexable(memoire: MemoireStrategique) -> str:
    morceaux = [f"{memoire.titre}.", f"Signal initial : {memoire.signal_initial}"]
    if memoire.hypothese_texte:
        morceaux.append(f"Hypothèse confirmée : {memoire.hypothese_texte}")
    if memoire.action_texte:
        morceaux.append(f"Action menée : {memoire.action_texte}")
    if memoire.impact_attendu_fcfa is not None:
        morceaux.append(f"Impact attendu : {memoire.impact_attendu_fcfa} FCFA.")
    if memoire.impact_mesure_fcfa is not None:
        taux = f" (taux de réalisation {memoire.taux_realisation} %)" if memoire.taux_realisation is not None else ""
        morceaux.append(f"Impact mesuré : {memoire.impact_mesure_fcfa} FCFA{taux}.")
    return " ".join(morceaux)


def indexer_memoire_par_id(memoire_id) -> None:
    """Tâche d'arrière-plan : (ré)indexe la mémoire si elle n'est pas à jour."""
    memoire = MemoireStrategique.objects.filter(id=memoire_id).first()
    if memoire is None or memoire.indexee_rag:
        return
    resultat = indexer_document(
        organisation_id=memoire.organisation_id,
        document_id=memoire.id,
        texte=texte_indexable(memoire),
        source="memoire_strategique",
        metadata={"titre": memoire.titre, "statut": memoire.statut},
    )
    if "_error" in resultat:
        logger.warning("Mémoire %s non indexée dans le RAG : %s", memoire.id, resultat["_error"])
        return
    memoire.indexee_rag = True
    memoire.save(update_fields=("indexee_rag",))


def planifier_reindexation(memoire: MemoireStrategique) -> None:
    """Marque la mémoire « à ré-indexer » puis lance l'indexation en arrière-plan."""
    if memoire.indexee_rag:
        memoire.indexee_rag = False
        memoire.save(update_fields=("indexee_rag",))
    lancer_en_arriere_plan(indexer_memoire_par_id, memoire.id)


def creer_memoire_depuis_hypothese(hypothese: Hypothese) -> MemoireStrategique:
    if hypothese.statut != Hypothese.Statut.CONFIRMEE:
        raise ValueError(
            f"creer_memoire_depuis_hypothese attend une Hypothese CONFIRMEE, "
            f"reçu statut='{hypothese.statut}'."
        )

    anomalie = hypothese.anomalie
    titre = f"{anomalie.type.replace('_', ' ').capitalize()} — {anomalie.severite.replace('_', ' ').lower()}"

    memoire, _cree = MemoireStrategique.objects.get_or_create(
        anomalie=anomalie,
        defaults={
            "organisation": anomalie.organisation,
            "titre": titre,
            "signal_initial": (
                f"Écart de {anomalie.ecart_pourcentage}% par rapport à la baseline "
                f"(valeur observée : {anomalie.valeur_observee}, attendue : {anomalie.valeur_attendue})."
            ),
            "hypothese_texte": hypothese.texte,
            "statut": MemoireStrategique.Statut.A_VERIFIER,
            "sources": hypothese.preuves or [],
        },
    )
    planifier_reindexation(memoire)
    return memoire
