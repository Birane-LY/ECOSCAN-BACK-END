"""
Ce fichier isole les index de consommation et applique les règles de cohérence syntaxique.
"""

import re
from typing import Any, Dict, Optional

_NOMBRE_GROUPE = r"\d[\d ]*\d|\d"
_SAUT_RENVOIS = r"(?:\(\s*\d+\s*\)\s*\+?\s*)*"


class ParseurMetierService:
    """Isole les index numériques et certifie la cohérence arithmétique des variables."""

    def extraire_champs_energetiques(self, texte: str, type_document: str = "FACTURE_SENELEC") -> Dict[str, Any]:
        champs = {
            "numero_facture": self._extraire_regex(texte, r"(?:n°\s*facture|facture\s*n°)\s*[:.]?\s*([A-Z0-9\-]+)"),
            "numero_partenaire": self._extraire_regex(texte, r"n°\s*partenaire\s*[:.]?\s*([0-9\-]+)"),
            "police": self._extraire_regex(texte, r"(?:n°\s*police|police)\s*[:.]?\s*([0-9\-]+)"),
            "numero_compteur": self._extraire_regex(texte, r"(?:n°\s*compteur|compteur\s*n°|compteur)\s*[:.]?\s*([A-Z0-9\-]+)"),
            "numero_compte": self._extraire_regex(texte, r"(?:n°\s*compte\s*de\s*contrat|compte\s*de\s*contrat|n°\s*de\s*compte|compte\s*n°)\s*[:.]?\s*([0-9\-]+)"),
            "date_facture": self._extraire_regex(texte, r"\bdate\s*[:.]?\s*(\d{2}[/\.-]\d{2}[/\.-]\d{4})"),
            "date_limite_paiement": self._extraire_regex(texte, r"date\s*limite\s*de\s*paiement\s*[:.]?\s*(\d{2}[/\.-]\d{2}[/\.-]\d{4})"),
            "unite": "kWh", "devise": "FCFA",
        }

        est_multi_compteur = bool(re.search(r"\bk1\b", texte, re.IGNORECASE)) and bool(re.search(r"\bk2\b", texte, re.IGNORECASE))
        if est_multi_compteur:
            champs.update(self._extraire_index_multi_compteur(texte))
        else:
            champs.update(self._extraire_index_simple(texte))

        if champs.get("consommation_kwh") is None and champs.get("ancien_index") is not None and champs.get("nouveau_index") is not None:
            delta = champs["nouveau_index"] - champs["ancien_index"]
            if delta >= 0:
                champs["consommation_kwh"] = delta

        champs["montant_net_paye"] = self._extraire_montant_total(texte)
        champs["index"] = champs.get("nouveau_index")
        champs["montant"] = champs["montant_net_paye"]
        champs["date"] = champs.get("date_facture")
        return champs

    def valider_champs_energetiques(self, champs: Dict[str, Any]) -> Dict[str, Any]:
        erreurs = []
        warnings = []
        ancien_index = champs.get("ancien_index")
        nouveau_index = champs.get("nouveau_index")
        consommation = champs.get("consommation_kwh")
        montant = champs.get("montant_net_paye")

        if ancien_index is not None and ancien_index < 0:
            erreurs.append({"code": "INDEX_NEGATIF", "message": "L'ancien index est négatif."})
        if nouveau_index is not None and nouveau_index < 0:
            erreurs.append({"code": "INDEX_NEGATIF", "message": "Le nouvel index est négatif."})

        if ancien_index is not None and nouveau_index is not None and nouveau_index < ancien_index:
            erreurs.append({"code": "INDEX_INCOHERENT", "message": f"Le nouvel index ({nouveau_index}) est inférieur à l'ancien ({ancien_index})."})
        elif ancien_index is not None and nouveau_index is not None and consommation is not None:
            delta_calcul = nouveau_index - ancien_index
            if abs(delta_calcul - consommation) > 0.01:
                warnings.append({"code": "ECART_CONSOMMATION", "message": f"La consommation calculée ({delta_calcul} kWh) diffère du total imprimé ({consommation} kWh)."})

        if montant is not None and montant <= 0:
            erreurs.append({"code": "MONTANT_INVALIDE", "message": "Le montant total doit être supérieur à 0 FCFA."})
        if nouveau_index is None and consommation is None:
            erreurs.append({"code": "METRIQUE_MANQUANTE", "message": "Aucun index ni volume de consommation (kWh) n'a pu être extrait."})
        elif nouveau_index is None:
            warnings.append({"code": "INDEX_MANQUANT", "message": "Le nouvel index n'a pas été détecté."})
        if montant is None:
            warnings.append({"code": "MONTANT_MANQUANT", "message": "Le montant total n'a pas été détecté."})

        return {"valide": len(erreurs) == 0, "erreurs": erreurs, "warnings": warnings, "requires_review": bool(erreurs or warnings)}

    @staticmethod
    def _extraire_index_simple(texte: str) -> Dict[str, Optional[float]]:
        return {
            "ancien_index": ParseurMetierService._extraire_nombre(texte, rf"ancien\s*index\s*(?:\([a-z]{{1,3}}\))?\s*[:.]?\s*({_NOMBRE_GROUPE})"),
            "nouveau_index": ParseurMetierService._extraire_nombre(texte, rf"nouv(?:eau|el)\s*index\s*(?:\([a-z]{{1,3}}\))?\s*[:.]?\s*({_NOMBRE_GROUPE})"),
            "consommation_kwh": ParseurMetierService._extraire_nombre(texte, rf"consommation\s*\(?kwh\)?\s*[:.]?\s*({_NOMBRE_GROUPE})"),
        }

    @staticmethod
    def _extraire_index_multi_compteur(texte: str) -> Dict[str, Optional[float]]:
        ancien = ParseurMetierService._extraire_derniere_valeur(texte, r"ancien\s*index[^\n]*")
        nouveau = ParseurMetierService._extraire_derniere_valeur(texte, r"nouv(?:eau|el)\s*index[^\n]*")
        consommation = ParseurMetierService._extraire_derniere_valeur(texte, r"consommation[^\n]*")
        total_a_facturer = ParseurMetierService._extraire_derniere_valeur(texte, r"total\s*[aà]\s*facturer[^\n]*")
        return {"ancien_index": ancien, "nouveau_index": nouveau, "consommation_kwh": consommation if consommation is not None else total_a_facturer}

    @staticmethod
    def _extraire_montant_total(texte: str) -> Optional[float]:
        patterns = [
            rf"montant\s*total\s*ttc\s*{_SAUT_RENVOIS}[:.]?\s*({_NOMBRE_GROUPE})",
            rf"total\s*des\s*sommes\s*dues\s*{_SAUT_RENVOIS}[:.]?\s*({_NOMBRE_GROUPE})",
            rf"montant\s*total\s*{_SAUT_RENVOIS}[:.]?\s*({_NOMBRE_GROUPE})",
            rf"total\s*facture\s*{_SAUT_RENVOIS}[:.]?\s*({_NOMBRE_GROUPE})",
        ]
        for pattern in patterns:
            match = re.search(pattern, texte, re.IGNORECASE)
            if match:
                try: return float(match.group(1).strip().replace(" ", "").replace(",", "."))
                except ValueError: continue
        return None

    @staticmethod
    def _extraire_regex(texte: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, texte, re.IGNORECASE)
        return match.group(1).strip() if match else None

    @staticmethod
    def _extraire_nombre(texte: str, pattern: str) -> Optional[float]:
        match = re.search(pattern, texte, re.IGNORECASE)
        if not match: return None
        try: return float(match.group(1).strip().replace(" ", "").replace(",", "."))
        except ValueError: return None

    @staticmethod
    def _extraire_derniere_valeur(texte: str, pattern_ligne: str) -> Optional[float]:
        match = re.search(pattern_ligne, texte, re.IGNORECASE)
        if not match: return None
        segment = re.sub(r"\b(?:k[12]|total)\b", " ", match.group(0), flags=re.IGNORECASE)
        tokens = segment.split()
        nombres = []
        i = 0
        while i < len(tokens):
            if re.fullmatch(r"\d+", tokens[i]):
                valeur = tokens[i]
                j = i + 1
                while j < len(tokens) and re.fullmatch(r"\d{3}", tokens[j]):
                    valeur += tokens[j]
                    j += 1
                nombres.append(valeur.replace(",", "."))
                i = j
            else: i += 1
        if not nombres: return None
        try: return float(nombres[-1])
        except ValueError: return None
