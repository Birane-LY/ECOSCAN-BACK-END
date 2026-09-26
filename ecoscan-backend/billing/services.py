"""
Services métier de facturation. Règle non négociable reprise du document
partagé : le paiement ne donne/retire jamais de droits directement — tout passe
par ces services, jamais par une vue qui modifierait un statut à la main.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Abonnement, Facture, HistoriqueAbonnement, Paiement, Plan, Relance

logger = logging.getLogger(__name__)

DUREE_ESSAI_JOURS = 14
DUREE_GRACE_JOURS = 7

# Combien de jours avant la fin d'un cycle ACTIVE on génère la facture du
# cycle suivant (laisse le temps à l'organisation de payer avant l'échéance).
DELAI_ANTICIPATION_RENOUVELLEMENT_JOURS = 3

# Calendrier de relances après échec — niveau : (jours après l'échec, canal)
CALENDRIER_RELANCES_ECHEC = [
    (0, Relance.Canal.IN_APP),
    (2, Relance.Canal.EMAIL),
    (5, Relance.Canal.EMAIL),
]


class BillingAccessService:
    """Point d'entrée UNIQUE pour décider si une organisation peut utiliser une
    fonctionnalité. Centralisé ici pour que la règle d'accès ne soit jamais
    dupliquée (et potentiellement désynchronisée) dans chaque vue métier."""

    # Statuts qui conservent l'accès — PAST_DUE et GRACE_PERIOD y sont
    # délibérément inclus : "il ne faut pas suspendre immédiatement après le
    # premier échec" (voir document partagé, section 6).
    #
    # IMPORTANT : ce statut seul ne suffit PAS à garantir un accès légitime —
    # voir le commentaire sur `abonnement_courant()` ci-dessous. Les tâches
    # planifiées (tasks.py) sont ce qui maintient ce statut synchronisé avec
    # la réalité (essai expiré -> EXPIRED, cycle non renouvelé -> PAST_DUE) ;
    # sans elles, un abonnement resterait TRIALING/ACTIVE indéfiniment même
    # après la date de fin de période.
    STATUTS_AVEC_ACCES = (
        Abonnement.Statut.TRIALING,
        Abonnement.Statut.ACTIVE,
        Abonnement.Statut.PAST_DUE,
        Abonnement.Statut.GRACE_PERIOD,
    )

    def abonnement_courant(self, organisation):
        return (
            organisation.abonnements
            .filter(statut__in=self.STATUTS_AVEC_ACCES)
            .order_by("-debut")
            .first()
        )

    def can_use_feature(self, organisation, feature_code: str) -> bool:
        abonnement = self.abonnement_courant(organisation)
        if abonnement is None:
            return False
        return bool(abonnement.plan.fonctionnalites.get(feature_code, False))

    def within_plan_limit(self, organisation, feature_code: str, usage_actuel: int) -> bool:
        """`feature_code` ici est une clé de `limites` (ex. "max_documents_month"),
        pas de `fonctionnalites`. Une limite absente ou None = illimitée."""
        abonnement = self.abonnement_courant(organisation)
        if abonnement is None:
            return False
        limite = abonnement.plan.limites.get(feature_code)
        if limite is None:
            return True
        return usage_actuel < limite

    def statut_acces(self, organisation) -> dict:
        """Résumé exploitable directement par le frontend — évite de dupliquer
        cette logique de présentation ailleurs."""
        abonnement = self.abonnement_courant(organisation)
        if abonnement is None:
            return {"acces": False, "statut": None, "message": "Aucun abonnement actif."}

        message = None
        if abonnement.statut == Abonnement.Statut.PAST_DUE:
            message = "Votre paiement a échoué. Merci de régulariser votre abonnement."
        elif abonnement.statut == Abonnement.Statut.GRACE_PERIOD:
            jours_restants = max((abonnement.fin_periode - timezone.now()).days, 0)
            message = f"Vous disposez encore de {jours_restants} jour(s) pour régulariser votre abonnement."
        elif abonnement.statut == Abonnement.Statut.TRIALING:
            jours_restants = max((abonnement.fin_periode - timezone.now()).days, 0)
            message = f"Essai gratuit — {jours_restants} jour(s) restant(s)."

        return {"acces": True, "statut": abonnement.statut, "plan": abonnement.plan.code, "message": message}


class SubscriptionService:
    """Transitions d'état de l'abonnement — jamais modifiées directement ailleurs
    (webhooks.py, tasks.py et les vues appellent ces méthodes, ne touchent
    jamais `abonnement.statut` à la main)."""

    def demarrer_essai_gratuit(self, organisation, plan: Plan) -> Abonnement:
        maintenant = timezone.now()
        abonnement = Abonnement.objects.create(
            organisation=organisation,
            plan=plan,
            statut=Abonnement.Statut.TRIALING,
            periodicite="MENSUEL",
            debut=maintenant,
            fin_periode=maintenant + timedelta(days=DUREE_ESSAI_JOURS),
            fournisseur=Abonnement.Fournisseur.MANUAL,
        )
        self._historiser(abonnement, ancien_statut="", nouveau_statut=abonnement.statut, raison="essai_gratuit_demarre")
        return abonnement

    def activer(self, abonnement: Abonnement, fin_periode) -> None:
        ancien_statut = abonnement.statut
        abonnement.statut = Abonnement.Statut.ACTIVE
        abonnement.fin_periode = fin_periode
        abonnement.save(update_fields=("statut", "fin_periode"))
        self._historiser(abonnement, ancien_statut=ancien_statut, nouveau_statut=abonnement.statut, raison="paiement_confirme")

    def mettre_en_grace(self, abonnement: Abonnement) -> None:
        ancien_statut = abonnement.statut
        abonnement.statut = Abonnement.Statut.GRACE_PERIOD
        abonnement.save(update_fields=("statut",))
        self._historiser(abonnement, ancien_statut=ancien_statut, nouveau_statut=abonnement.statut, raison="paiement_echoue")

    def suspendre(self, abonnement: Abonnement) -> None:
        ancien_statut = abonnement.statut
        abonnement.statut = Abonnement.Statut.SUSPENDED
        abonnement.save(update_fields=("statut",))
        self._historiser(abonnement, ancien_statut=ancien_statut, nouveau_statut=abonnement.statut, raison="grace_period_expiree")

    def expirer_essai(self, abonnement: Abonnement) -> None:
        """CORRECTIF : `Statut.EXPIRED` était défini dans le modèle mais
        jamais utilisé nulle part — un essai TRIALING dont `fin_periode` est
        dépassée sans qu'aucune souscription payante n'ait démarré restait
        donc TRIALING indéfiniment, et `STATUTS_AVEC_ACCES` inclut TRIALING :
        l'organisation gardait un accès complet et gratuit sans limite de
        temps. Voir tasks.expirer_essais_termines(), qui appelle ceci pour
        chaque abonnement concerné."""
        ancien_statut = abonnement.statut
        abonnement.statut = Abonnement.Statut.EXPIRED
        abonnement.save(update_fields=("statut",))
        self._historiser(abonnement, ancien_statut=ancien_statut, nouveau_statut=abonnement.statut, raison="essai_gratuit_expire")

    def annuler(self, abonnement: Abonnement, immediat: bool) -> None:
        ancien_statut = abonnement.statut
        abonnement.annule_le = timezone.now()
        if immediat:
            abonnement.statut = Abonnement.Statut.CANCELED
            abonnement.fin_acces_si_annule = timezone.now()
        else:
            abonnement.fin_acces_si_annule = abonnement.fin_periode
        abonnement.save(update_fields=("statut", "annule_le", "fin_acces_si_annule"))
        self._historiser(
            abonnement, ancien_statut=ancien_statut, nouveau_statut=abonnement.statut,
            raison="annulation_immediate" if immediat else "annulation_fin_de_periode",
        )

    def _historiser(self, abonnement, *, ancien_statut, nouveau_statut, raison, ancien_plan=None, nouveau_plan=None):
        HistoriqueAbonnement.objects.create(
            abonnement=abonnement,
            ancien_plan=ancien_plan,
            nouveau_plan=nouveau_plan or abonnement.plan,
            ancien_statut=ancien_statut,
            nouveau_statut=nouveau_statut,
            raison=raison,
            date_effet=timezone.now(),
        )


class InvoiceService:
    """Génère la facture d'un cycle. Une facture est une obligation financière,
    un paiement est une tentative — jamais fusionnés (voir document partagé)."""

    def generer_facture_cycle(self, abonnement: Abonnement) -> Facture:
        # Idempotence : ne jamais générer deux factures pour le même cycle
        # (même si cette méthode est appelée deux fois — souscription manuelle
        # ET tâche de renouvellement automatique, par exemple).
        existante = Facture.objects.filter(
            abonnement=abonnement, periode_debut=abonnement.fin_periode
        ).first()
        if existante:
            return existante

        montant_ht = (
            abonnement.plan.prix_annuel if abonnement.periodicite == "ANNUEL" and abonnement.plan.prix_annuel
            else abonnement.plan.prix_mensuel
        )
        duree = timedelta(days=365) if abonnement.periodicite == "ANNUEL" else timedelta(days=30)

        numero = f"FAC-{timezone.now():%Y%m%d%H%M%S}-{str(abonnement.id)[:8].upper()}"
        return Facture.objects.create(
            abonnement=abonnement,
            organisation=abonnement.organisation,
            numero=numero,
            montant_ht=montant_ht,
            taxes=Decimal("0"),
            montant_total=montant_ht,
            devise=abonnement.plan.devise,
            periode_debut=abonnement.fin_periode,
            periode_fin=abonnement.fin_periode + duree,
            date_echeance=abonnement.fin_periode,
            statut=Facture.Statut.OPEN,
        )


class RenewalService:
    """CORRECTIF : rien ne détectait qu'un abonnement ACTIVE arrivait en fin
    de cycle sans renouvellement — PayDunya n'ayant pas d'abonnement récurrent
    natif, aucun webhook n'est déclenché tant que personne n'initie
    explicitement un nouveau paiement. Sans ce service, un abonnement ACTIVE
    dont `fin_periode` est dépassée depuis des mois reste ACTIVE
    indéfiniment : c'est exactement le même trou que pour les essais (voir
    SubscriptionService.expirer_essai), mais sur le cycle payant.

    Ce service, appelé par tasks.traiter_renouvellements_dus(), génère la
    facture du cycle suivant en avance (DELAI_ANTICIPATION_RENOUVELLEMENT_JOURS
    avant l'échéance) et, si le cycle finit par expirer sans paiement, bascule
    l'abonnement en PAST_DUE et démarre le calendrier de relances — exactement
    le même traitement que _traiter_paiement_echoue() dans webhooks.py, pour
    le cas "jamais payé" plutôt que "paiement refusé"."""

    def generer_prochaines_factures(self) -> list:
        """Pour chaque abonnement ACTIVE dont le cycle se termine dans moins
        de DELAI_ANTICIPATION_RENOUVELLEMENT_JOURS jours, génère la facture du
        cycle suivant si elle n'existe pas déjà (idempotent, voir
        InvoiceService.generer_facture_cycle)."""
        horizon = timezone.now() + timedelta(days=DELAI_ANTICIPATION_RENOUVELLEMENT_JOURS)
        candidats = Abonnement.objects.filter(statut=Abonnement.Statut.ACTIVE, fin_periode__lte=horizon)

        service_factures = InvoiceService()
        factures = []
        for abonnement in candidats:
            facture = service_factures.generer_facture_cycle(abonnement)
            factures.append(facture)
        return factures

    def marquer_impayes_les_cycles_expires(self) -> int:
        """Pour chaque abonnement ACTIVE dont `fin_periode` est déjà dépassée
        ET dont la facture du cycle n'a jamais été payée, bascule en PAST_DUE
        et démarre les relances — l'organisation n'a simplement rien payé, pas
        de webhook d'échec à attendre."""
        compte = 0
        candidats = Abonnement.objects.filter(statut=Abonnement.Statut.ACTIVE, fin_periode__lt=timezone.now())
        for abonnement in candidats:
            facture_cycle = (
                Facture.objects.filter(abonnement=abonnement, periode_debut=abonnement.fin_periode)
                .exclude(statut=Facture.Statut.PAID)
                .first()
            )
            if facture_cycle is None:
                continue

            with transaction.atomic():
                facture_cycle.statut = Facture.Statut.PAST_DUE
                facture_cycle.save(update_fields=("statut",))
                SubscriptionService().mettre_en_grace(abonnement)
                DunningService().planifier_relances_echec(facture_cycle)
            compte += 1
        return compte


class DunningService:
    """Calendrier de relances après échec de paiement — construit la liste des
    Relance à créer, ne les envoie pas (voir tasks.py pour l'envoi effectif)."""

    def planifier_relances_echec(self, facture: Facture) -> list:
        relances = []
        for niveau, (jours, canal) in enumerate(CALENDRIER_RELANCES_ECHEC, start=1):
            relance, _cree = Relance.objects.get_or_create(
                idempotency_key=f"{facture.id}-echec-niveau-{niveau}",
                defaults={
                    "facture": facture,
                    "niveau": niveau,
                    "canal": canal,
                    "planifiee_le": timezone.now() + timedelta(days=jours),
                    "statut": Relance.Statut.PENDING,
                },
            )
            relances.append(relance)
        return relances

    def annuler_relances_en_attente(self, facture: Facture) -> int:
        return facture.relances.filter(statut=Relance.Statut.PENDING).update(statut=Relance.Statut.CANCELED)