"""
Registre officiel des fiches de justification des métriques d'EcoScan.
Fige les objectifs, les questions métiers et les contraintes de complétude.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class QualiteRequise:
    """Conditions minimales de qualité pour autoriser la validation d'une métrique."""
    completude_min: float = 0.80
    statut_validation_requis: Optional[str] = "VALIDEE"
    nombre_observations_min: int = 1


@dataclass(frozen=True)
class DefinitionBaseline:
    """Référence sémantique de comparaison par rapport à un historique de référence."""
    type: str
    parametres: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DefinitionMetrique:
    """Fiche de justification complète d'une métrique d'analyse déterministe."""
    code: str
    nom: str
    objectif: str
    question_metier: str
    decision_associee: str
    formule_description: str
    unite: str
    frequence: str
    sources_requises: tuple
    qualite_requise: QualiteRequise
    limites: tuple
    proprietaire: str
    version: str
    baseline: Optional[DefinitionBaseline] = None

    def __post_init__(self):
        if not self.limites:
            raise ValueError(
                f"La métrique '{self.code}' n'a aucune limite déclarée. "
                "Une métrique sans limite explicite ne doit pas être publiée."
            )


REGISTRE_METRIQUES = {
    "consommation_journaliere": DefinitionMetrique(
        code="consommation_journaliere",
        nom="Consommation journalière",
        objectif="Savoir combien d'énergie a été consommée sur la journée.",
        question_metier="Combien avons-nous consommé aujourd'hui ?",
        decision_associee="Comparer la journée à une référence et détecter une dérive.",
        formule_description="Index du jour - index précédent, sur le même compteur.",
        unite="kWh",
        frequence="quotidienne",
        sources_requises=("releves_compteur_valides",),
        qualite_requise=QualiteRequise(completude_min=1.0, nombre_observations_min=2),
        limites=("Ne montre pas à quel moment de la journée la consommation a eu lieu.",),
        proprietaire="energy_analytics",
        version="1.0",
    ),
    "consommation_par_creneau": DefinitionMetrique(
        code="consommation_par_creneau",
        nom="Consommation par créneau",
        objectif="Révéler les variations de consommation à l'intérieur d'une même journée.",
        question_metier="Entre quels créneaux la consommation augmente-t-elle le plus ?",
        decision_associee="Investiguer un horaire, une machine ou une opération.",
        formule_description="Index du créneau actuel - index du créneau précédent.",
        unite="kWh",
        frequence="par créneau (09h, 13h, 17h)",
        sources_requises=("releves_compteur_valides_multi_creneaux",),
        qualite_requise=QualiteRequise(completude_min=1.0, nombre_observations_min=2),
        limites=("Mesure une variation entre observations discrètes, pas une puissance instantanée réelle.",),
        proprietaire="energy_analytics",
        version="1.0",
    ),
    "pic_observe": DefinitionMetrique(
        code="pic_observe",
        nom="Pic de consommation observé",
        objectif="Repérer le créneau où la consommation est la plus élevée.",
        question_metier="Quel créneau présente la consommation la plus élevée ?",
        decision_associee="Observer les équipements et opérations actifs à ce moment.",
        formule_description="Maximum des consommations par créneau sur la période.",
        unite="kWh par intervalle observé",
        frequence="quotidienne",
        sources_requises=("consommation_par_creneau",),
        qualite_requise=QualiteRequise(completude_min=1.0, nombre_observations_min=2),
        limites=("N'est PAS une puissance maximale en kW — seulement le plus haut niveau observé.",),
        proprietaire="energy_analytics",
        version="1.0",
    ),
    "variation_vs_baseline": DefinitionMetrique(
        code="variation_vs_baseline",
        nom="Variation de consommation par rapport à la baseline",
        objectif="Détecter une amélioration ou une dérive par rapport à une référence.",
        question_metier="Consommons-nous plus ou moins que prévu ?",
        decision_associee="Investiguer une dérive ou confirmer l'effet d'une action.",
        formule_description="(valeur observée - baseline) / baseline × 100.",
        unite="%",
        frequence="quotidienne",
        sources_requises=("consommation_journaliere",),
        qualite_requise=QualiteRequise(completude_min=0.80, nombre_observations_min=4),
        baseline=DefinitionBaseline(type="moyenne_jour_semaine", parametres={"nombre_semaines": 4}),
        limites=(
            "Une baseline courte (4 semaines) peut être sensible à un événement exceptionnel.",
            "Ne tient pas compte du volume de production ou de la météo sans normalisation externe.",
        ),
        proprietaire="energy_analytics",
        version="1.0",
    ),
    "consommation_facture_periodique": DefinitionMetrique(
        code="consommation_facture_periodique",
        nom="Consommation facturée (période de facturation)",
        objectif=(
            "Intégrer la consommation totale d'une facture énergétique comme référence "
            "historique, sans la confondre avec un relevé journalier ou de créneau."
        ),
        question_metier="Quelle consommation et quel coût moyen cette facture représente-t-elle ?",
        decision_associee=(
            "Alimenter l'historique mensuel, le coût moyen par kWh et la baseline longue "
            "période — jamais la baseline journalière ou par créneau, qui suppose une "
            "granularité différente."
        ),
        formule_description=(
            "Nouvel index - ancien index tel qu'extrait de la facture ; "
            "coût moyen facturé = montant total / consommation."
        ),
        unite="kWh",
        frequence="par facture",
        sources_requises=("facture_energie_validee",),
        qualite_requise=QualiteRequise(completude_min=1.0, nombre_observations_min=1),
        limites=(
            "Représente une période de facturation complète (souvent 30 à 60 jours pour "
            "Senelec), pas une journée ni un créneau — ne jamais l'agréger directement "
            "avec consommation_journaliere ou consommation_par_creneau.",
            "La période réelle de consommation (periode_debut/periode_fin) n'est fiable "
            "que si l'OCR a explicitement extrait le champ 'PERIODE DU ... AU ...' de la "
            "facture ; à défaut, une approximation est utilisée et signalée comme telle "
            "dans le résultat, jamais présentée comme une période confirmée.",
            "Le coût moyen dérivé inclut taxes, redevances et frais fixes — ce n'est PAS "
            "le tarif unitaire pur du kWh.",
        ),
        proprietaire="energy_analytics",
        version="1.0",
    ),
    "variation_facture_vs_facture_precedente": DefinitionMetrique(
    code="variation_facture_vs_facture_precedente",
    nom="Variation de consommation entre deux factures consécutives",
    objectif=(
        "Détecter une dérive ou une amélioration de consommation pour les comptes "
        "facturés au réel, où les relevés ne sont pas assez fréquents pour une "
        "comparaison journalière (voir variation_vs_baseline, pensée pour Woyofal "
        "ou des capteurs automatiques)."
    ),
    question_metier="Notre consommation facturée augmente-t-elle ou diminue-t-elle par rapport à la période précédente ?",
    decision_associee="Investiguer une dérive de consommation entre deux factures, ou confirmer l'effet d'une action déjà décidée.",
    formule_description="(consommation de la facture actuelle - consommation de la facture précédente) / facture précédente × 100.",
    unite="%",
    frequence="par facture",
    sources_requises=("consommation_facture_periodique",),
    qualite_requise=QualiteRequise(completude_min=1.0, nombre_observations_min=2),
    limites=(
        "Compare deux périodes de facturation qui peuvent avoir des durées "
        "légèrement différentes (30 à 60 jours selon Senelec) — la variation "
        "n'est pas normalisée par jour, seulement par période facturée.",
        "Une seule facture précédente sert de référence, pas une moyenne — plus "
        "sensible à un événement exceptionnel isolé qu'une baseline sur plusieurs "
        "semaines.",
        "Ne tient pas compte du volume de production ou de la météo sans "
        "normalisation externe.",
    ),
    proprietaire="energy_analytics",
    version="1.0",
),
}


def obtenir_definition(code: str) -> DefinitionMetrique:
    """Récupère de façon sécurisée la définition d'une métrique métier."""
    try:
        return REGISTRE_METRIQUES[code]
    except KeyError:
        raise KeyError(f"Métrique '{code}' non enregistrée. Disponibles : {sorted(REGISTRE_METRIQUES)}")