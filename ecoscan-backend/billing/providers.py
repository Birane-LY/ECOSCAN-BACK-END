"""
Abstraction des prestataires de paiement.

PayDunya est l'implémentation principale (recommandation par défaut pour un
SaaS sénégalais : couvre Wave, Orange Money, Free Money et cartes via une seule
API). L'interface PaymentProvider est volontairement minimale pour qu'ajouter
PayTech ou un autre prestataire plus tard n'exige pas de retoucher
SubscriptionService ni le webhook — seulement d'écrire une nouvelle classe et
de l'enregistrer dans PROVIDERS.

Point important trouvé en documentation : PayDunya n'a PAS d'API d'abonnement
récurrent native — c'est un modèle facture + redirection de paiement. La
récurrence (génération d'une nouvelle facture à chaque cycle) est donc gérée
entièrement côté EcoScan (InvoiceService / RenewalService), pas déléguée au
prestataire.

CORRECTIF IMPORTANT (vérifié contre la doc officielle developers.paydunya.com/doc/FR/http_json,
section 8 "Configuration de l'IPN") :

1. Le hash renvoyé par PayDunya (`data.hash`) est le SHA-512 de la MASTER KEY
   SEULE — une valeur CONSTANTE, indépendante de la transaction. Ce n'est pas
   une signature cryptographique liée au paiement : quiconque observe ce hash
   une seule fois (IPN légitime intercepté, fuite de la clé) peut forger des
   événements "completed" arbitraires indéfiniment, pour n'importe quel
   montant. Ce n'est pas un bug de cette implémentation, c'est une faiblesse
   connue du mécanisme IPN de PayDunya lui-même.

   => Le webhook ne doit donc JAMAIS créditer un paiement sur la seule foi du
   corps de l'IPN. Il doit s'en servir uniquement comme DÉCLENCHEUR pour
   rappeler PayDunya en serveur-à-serveur via l'API "confirm"
   (`/checkout-invoice/confirm/{token}`), qui EST authentifiée par nos clés
   API et renvoie l'état réel de la transaction. C'est le webhook lui-même qui
   applique cette règle (voir webhooks.py) ; `recuperer_transaction()` ici
   fournit le moyen de le faire.

2. `custom_data` est un nœud FRÈRE de `invoice` dans la réponse PayDunya, pas
   un enfant de `invoice`. Le code précédent lisait
   `invoice.get("custom_data")`, qui renvoie toujours {} — `facture_id`
   n'était donc JAMAIS retrouvé, et TOUT paiement confirmé échouait
   silencieusement au traitement (voir _traiter_paiement_reussi dans
   webhooks.py, qui log une erreur et s'arrête sans lever d'exception).

3. Le `token` de la facture PayDunya est un enfant de `invoice`
   (`invoice.token`), pas un champ direct de `data`. Le code précédent lisait
   `data.get("token")`, qui renvoie toujours None.
"""

import hashlib
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class PaymentProviderError(Exception):
    """Erreur de communication avec le prestataire — distincte d'un paiement
    refusé (qui est un résultat normal, pas une erreur technique)."""


class PaymentProvider(ABC):
    @abstractmethod
    def creer_session_paiement(self, *, facture, return_url: str, cancel_url: str) -> dict:
        """Crée la session/facture côté prestataire. Retourne au minimum
        {'checkout_url': str, 'external_invoice_id': str}."""

    @abstractmethod
    def verifier_signature_webhook(self, payload: dict, headers: dict) -> bool:
        """Premier filtre, peu coûteux, avant même de regarder le contenu.
        NE JAMAIS considérer ceci comme une preuve suffisante du montant/statut
        réel — voir recuperer_transaction()."""

    @abstractmethod
    def extraire_reference_transaction(self, payload: dict) -> Optional[str]:
        """Extrait uniquement l'identifiant de transaction (token) du payload
        IPN reçu, pour pouvoir ensuite rappeler le prestataire en
        serveur-à-serveur. Ne renvoie AUCUNE autre donnée métier (montant,
        statut) — celles-ci ne doivent venir que de recuperer_transaction()."""

    @abstractmethod
    def recuperer_transaction(self, reference: str) -> dict:
        """Rappel serveur-à-serveur, authentifié par nos clés API, qui fait
        AUTORITÉ sur l'état réel de la transaction — contrairement au corps de
        l'IPN. Retourne :
        {'external_event_id', 'external_invoice_id', 'facture_id', 'statut',
         'montant', 'devise', 'methode', 'external_payment_id'}."""


class PayDunyaProvider(PaymentProvider):
    def __init__(self):
        self.master_key = getattr(settings, "PAYDUNYA_MASTER_KEY", "")
        self.private_key = getattr(settings, "PAYDUNYA_PRIVATE_KEY", "")
        self.public_key = getattr(settings, "PAYDUNYA_PUBLIC_KEY", "")
        self.token = getattr(settings, "PAYDUNYA_TOKEN", "")
        self.mode = getattr(settings, "PAYDUNYA_MODE", "test")  # "test" ou "live"
        self.base_url = (
            "https://app.paydunya.com/api/v1"
            if self.mode == "live"
            else "https://app.paydunya.com/sandbox-api/v1"
        )

    def _headers(self) -> dict:
        return {
            "PAYDUNYA-MASTER-KEY": self.master_key,
            "PAYDUNYA-PRIVATE-KEY": self.private_key,
            "PAYDUNYA-PUBLIC-KEY": self.public_key,
            "PAYDUNYA-TOKEN": self.token,
            "Content-Type": "application/json",
        }

    def creer_session_paiement(self, *, facture, return_url: str, cancel_url: str) -> dict:
        if not (self.master_key and self.private_key and self.public_key and self.token):
            raise PaymentProviderError("Clés PayDunya non configurées (MASTER/PRIVATE/PUBLIC_KEY, TOKEN).")

        payload = {
            "invoice": {
                "total_amount": int(facture.montant_total),
                "description": f"EcoScan — {facture.numero}",
                "items": {
                    "item_0": {
                        "name": f"Abonnement {facture.abonnement.plan.nom}",
                        "unit_price": str(int(facture.montant_ht)),
                        "quantity": "1",
                        "total_price": str(int(facture.montant_ht)),
                    }
                },
            },
            "store": {"name": "EcoScan"},
            "actions": {"return_url": return_url, "cancel_url": cancel_url},
            # custom_data est un nœud racine, frère de "invoice" — PAS un enfant.
            "custom_data": {"facture_id": str(facture.id)},
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                r = client.post(f"{self.base_url}/checkout-invoice/create", json=payload, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"PayDunya indisponible : {exc}") from exc

        if data.get("response_code") != "00":
            raise PaymentProviderError(f"PayDunya a refusé la création de facture : {data.get('response_text')}")

        return {
            "checkout_url": data["response_text"],  # l'URL de paiement est renvoyée dans response_text
            "external_invoice_id": data.get("token", ""),
        }

    def verifier_signature_webhook(self, payload: dict, headers: dict) -> bool:
        """Filtre rapide, insuffisant seul (voir docstring de module) — sert à
        écarter le bruit évident avant d'aller interroger recuperer_transaction()."""
        import secrets

        data = payload.get("data", payload)  # PayDunya poste sous la clé "data"
        hash_recu = data.get("hash", "")
        hash_attendu = hashlib.sha512(self.master_key.encode("utf-8")).hexdigest()
        if not hash_recu or not self.master_key:
            return False
        return secrets.compare_digest(hash_recu, hash_attendu)

    def extraire_reference_transaction(self, payload: dict) -> Optional[str]:
        data = payload.get("data", payload)
        invoice = data.get("invoice", {})
        return invoice.get("token") or None

    def recuperer_transaction(self, reference: str) -> dict:
        """GET /checkout-invoice/confirm/{token} — source de vérité."""
        try:
            with httpx.Client(timeout=15.0) as client:
                r = client.get(
                    f"{self.base_url}/checkout-invoice/confirm/{reference}",
                    headers=self._headers(),
                )
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"PayDunya indisponible (confirm) : {exc}") from exc

        if data.get("response_code") != "00":
            raise PaymentProviderError(f"PayDunya : transaction introuvable pour {reference}.")

        invoice = data.get("invoice", {})
        custom_data = data.get("custom_data", {})  # frère de "invoice", pas enfant
        statut_paydunya = data.get("status", "")

        statut_normalise = {
            "completed": "SUCCEEDED",
            "cancelled": "CANCELED",
            "failed": "FAILED",
            "pending": "PENDING",
        }.get(statut_paydunya, "PENDING")

        token = invoice.get("token", reference)
        return {
            "external_event_id": token,
            "external_invoice_id": token,
            "facture_id": custom_data.get("facture_id"),
            "statut": statut_normalise,
            "montant": Decimal(str(invoice.get("total_amount", 0))),
            "devise": "XOF",
            # PayDunya ne renvoie pas le canal utilisé (Wave/OM/carte...) dans
            # cette réponse standard — laissé vide plutôt qu'une valeur inventée.
            "methode": "",
            "external_payment_id": token,
        }


class ManualProvider(PaymentProvider):
    """Paiement manuel (virement, mobile money hors ligne) validé par un
    administrateur — pas de webhook réel, la confirmation vient d'une action
    humaine explicite (voir BillingService.confirmer_paiement_manuel)."""

    def creer_session_paiement(self, *, facture, return_url: str, cancel_url: str) -> dict:
        return {"checkout_url": "", "external_invoice_id": f"MANUAL-{facture.numero}"}

    def verifier_signature_webhook(self, payload: dict, headers: dict) -> bool:
        return False  # ManualProvider n'a jamais de webhook entrant à vérifier

    def extraire_reference_transaction(self, payload: dict) -> Optional[str]:
        return None

    def recuperer_transaction(self, reference: str) -> dict:
        raise NotImplementedError("ManualProvider ne traite pas de webhooks.")


PROVIDERS = {
    "PAYDUNYA": PayDunyaProvider,
    "MANUAL": ManualProvider,
    # "PAYTECH": PayTechProvider,  # à ajouter si besoin — même interface
}


def obtenir_provider(nom: str) -> PaymentProvider:
    try:
        return PROVIDERS[nom]()
    except KeyError:
        raise ValueError(f"Prestataire de paiement '{nom}' non implémenté. Disponibles : {sorted(PROVIDERS)}")