from rest_framework import serializers

from .models import Abonnement, Facture, HistoriqueAbonnement, Paiement, Plan, Relance, WebhookEvent


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = ("id", "code", "nom", "description", "prix_mensuel", "prix_annuel", "devise", "limites", "fonctionnalites", "actif", "date_creation")
        read_only_fields = ("id", "date_creation")


class AbonnementSerializer(serializers.ModelSerializer):
    """Lecture seule au niveau des champs métier : un abonnement ne change
    d'état QUE via SubscriptionService (souscription, webhook, annulation
    demandée par l'API dédiée) — jamais par un PATCH générique sur `statut`.

    CORRECTIF : `read_only_fields` référençait "external_customer_id" et
    "external_subscription_id", absents du tuple `fields` — DRF valide que
    `read_only_fields` est un sous-ensemble de `fields` à la première
    instanciation du serializer et lève une AssertionError sinon. Concrètement,
    ce bug faisait planter (500) le tout premier appel à AbonnementViewSet,
    quelle que soit l'action. Les deux champs sont maintenant inclus dans
    `fields` (utiles en lecture pour le support/debug) et restent read-only.
    """

    class Meta:
        model = Abonnement
        fields = (
            "id", "organisation", "plan", "statut", "periodicite", "debut", "fin_periode",
            "annule_le", "fin_acces_si_annule", "fournisseur",
            "external_customer_id", "external_subscription_id", "date_creation",
        )
        read_only_fields = (
            "id", "statut", "debut", "fin_periode", "annule_le", "fin_acces_si_annule",
            "external_customer_id", "external_subscription_id", "date_creation",
        )


class FactureSerializer(serializers.ModelSerializer):
    class Meta:
        model = Facture
        fields = (
            "id", "abonnement", "organisation", "numero", "montant_ht", "taxes", "montant_total",
            "devise", "periode_debut", "periode_fin", "date_echeance", "statut", "pdf_url", "date_creation",
        )
        read_only_fields = fields  # générées uniquement par InvoiceService/webhooks


class PaiementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Paiement
        fields = ("id", "facture", "organisation", "montant", "devise", "methode", "statut", "date_paiement", "date_creation")
        read_only_fields = fields  # créés uniquement par le traitement webhook


class RelanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Relance
        fields = ("id", "facture", "niveau", "canal", "planifiee_le", "envoyee_le", "statut")
        read_only_fields = fields