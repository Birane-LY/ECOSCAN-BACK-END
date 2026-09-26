"""
Endpoint webhook des prestataires de paiement.

Public au niveau de l'authentification EcoScan (le prestataire n'a pas de
session utilisateur), mais protégé par la vérification de signature du
prestataire — jamais l'inverse.

CORRECTIF : l'IPN n'est plus qu'un DÉCLENCHEUR. Le hash PayDunya est constant
(SHA-512 de la master key, indépendant de la transaction — voir providers.py),
donc le corps de l'IPN seul n'authentifie ni le montant ni le statut annoncés.
Dès que la signature basique passe, on rappelle immédiatement PayDunya en
serveur-à-serveur (recuperer_transaction) et on ne fait confiance qu'à CETTE
réponse pour créditer un paiement. On vérifie en plus que le montant confirmé
correspond au montant réellement dû sur la facture, avant de la marquer payée.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

from .models import Facture, Paiement, WebhookEvent
from .providers import PaymentProviderError, obtenir_provider
from .services import DunningService, SubscriptionService

logger = logging.getLogger(__name__)

# Tolérance sur l'écart montant confirmé / montant dû — les prestataires
# arrondissent parfois différemment le XOF (pas de sous-unité). 1 FCFA suffit.
TOLERANCE_MONTANT = 1


@api_view(["POST"])
@permission_classes([AllowAny])
def webhook_paiement(request, fournisseur: str):
    """AllowAny est correct ICI et seulement ici : le prestataire de paiement
    n'a pas de compte utilisateur EcoScan. La sécurité de fond vient de
    recuperer_transaction(), jamais de IsAuthenticated ni du seul hash IPN."""
    fournisseur = fournisseur.upper()
    try:
        provider = obtenir_provider(fournisseur)
    except ValueError:
        return Response({"error": "Fournisseur inconnu."}, status=status.HTTP_404_NOT_FOUND)

    payload = request.data

    if not provider.verifier_signature_webhook(payload, dict(request.headers)):
        logger.warning("Signature webhook invalide reçue pour le fournisseur %s.", fournisseur)
        return Response({"error": "Signature invalide."}, status=status.HTTP_401_UNAUTHORIZED)

    reference = provider.extraire_reference_transaction(payload)
    if not reference:
        return Response({"error": "Référence de transaction introuvable dans le payload."}, status=status.HTTP_400_BAD_REQUEST)

    # Dès ce point, on n'utilise plus AUCUNE donnée du payload IPN lui-même :
    # tout vient de l'appel serveur-à-serveur ci-dessous.
    try:
        evenement = provider.recuperer_transaction(reference)
    except PaymentProviderError as exc:
        logger.error("Échec de la reconfirmation PayDunya pour %s : %s", reference, exc)
        return Response({"error": "Impossible de reconfirmer la transaction."}, status=status.HTTP_502_BAD_GATEWAY)

    external_event_id = evenement["external_event_id"]
    if not external_event_id:
        return Response({"error": "Événement sans identifiant exploitable."}, status=status.HTTP_400_BAD_REQUEST)

    # Déduplication AVANT tout traitement — un même événement peut être
    # retransmis par le prestataire (timeout de notre côté, retry réseau).
    if WebhookEvent.objects.filter(fournisseur=fournisseur, external_event_id=external_event_id).exists():
        return Response({"status": "already_processed"}, status=status.HTTP_200_OK)

    with transaction.atomic():
        WebhookEvent.objects.create(
            fournisseur=fournisseur,
            external_event_id=external_event_id,
            event_type=evenement.get("statut", ""),
            payload=payload,
        )

        if evenement["statut"] == "SUCCEEDED":
            _traiter_paiement_reussi(evenement)
        elif evenement["statut"] in ("FAILED", "CANCELED"):
            _traiter_paiement_echoue(evenement)
        # Les statuts intermédiaires (PENDING) sont enregistrés (WebhookEvent
        # ci-dessus) mais ne déclenchent aucune transition d'état.

    return Response({"status": "processed"}, status=status.HTTP_200_OK)


def _traiter_paiement_reussi(evenement: dict) -> None:
    facture_id = evenement.get("facture_id")
    if not facture_id:
        logger.error("Événement de paiement réussi sans facture_id exploitable : %s", evenement)
        return

    try:
        facture = Facture.objects.select_for_update().get(id=facture_id)
    except Facture.DoesNotExist:
        logger.error("Facture %s introuvable pour un paiement confirmé.", facture_id)
        return

    # Le montant confirmé DOIT correspondre à ce qui est réellement dû —
    # sans ce contrôle, un montant falsifié en amont (même via un canal
    # normalement fiable) pourrait solder une facture pour moins que son dû.
    ecart = abs(evenement["montant"] - facture.montant_total)
    if ecart > TOLERANCE_MONTANT:
        logger.error(
            "Montant confirmé (%s) ne correspond pas au montant dû (%s) pour la facture %s — "
            "paiement enregistré mais facture NON soldée, à vérifier manuellement.",
            evenement["montant"], facture.montant_total, facture.id,
        )
        Paiement.objects.update_or_create(
            external_payment_id=evenement["external_payment_id"],
            defaults={
                "facture": facture, "organisation": facture.organisation,
                "montant": evenement["montant"], "devise": evenement["devise"],
                "methode": evenement.get("methode", ""), "statut": Paiement.Statut.SUCCEEDED,
                "date_paiement": timezone.now(),
                "metadata": {"alerte": "montant_incoherent_avec_facture"},
            },
        )
        return

    Paiement.objects.update_or_create(
        external_payment_id=evenement["external_payment_id"],
        defaults={
            "facture": facture,
            "organisation": facture.organisation,
            "montant": evenement["montant"],
            "devise": evenement["devise"],
            "methode": evenement.get("methode", ""),
            "statut": Paiement.Statut.SUCCEEDED,
            "date_paiement": timezone.now(),
        },
    )

    facture.statut = Facture.Statut.PAID
    facture.save(update_fields=("statut",))

    abonnement = facture.abonnement
    SubscriptionService().activer(abonnement, fin_periode=facture.periode_fin)
    DunningService().annuler_relances_en_attente(facture)


def _traiter_paiement_echoue(evenement: dict) -> None:
    facture_id = evenement.get("facture_id")
    if not facture_id:
        logger.error("Événement de paiement échoué sans facture_id exploitable : %s", evenement)
        return

    try:
        facture = Facture.objects.select_for_update().get(id=facture_id)
    except Facture.DoesNotExist:
        logger.error("Facture %s introuvable pour un paiement échoué.", facture_id)
        return

    facture.statut = Facture.Statut.PAST_DUE
    facture.save(update_fields=("statut",))

    SubscriptionService().mettre_en_grace(facture.abonnement)
    DunningService().planifier_relances_echec(facture)