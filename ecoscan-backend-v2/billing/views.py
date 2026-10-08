from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.db import transaction

from audit.services import enregistrer_evenement
from organizations.models import Organisation
from .models import Abonnement, Facture, Paiement, Plan, Relance
from .permissions import EstAdminDeLOrganisationAbonnee, EstAppelDeConfianceN8N, EstSuperAdminEcoScan
from .providers import PaymentProviderError, obtenir_provider
from .serializers import AbonnementSerializer, FactureSerializer, PaiementSerializer, PlanSerializer, RelanceSerializer
from .services import BillingAccessService, InvoiceService, SubscriptionService

# États depuis lesquels il n'est plus légitime d'initier un nouveau paiement
# ou de redemander une annulation — évite d'ouvrir une session de paiement
# pour un abonnement déjà mort.
ETATS_ABONNEMENT_CLOS = (Abonnement.Statut.CANCELED, Abonnement.Statut.SUSPENDED)


class PlanViewSet(viewsets.ModelViewSet):
    """CRUD complet, mais réservé au SUPER_ADMIN — pas de filtrage multi-tenant
    ici, le catalogue est le même pour toutes les organisations."""

    queryset = Plan.objects.all().order_by("nom")
    serializer_class = PlanSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsAuthenticated(), EstSuperAdminEcoScan()]

    def get_queryset(self):
        if self.request.user.is_authenticated and getattr(self.request.user, "role", None) == "SUPER_ADMIN":
            return self.queryset
        return self.queryset.filter(actif=True)


class BillingScopedQuerySetMixin:
    """Même principe que dans analysis/audit : filtrage par organisation
    d'appartenance, SUPER_ADMIN exclu des données de facturation des tenants
    (contrairement à l'audit, la facturation reste une donnée privée de
    l'organisation — pas un besoin de conformité plateforme)."""

    permission_classes = [IsAuthenticated]
    organisation_lookup = "organisation"

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "role", None) == "SUPER_ADMIN":
            return self.queryset
        organisations = Organisation.objects.filter(membres__utilisateur=user)
        return self.queryset.filter(**{f"{self.organisation_lookup}__in": organisations})


class AbonnementViewSet(BillingScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Abonnement.objects.select_related("organisation", "plan").order_by("-debut")
    serializer_class = AbonnementSerializer

    def get_permissions(self):
        # souscrire/annuler engagent de l'argent réel pour l'organisation :
        # réservé à son ADMIN_ORGANISATION (voir permissions.py).
        if self.action in ("souscrire", "annuler", "demarrer_essai", "souscrire_plan", "confirmer_retour"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def _organisation_administrable(self, request):
        if getattr(request.user, "role", None) != "ADMIN_ORGANISATION":
            return None
        return Organisation.objects.filter(
            membres__utilisateur=request.user,
            statut=Organisation.Statut.ACTIVE,
        ).order_by("date_creation").first()

    @action(detail=False, methods=["post"], url_path="essai")
    def demarrer_essai(self, request):
        """Démarre l'essai unique des organisations existantes sans abonnement."""
        organisation = self._organisation_administrable(request)
        if organisation is None:
            return Response(
                {"detail": "Un administrateur rattaché à une organisation active est requis."},
                status=status.HTTP_403_FORBIDDEN,
            )
        plan_id = request.data.get("plan_id")
        plan = Plan.objects.filter(pk=plan_id, actif=True).first()
        if plan is None:
            return Response({"plan_id": "Choisissez une formule active."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            organisation = Organisation.objects.select_for_update().get(pk=organisation.pk)
            if organisation.abonnements.exists():
                return Response(
                    {"detail": "Un essai gratuit ne peut être démarré qu'une seule fois. Souscrivez à une formule."},
                    status=status.HTTP_409_CONFLICT,
                )
            abonnement = SubscriptionService().demarrer_essai_gratuit(organisation, plan)

        enregistrer_evenement(
            action="DEMARRER_ESSAI_GRATUIT",
            ressource="Abonnement",
            identifiant_ressource=str(abonnement.id),
            utilisateur=request.user,
            organisation=organisation,
            request=request,
            details={"plan": plan.code, "duree_jours": 14},
        )
        return Response(self.get_serializer(abonnement).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="souscrire-plan")
    def souscrire_plan(self, request):
        """Ouvre le paiement d'une formule choisie, y compris sans abonnement antérieur."""
        organisation = self._organisation_administrable(request)
        if organisation is None:
            return Response(
                {"detail": "Un administrateur rattaché à une organisation active est requis."},
                status=status.HTTP_403_FORBIDDEN,
            )
        plan = Plan.objects.filter(pk=request.data.get("plan_id"), actif=True).first()
        if plan is None:
            return Response({"plan_id": "Choisissez une formule active."}, status=status.HTTP_400_BAD_REQUEST)
        periodicite = request.data.get("periodicite", "MENSUEL")
        if periodicite not in ("MENSUEL", "ANNUEL"):
            return Response({"periodicite": "Choisissez une périodicité valide."}, status=status.HTTP_400_BAD_REQUEST)
        if periodicite == "ANNUEL" and plan.prix_annuel is None:
            return Response({"periodicite": "Cette formule ne propose pas de tarif annuel."}, status=status.HTTP_400_BAD_REQUEST)

        maintenant = timezone.now()
        abonnement = Abonnement.objects.create(
            organisation=organisation,
            plan=plan,
            statut=Abonnement.Statut.EXPIRED,
            periodicite=periodicite,
            debut=maintenant,
            fin_periode=maintenant,
            fournisseur=Abonnement.Fournisseur.PAYDUNYA,
        )
        SubscriptionService()._historiser(
            abonnement,
            ancien_statut="",
            nouveau_statut=abonnement.statut,
            raison="souscription_en_attente_de_paiement",
        )
        facture = InvoiceService().generer_facture_cycle(abonnement)

        try:
            provider = obtenir_provider(abonnement.fournisseur)
            session = provider.creer_session_paiement(
                facture=facture,
                return_url=request.data.get("return_url", ""),
                cancel_url=request.data.get("cancel_url", ""),
            )
        except PaymentProviderError as exc:
            abonnement.delete()
            facture.delete()
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        facture.external_invoice_id = session["external_invoice_id"]
        facture.save(update_fields=("external_invoice_id",))
        enregistrer_evenement(
            action="INITIER_PAIEMENT",
            ressource="Facture",
            identifiant_ressource=str(facture.id),
            utilisateur=request.user,
            organisation=organisation,
            request=request,
            details={"fournisseur": abonnement.fournisseur, "montant": str(facture.montant_total)},
        )
        return Response(
            {
                "checkout_url": session["checkout_url"],
                "facture_id": str(facture.id),
                "abonnement_id": str(abonnement.id),
                "paydunya_invoice_token": facture.external_invoice_id,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="confirmer-retour")
    def confirmer_retour(self, request):
        """Reconfirme côté serveur le paiement après le retour PayDunya.

        Le token transmis dans l'URL de retour est seulement un identifiant :
        le statut et le montant proviennent exclusivement de l'API PayDunya.
        """
        token = str(request.data.get("token", "")).strip()
        if not token:
            return Response({"token": "Référence PayDunya manquante."}, status=status.HTTP_400_BAD_REQUEST)

        facture = Facture.objects.select_related(
            "organisation", "abonnement", "abonnement__plan",
        ).filter(
            external_invoice_id=token,
            organisation__membres__utilisateur=request.user,
        ).first()
        if facture is None:
            return Response({"detail": "Facture introuvable pour cette organisation."}, status=status.HTTP_404_NOT_FOUND)

        try:
            provider = obtenir_provider(facture.abonnement.fournisseur)
            evenement = provider.recuperer_transaction(token)
        except (PaymentProviderError, ValueError) as exc:
            return Response(
                {"detail": f"Impossible de vérifier le paiement auprès du prestataire : {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if (
            evenement.get("external_invoice_id") != token
            or str(evenement.get("facture_id") or "") != str(facture.id)
        ):
            return Response(
                {"detail": "La confirmation du prestataire ne correspond pas à cette facture."},
                status=status.HTTP_409_CONFLICT,
            )

        if evenement.get("statut") != "SUCCEEDED":
            return Response(
                {"status": evenement.get("statut", "PENDING").lower()},
                status=status.HTTP_200_OK,
            )

        from .webhooks import _traiter_paiement_reussi

        with transaction.atomic():
            traite = _traiter_paiement_reussi(evenement)
            facture.refresh_from_db(fields=("statut",))
            if not traite or facture.statut != Facture.Statut.PAID:
                return Response(
                    {"detail": "Le paiement a été reçu mais ne correspond pas au montant de la facture."},
                    status=status.HTTP_409_CONFLICT,
                )

        return Response({"status": "confirmed", "abonnement_id": str(facture.abonnement_id)})

    @action(detail=True, methods=["post"], url_path="souscrire")
    def souscrire(self, request, pk=None):
        """Crée la session de paiement chez le prestataire pour le cycle
        courant. N'active RIEN localement — l'activation vient uniquement du
        webhook (et de sa reconfirmation serveur-à-serveur, voir providers.py)
        après confirmation réelle du paiement."""
        abonnement = self.get_object()
        self.check_object_permissions(request, abonnement)

        if abonnement.statut in ETATS_ABONNEMENT_CLOS:
            return Response(
                {"error": f"Cet abonnement est {abonnement.get_statut_display()} — impossible d'initier un paiement."},
                status=status.HTTP_409_CONFLICT,
            )

        facture = InvoiceService().generer_facture_cycle(abonnement)

        try:
            provider = obtenir_provider(abonnement.fournisseur)
            session = provider.creer_session_paiement(
                facture=facture,
                return_url=request.data.get("return_url", ""),
                cancel_url=request.data.get("cancel_url", ""),
            )
        except PaymentProviderError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        facture.external_invoice_id = session["external_invoice_id"]
        facture.save(update_fields=("external_invoice_id",))

        enregistrer_evenement(
            action="INITIER_PAIEMENT", ressource="Facture", identifiant_ressource=str(facture.id),
            utilisateur=request.user, organisation=abonnement.organisation, request=request,
            details={"fournisseur": abonnement.fournisseur, "montant": str(facture.montant_total)},
        )
        return Response(
            {
                "checkout_url": session["checkout_url"],
                "facture_id": str(facture.id),
                "paydunya_invoice_token": facture.external_invoice_id,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def annuler(self, request, pk=None):
        abonnement = self.get_object()
        self.check_object_permissions(request, abonnement)

        if abonnement.statut in ETATS_ABONNEMENT_CLOS:
            return Response(
                {"error": f"Cet abonnement est déjà {abonnement.get_statut_display()}."},
                status=status.HTTP_409_CONFLICT,
            )

        immediat = bool(request.data.get("immediat", False))
        SubscriptionService().annuler(abonnement, immediat=immediat)
        enregistrer_evenement(
            action="ANNULER_ABONNEMENT", ressource="Abonnement", identifiant_ressource=str(abonnement.id),
            utilisateur=request.user, organisation=abonnement.organisation, request=request,
            details={"immediat": immediat},
        )
        return Response(self.get_serializer(abonnement).data, status=status.HTTP_200_OK)


class FactureViewSet(BillingScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Facture.objects.select_related("organisation", "abonnement", "abonnement__plan").order_by("-periode_debut")
    serializer_class = FactureSerializer


class PaiementViewSet(BillingScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Paiement.objects.select_related("organisation", "facture").order_by("-date_creation")
    serializer_class = PaiementSerializer


class RelanceViewSet(BillingScopedQuerySetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Relance.objects.select_related("facture__organisation").order_by("planifiee_le")
    organisation_lookup = "facture__organisation"
    serializer_class = RelanceSerializer


class RelanceCallbackView(APIView):
    """Callback appelé par le workflow n8n une fois l'envoi (email/SMS/WhatsApp)
    réellement effectué — voir tasks._envoyer_notification (déclenchement) et
    la conception du workflow n8n livrée séparément.

    Authentification par secret partagé (EstAppelDeConfianceN8N), pas par JWT :
    n8n n'est pas un utilisateur EcoScan."""

    permission_classes = [EstAppelDeConfianceN8N]

    def post(self, request, relance_id):
        try:
            relance = Relance.objects.select_related("facture__organisation").get(id=relance_id)
        except Relance.DoesNotExist:
            return Response({"error": "Relance introuvable."}, status=status.HTTP_404_NOT_FOUND)

        succes = bool(request.data.get("succes"))
        if relance.statut not in (Relance.Statut.PENDING, Relance.Statut.PROCESSING):
            # Déjà finalisée (double callback n8n, retry réseau) — idempotent.
            return Response({"status": "already_finalized"}, status=status.HTTP_200_OK)

        if succes:
            relance.statut = Relance.Statut.SENT
            relance.envoyee_le = timezone.now()
            relance.save(update_fields=("statut", "envoyee_le"))
        else:
            relance.statut = Relance.Statut.FAILED
            relance.save(update_fields=("statut",))

        enregistrer_evenement(
            action="RELANCE_TRAITEE_N8N", ressource="Relance", identifiant_ressource=str(relance.id),
            organisation=relance.facture.organisation, request=request,
            details={"canal": relance.canal, "succes": succes, "detail": request.data.get("detail", "")},
        )
        return Response({"status": "ok"}, status=status.HTTP_200_OK)