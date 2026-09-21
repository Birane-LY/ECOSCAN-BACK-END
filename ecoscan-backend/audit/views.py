from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from organizations.models import Organisation
from .models import JournalAudit
from .serializers import JournalAuditSerializer


class JournalAuditViewSet(viewsets.ReadOnlyModelViewSet):
    """Consultation du journal d'audit — visibilité différenciée par rôle,
    volontairement DIFFÉRENTE de la politique "SUPER_ADMIN exclu des objets
    métier" appliquée partout ailleurs (energy, analysis) :

      - SUPER_ADMIN : voit TOUT, toutes organisations confondues. C'est un choix
        délibéré, pas un oubli — un journal d'audit sert la conformité et
        l'investigation de sécurité au niveau plateforme, ce qui est un besoin
        différent de la confidentialité des données métier des tenants. Si cette
        politique doit plutôt être alignée sur l'exclusion habituelle, c'est une
        décision produit à trancher explicitement, pas quelque chose à décider
        silencieusement dans le code.
      - ADMIN_ORGANISATION : voit toutes les entrées de SES organisations (pas
        seulement ses propres actions) — c'est l'usage principal d'un audit
        trail pour un responsable d'entreprise cliente.
      - Les autres rôles (UTILISATEUR_ORGANISATION, CONSULTANT) ne voient que
        leurs propres actions, jamais celles des autres membres de leur
        organisation (confidentialité entre collègues).
    """

    queryset = JournalAudit.objects.select_related("utilisateur", "organisation").order_by("-date_action")
    serializer_class = JournalAuditSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        role = getattr(user, "role", None)

        if role == "SUPER_ADMIN":
            return self.queryset

        organisations_administrees = Organisation.objects.filter(
            membres__utilisateur=user
        )
        if role == "ADMIN_ORGANISATION":
            return self.queryset.filter(organisation__in=organisations_administrees)

        return self.queryset.filter(utilisateur=user)