from __future__ import annotations

from typing import Any, Dict
from energy.services.ocr import MoteurOCRService, OCRServiceError
from energy.services.classificateur import ClassificateurService
from energy.services.parseur import ParseurMetierService


class OCRService:
    """Façade orchestrant la chaîne d'ingestion et de contrôle qualité des documents."""

    def __init__(self):
        self.moteur = MoteurOCRService()
        self.classificateur = ClassificateurService()
        self.parseur = ParseurMetierService()

    def traiter_import(self, import_instance) -> str:
        """Déclenche l'extraction physique et alimente le cache immuable (Bronze Layer)."""
        fichier_source = import_instance.fichier_source

        if fichier_source.texte_brut_cache:
            return fichier_source.texte_brut_cache

        # Appel au moteur d'extraction physique découplé
        texte = self.moteur.extraire_texte_brut(fichier_source)

        if texte and texte.strip():
            fichier_source.texte_brut_cache = texte
            fichier_source.save(update_fields=("texte_brut_cache",))

        return texte

    def calculer_score_lisibilite(self, texte: str) -> float:
        return self.classificateur.calculer_score_lisibilite(texte)

    def classifier_document(self, texte: str) -> Dict[str, Any]:
        return self.classificateur.classifier_document(texte)

    def extraire_champs_energetiques(self, texte: str, type_document: str = "FACTURE_SENELEC") -> Dict[str, Any]:
        return self.parseur.extraire_champs_energetiques(texte, type_document)

    def valider_champs_energetiques(self, champs: Dict[str, Any]) -> Dict[str, Any]:
        return self.parseur.valider_champs_energetiques(champs)
