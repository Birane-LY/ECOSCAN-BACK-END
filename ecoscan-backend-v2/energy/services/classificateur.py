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

INDICES_SENEGAL = (
    "sénégal",
    "senegal",
    "dakar",
    "thiès",
    "thies",
    "saint-louis",
    "kaolack",
    "ziguinchor",
    "fcfa",
    "xof",
)
EMETTEURS_SENEGAL = ("senelec",)
INDICES_DOCUMENT = (
    "n° facture",
    "facture n°",
    "numero facture",
    "ancien index",
    "nouvel index",
    "nouveau index",
    "montant total",
    "total facture",
    "periode du",
    "période du",
    "compte de contrat",
    "n° partenaire",
)

SEUIL_REJET_PERTINENCE = 0.40


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
                "pays_detecte": None,
                "authenticite": {
                    "statut": "REJETE",
                    "motif": "Le document contient des signaux non énergétiques.",
                    "verification_officielle_disponible": False,
                },
            }

        emetteur = next(
            (nom.upper() for nom in EMETTEURS_SENEGAL if nom in texte_norm),
            None,
        )
        est_ticket_woyofal = "woyofal" in texte_norm and bool(
            re.search(r"\b(?:recharge|ticket|token|jeton)\b", texte_norm)
        )
        indices_senegal = [indice for indice in INDICES_SENEGAL if indice in texte_norm]
        indices_document = [
            indice for indice in INDICES_DOCUMENT if indice in texte_norm
        ]
        score = pertinence["score"]

        if est_ticket_woyofal:
            return {
                "type": "TICKET_RECHARGE_WOYOFAL",
                "relevance_score": score,
                "decision": "reject",
                "emetteur": emetteur or "NON_VERIFIE",
                "signals": pertinence["signaux"],
                "negative_signals": [],
                "pays_detecte": "SENEGAL" if indices_senegal or emetteur else None,
                "motif_rejet": (
                    "Ticket de recharge Woyofal détecté. Ce document n'est pas une facture "
                    "de consommation : saisissez séparément le montant payé et les kWh "
                    "crédités indiqués sur le ticket; ils ne représentent pas la consommation."
                ),
                "authenticite": {
                    "statut": "NON_VERIFIEE",
                    "motif": (
                        "Le ticket doit être vérifié séparément avant saisie; son montant "
                        "ne permet pas de déduire les kWh crédités."
                    ),
                    "verification_officielle_disponible": False,
                },
            }

        if score < SEUIL_REJET_PERTINENCE:
            return {
                "type": "DOCUMENT_NON_ENERGETIQUE",
                "relevance_score": score,
                "decision": "reject",
                "emetteur": emetteur or "AUTRE_FOURNISSEUR",
                "signals": pertinence["signaux"],
                "negative_signals": [],
                "pays_detecte": "SENEGAL" if indices_senegal or emetteur else None,
                "authenticite": {
                    "statut": "REJETE",
                    "motif": "Les indices énergétiques sont insuffisants.",
                    "verification_officielle_disponible": False,
                },
            }

        # OCR and issuer keywords identify a candidate, but the import pipeline
        # must run its independent quality and consistency checks before publication.
        return {
            "type": "FACTURE_SENELEC" if emetteur else "DOCUMENT_ENERGETIQUE_A_REVOIR",
            "relevance_score": score,
            "decision": "candidate",
            "emetteur": emetteur or "NON_VERIFIE",
            "signals": pertinence["signaux"],
            "negative_signals": [],
            "pays_detecte": "SENEGAL" if indices_senegal or emetteur else None,
            "indices_pays": indices_senegal,
            "indices_document": indices_document,
            "authenticite": {
                "statut": "NON_VERIFIEE",
                "motif": (
                    "Le contenu semble énergétique, mais aucune source officielle "
                    "ne permet de vérifier automatiquement l'émetteur ou l'origine."
                ),
                "verification_officielle_disponible": False,
            },
        }
