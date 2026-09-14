from decimal import Decimal
from typing import Any, Dict, List
from datetime import datetime, timedelta
from django.utils import timezone

from energy.models import DonneeEnergetique
from analysis.models import ResultatMetrique
from .baselines import calculer_baseline
from .exceptions import IndexIncoherentError
from .registry import DefinitionMetrique, QualiteRequise, obtenir_definition


class MetricsService:
    """Calcule et persiste les métriques de performance isolées par organisation."""

    def __init__(self, organisation):
        self.organisation = organisation

    def _evaluer_qualite(self, releves_count: int, qualite_requise: QualiteRequise) -> dict:
        nombre_observations = releves_count
        completude = (
            min(nombre_observations / qualite_requise.nombre_observations_min, 1.0)
            if qualite_requise.nombre_observations_min > 0
            else 1.0
        )
        suffisante = (
            nombre_observations >= qualite_requise.nombre_observations_min
            and completude >= qualite_requise.completude_min
        )
        return {
            "suffisante": suffisante,
            "completude": round(completude, 4),
            "nombre_observations": nombre_observations,
        }

    def _resultat_insuffisant(self, definition: DefinitionMetrique, qualite: dict, periode: dict) -> dict:
        return {
            "metric": definition.code,
            "version": definition.version,
            "value": None,
            "unit": definition.unite,
            "period": periode,
            "baseline": None,
            "data_quality": {
                "completeness": qualite["completude"],
                "observations": qualite["nombre_observations"],
                "status": "insuffisant",
            },
            "confidence": None,
            "sources": list(definition.sources_requises),
            "limitations": list(definition.limites),
            "decision_supported": definition.decision_associee,
            "message": "Pas assez de données validées pour calculer cette métrique de façon fiable.",
        }

    def calculer_consommation_journaliere(self, compteur, date_debut: datetime, date_fin: datetime) -> dict:
        definition = obtenir_definition("consommation_journaliere")
        periode = {"from": date_debut.isoformat(), "to": date_fin.isoformat()}

        releves = list(
            DonneeEnergetique.objects.filter(
                compteur=compteur,
                compteur__site__organisation=self.organisation,
                statut_validation=DonneeEnergetique.StatutValidation.VALIDEE,
                periode_fin__gte=date_debut,
                periode_fin__lte=date_fin,
            ).order_by("periode_fin")
        )

        qualite = self._evaluer_qualite(len(releves), definition.qualite_requise)
        if not qualite["suffisante"]:
            return self._resultat_insuffisant(definition, qualite, periode)

        index_precedent = Decimal(str(releves[0].valeur))
        index_actuel = Decimal(str(releves[-1].valeur))

        if index_actuel < index_precedent:
            raise IndexIncoherentError(
                f"Index incohérent pour le compteur {compteur.reference} : {index_actuel} < {index_precedent}."
            )

        valeur = index_actuel - index_precedent
        valeur_float = float(valeur)

        resultat = {
            "metric": definition.code,
            "version": definition.version,
            "value": valeur_float,
            "unit": definition.unite,
            "period": periode,
            "baseline": None,
            "observed": valeur_float,
            "data_quality": {
                "completeness": qualite["completude"],
                "observations": qualite["nombre_observations"],
                "status": "fiable",
            },
            "confidence": 1.0,
            "sources": list(definition.sources_requises),
            "limitations": list(definition.limites),
            "decision_supported": definition.decision_associee,
        }

        self._persister(definition, compteur, date_debut, date_fin, resultat)
        return resultat

    def calculer_consommation_par_creneau(self, compteur, date_jour: datetime) -> dict:
        definition = obtenir_definition("consommation_par_creneau")
        debut_jour = date_jour.replace(hour=0, minute=0, second=0, microsecond=0)
        fin_jour = debut_jour + timedelta(days=1)
        periode = {"from": debut_jour.isoformat(), "to": fin_jour.isoformat()}

        releves = list(
            DonneeEnergetique.objects.filter(
                compteur=compteur,
                compteur__site__organisation=self.organisation,
                statut_validation=DonneeEnergetique.StatutValidation.VALIDEE,
                periode_fin__gte=debut_jour,
                periode_fin__lt=fin_jour,
            ).order_by("periode_fin")
        )

        qualite = self._evaluer_qualite(len(releves), definition.qualite_requise)
        if not qualite["suffisante"]:
            return self._resultat_insuffisant(definition, qualite, periode)

        consommations_par_creneau = []
        for precedent, actuel in zip(releves, releves[1:]):
            valeur_prec = Decimal(str(precedent.valeur))
            valeur_act = Decimal(str(actuel.valeur))
            
            if valeur_act < valeur_prec:
                raise IndexIncoherentError(
                    f"Index incohérent entre {precedent.periode_fin} et {actuel.periode_fin} pour le compteur {compteur.reference}."
                )
            
            consommations_par_creneau.append({
                "creneau": actuel.periode_fin.strftime("%Hh"),
                "valeur": float(valeur_act - valeur_prec)
            })

        return {
            "metric": definition.code,
            "version": definition.version,
            "value": consommations_par_creneau,
            "unit": definition.unite,
            "period": periode,
            "baseline": None,
            "data_quality": {
                "completeness": qualite["completude"],
                "observations": qualite["nombre_observations"],
                "status": "fiable",
            },
            "confidence": 1.0,
            "sources": list(definition.sources_requises),
            "limitations": list(definition.limites),
            "decision_supported": definition.decision_associee,
        }

    def calculer_pic_observe(self, compteur, date_jour: datetime) -> dict:
        definition = obtenir_definition("pic_observe")
        consommation_creneaux = self.calculer_consommation_par_creneau(compteur, date_jour)

        if consommation_creneaux["value"] is None or not consommation_creneaux["value"]:
            qualite = consommation_creneaux["data_quality"]
            return self._resultat_insuffisant(
                definition,
                {
                    "suffisante": False,
                    "completude": qualite["completeness"],
                    "nombre_observations": qualite["observations"],
                },
                consommation_creneaux["period"],
            )

        creneaux = consommation_creneaux["value"]
        pic = max(creneaux, key=lambda c: c["valeur"])

        return {
            "metric": definition.code,
            "version": definition.version,
            "value": pic["valeur"],
            "unit": definition.unite,
            "period": consommation_creneaux["period"],
            "baseline": None,
            "observed": {"creneau": pic["creneau"], "valeur": pic["valeur"]},
            "data_quality": consommation_creneaux["data_quality"],
            "confidence": consommation_creneaux["confidence"],
            "sources": list(definition.sources_requises),
            "limitations": list(definition.limites),
            "decision_supported": definition.decision_associee,
            "label": f"Pic observé à {pic['creneau']}",
        }

    def calculer_variation_vs_baseline(self, compteur, date_jour: datetime) -> dict:
        definition = obtenir_definition("variation_vs_baseline")
        debut_jour = date_jour.replace(hour=0, minute=0, second=0, microsecond=0)
        fin_jour = debut_jour + timedelta(days=1)

        observation = self.calculer_consommation_journaliere(compteur, debut_jour, fin_jour)

        if observation["value"] is None:
            return self._resultat_insuffisant(
                definition,
                {
                    "suffisante": False,
                    "completude": observation["data_quality"]["completeness"],
                    "nombre_observations": observation["data_quality"]["observations"],
                },
                observation["period"],
            )

        nombre_semaines = definition.baseline.parametres.get("nombre_semaines", 4)
        valeurs_reference = self._recuperer_consommations_meme_jour_semaine(compteur, date_jour, nombre_semaines)

        baseline = calculer_baseline(definition.baseline.type, valeurs_reference)
        
        # Validation de la qualité de la baseline
        if (
            baseline.get("valeur") is None
            or baseline["nombre_observations"] < definition.qualite_requise.nombre_observations_min
        ):
            completude_baseline = (
                baseline.get("nombre_observations", 0) / definition.qualite_requise.nombre_observations_min
                if definition.qualite_requise.nombre_observations_min > 0
                else 0
            )
            return self._resultat_insuffisant(
                definition,
                {
                    "suffisante": False,
                    "completude": min(completude_baseline, 1.0),
                    "nombre_observations": baseline.get("nombre_observations", 0),
                },
                observation["period"],
            )

        valeur_baseline = baseline["valeur"]
        
        # Protection contre la division par zéro
        if valeur_baseline == 0:
            variation = 0.0 if observation["value"] == 0 else 100.0
        else:
            variation = ((observation["value"] - valeur_baseline) / valeur_baseline) * 100

        resultat = {
            "metric": definition.code,
            "version": definition.version,
            "value": round(variation, 2),
            "unit": definition.unite,
            "period": observation["period"],
            "baseline": {
                "type": baseline["type"],
                "value": round(valeur_baseline, 2),
                "nombre_observations": baseline["nombre_observations"],
            },
            "observed": observation["value"],
            "data_quality": observation["data_quality"],
            "confidence": round(
                min(baseline["nombre_observations"] / definition.qualite_requise.nombre_observations_min, 1.0), 4
            ),
            "sources": list(definition.sources_requises),
            "limitations": list(definition.limites),
            "decision_supported": definition.decision_associee,
        }

        self._persister(definition, compteur, debut_jour, fin_jour, resultat)
        return resultat

    def _recuperer_consommations_meme_jour_semaine(self, compteur, date_jour: datetime, nombre_semaines: int) -> list:
        valeurs = []
        for i in range(1, nombre_semaines + 1):
            jour_ref = date_jour - timedelta(weeks=i)
            debut_ref = jour_ref.replace(hour=0, minute=0, second=0, microsecond=0)
            fin_ref = debut_ref + timedelta(days=1)
            try:
                res = self.calculer_consommation_journaliere(compteur, debut_ref, fin_ref)
                if res["value"] is not None:
                    valeurs.append(res["value"])
            except IndexIncoherentError:
                continue
        return valeurs

    def _persister(self, definition: DefinitionMetrique, compteur, date_debut: datetime, date_fin: datetime, resultat: dict) -> None:
        if resultat.get("value") is None:
            return
            
        baseline = resultat.get("baseline") or {}
        
        ResultatMetrique.objects.update_or_create(
            organisation=self.organisation,
            compteur=compteur,
            code_metrique=definition.code,
            periode_debut=date_debut,
            periode_fin=date_fin,
            version_metrique=definition.version,
            defaults={
                "valeur": Decimal(str(resultat["value"])) if isinstance(resultat["value"], (int, float)) else None,
                "unite": definition.unite,
                "baseline_type": baseline.get("type", ""),
                "baseline_valeur": Decimal(str(baseline["value"])) if baseline.get("value") is not None else None,
                "baseline_nombre_observations": baseline.get("nombre_observations", 0),
                "completude": Decimal(str(resultat["data_quality"]["completeness"])),
                "statut_qualite": ResultatMetrique.StatutQualite.FIABLE,
                "confiance": Decimal(str(resultat["confidence"])) if resultat.get("confidence") is not None else None,
                "sources": resultat["sources"],
                "limites": resultat["limitations"],
            },
        )