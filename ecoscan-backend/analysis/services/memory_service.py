"""
Transforme une Hypothese CONFIRMEE en MemoireStrategique — la seule voie
légitime de création de ce modèle (voir MemoireStrategique.__doc__).

Appelé depuis HypotheseViewSet.confirmer(), jamais depuis un autre point
d'entrée.

Limite actuelle assumée : Action n'a de lien que vers Recommandation, pas
vers Anomalie — impossible de savoir automatiquement quelle action a suivi
quelle anomalie. La mémoire démarre donc toujours à A_VERIFIER ; associer une
Action et un impact mesuré reste un geste humain ultérieur (voir la vue de
rattachement à écrire si besoin).
"""

from analysis.models import Hypothese, MemoireStrategique


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
    return memoire