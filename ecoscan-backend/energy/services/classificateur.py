""" 
Ce fichier héberge nos dictionnaires Senelec et calcule la légitimité énergétique
"""

import re
from typing import Any, Dict

MOTS_CLES_FORTS: Dict[str, float] = {
    "senelec": 0.40, "woyofal": 0.40, "kwh": 0.30, "ancien index": 0.30,
    "nouvel index": 0.30, "nouveau index": 0.25, "consommation": 0.25,
    "puissance souscrite": 0.25, "puissance transfo": 0.15, "montant total": 0.15,
    "total facture": 0.15, "total des sommes dues": 0.15, "n° partenaire": 0.10, "compte de contrat": 0.10,
}

MOTS_CLES_FAIBLES: Dict[str, float] = {
    "index": 0.05, "police": 0.05, "tranche": 0.05, "n° facture": 0.05, "facture n°": 0.05,
    "fcfa": 0.03, "tva": 0.03, "redevance": 0.03,
}

MOTS_CLES_NON_ENERGETIQUES = {
    "bulletin de salaire", "contrat de travail", "curriculum vitae", "passeport",
    "carte nationale d'identité", "certificat médical", "relevé bancaire", "bon de commande", "attestation de travail",
}

SEUIL_REJET_PERTINENCE = 0.40
SEUIL_ACCEPTATION_PERTINENCE = 0.65


class ClassificateurService:
    """Analyse la structure sémantique et la lisibilité du texte extrait."""

    def calculer_score_lisibilite(self, texte: str) -> float:
        if not texte or not texte.strip():
            return 0.0
        total_caracteres = len(texte)
        caracteres_suspects = len(re.findall(r"[^\w\s.,;:€$%\-\(\)/àâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ°]", texte, flags=re.UNICODE))
        confiance = max(0.00, 100.00 - ((caracteres_suspects / total_caracteres) * 100.00))
        return round(confiance / 100.00, 4)

    def calculer_pertinence_energetique(self, texte: str) -> Dict[str, Any]:
        texte_normalise = texte.casefold()
        score = 0.0
        signaux = []
        for mot, poids in {**MOTS_CLES_FORTS, **MOTS_CLES_FAIBLES}.items():
            if mot in texte_normalise:
                score += poids
                signaux.append(mot)
        return {"score": round(min(score, 1.00), 4), "signaux": signaux}

    def detecter_signaux_negatifs(self, texte: str) -> list:
        texte_normalise = texte.casefold()
        return [mot for mot in MOTS_CLES_NON_ENERGETIQUES if mot in texte_normalise]

    def classifier_document(self, texte: str) -> Dict[str, Any]:
        texte_norm = texte.casefold()
        pertinence = self.calculer_pertinence_energetique(texte)
        signaux_negatifs = self.detecter_signaux_negatifs(texte)

        if signaux_negatifs:
            return {
                "type": "DOCUMENT_NON_ENERGETIQUE", "relevance_score": 0.0,
                "decision": "reject", "emetteur": "AUTRE_FOURNISSEUR",
                "signals": pertinence["signaux"], "negative_signals": signaux_negatifs,
            }

        is_senelec = "senelec" in texte_norm or "woyofal" in texte_norm
        has_facture = "facture" in texte_norm or "total facture" in texte_norm
        has_index = "index" in texte_norm or "kwh" in texte_norm
        score = pertinence["score"]

        if is_senelec and (has_facture or has_index):
            type_doc = "FACTURE_SENELEC"
            decision = "accept_extraction"
        elif score >= SEUIL_ACCEPTATION_PERTINENCE:
            type_doc = "FACTURE_ENERGIE"
            decision = "accept_extraction"
        elif score >= SEUIL_REJET_PERTINENCE:
            type_doc = "DOCUMENT_ENERGETIQUE_A_REVOIR"
            decision = "human_review"
        else:
            type_doc = "DOCUMENT_NON_ENERGETIQUE"
            decision = "reject"

        return {
            "type": type_doc, "relevance_score": score, "decision": decision,
            "emetteur": "SENELEC" if is_senelec else "AUTRE_FOURNISSEUR",
            "signals": pertinence["signaux"], "negative_signals": [],
        }
