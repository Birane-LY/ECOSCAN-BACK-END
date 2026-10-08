from rest_framework import serializers  # type: ignore

from .models import JournalAudit


class JournalAuditSerializer(serializers.ModelSerializer):
    """Sérialiseur STRICTEMENT en lecture : un journal d'audit n'a aucune raison
    d'accepter la moindre écriture depuis l'API, quelle que soit la vue qui
    l'utilise. Tous les champs sont read_only, pas seulement id/utilisateur/date —
    ce serializer ne doit jamais permettre de créer ou modifier une entrée, même
    si un jour une vue moins prudente que JournalAuditViewSet le réutilise."""

    utilisateur_nom = serializers.CharField(source="utilisateur.nom", read_only=True, default="")
    utilisateur_email = serializers.EmailField(source="utilisateur.email", read_only=True, default="")
    organisation_nom = serializers.CharField(source="organisation.nom", read_only=True, default="")

    class Meta:
        model = JournalAudit
        fields = (
            "id", "utilisateur", "utilisateur_nom", "utilisateur_email",
            "organisation", "organisation_nom", "action", "ressource",
            "identifiant_ressource", "resultat", "details", "adresse_ip", "date_action",
        )
        read_only_fields = fields