"""
Exceptions d'intégrité analytique pour le module analysis.
"""

class QualiteInsuffisanteError(Exception):
    """Levée quand les données disponibles n'atteignent pas la complétude requise."""
    pass


class IndexIncoherentError(Exception):
    """Levée quand un index physique extrait présente une régression arithmétique."""
    pass
