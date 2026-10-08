"""
Exécution en arrière-plan des traitements lents (appels IA, indexation RAG).

Avant : la génération d'hypothèse (jusqu'à 60 s) et l'indexation RAG tournaient
DANS la requête HTTP — l'utilisateur attendait devant un import « qui tourne ».

Solution sans dépendance : un petit pool de threads, lancé APRÈS le commit de la
transaction en cours (transaction.on_commit), avec fermeture propre des
connexions base de données. C'est suffisant pour une PME ; pour un volume
supérieur, remplacer le corps de `lancer_en_arriere_plan` par
`ma_tache.delay(...)` (Celery) sans toucher aux appelants.

Réglage utile : ECOSCAN_TACHES_SYNCHRONES = True (tests, dépannage) exécute
tout immédiatement, dans le processus appelant.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections, transaction

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ecoscan-tache")


def _executer(fonction, args, kwargs):
    close_old_connections()
    try:
        fonction(*args, **kwargs)
    except Exception:
        logger.exception("Tâche en arrière-plan en échec : %s", getattr(fonction, "__name__", fonction))
    finally:
        close_old_connections()


def lancer_en_arriere_plan(fonction, *args, **kwargs) -> None:
    """Planifie `fonction(*args, **kwargs)`. Ne lève jamais d'exception.

    Passer des identifiants (pas des instances de modèle) : la tâche recharge
    l'objet depuis la base, dans son propre thread."""
    if getattr(settings, "ECOSCAN_TACHES_SYNCHRONES", False):
        _executer(fonction, args, kwargs)
        return

    def _soumettre():
        _executor.submit(_executer, fonction, args, kwargs)

    try:
        transaction.on_commit(_soumettre)  # exécuté tout de suite hors transaction
    except Exception:
        logger.exception("Impossible de planifier la tâche %s.", getattr(fonction, "__name__", fonction))
