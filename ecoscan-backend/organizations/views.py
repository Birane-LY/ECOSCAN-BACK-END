from django.conf import settings
from django.http import JsonResponse
from django.db import transaction
from rest_framework import viewsets, permissions, status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
import logging
from .models import (
    Organisation,
    UtilisateurOrganisation,
    Site,
    FicheProjet,
    Activite,
    Compteur,
    ConfigurationSecurite,
)
from energy.models import DonneeEnergetique, Objectif
from analysis.models import Anomalie, Recommandation, MemoireStrategique
from accounts.models import Utilisateur
from accounts.services import envoyer_email_activation
from billing.services import BillingAccessService

from .serializers import (
    OrganisationSerializer,
    UtilisateurOrganisationSerializer,
    SiteSerializer,
    FicheProjetSerializer,
    ActiviteSerializer,
    CompteurSerializer,
    ConfigurationSecuriteSerializer,
)
from billing.permissions import EstAbonnementActif

logger = logging.getLogger(__name__)


class EmailActivationDeliveryError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = (
        "L’e-mail d’activation n’a pas pu être envoyé. "
        "La demande reste en attente ; réessayez après vérification du service e-mail."
    )


class EstMembreDeLOrganisation(permissions.BasePermission):
    """Permission globale appliquant le principe de cloisonnement strict par entreprise.

    L'accès est accordé uniquement si l'utilisateur est explicitement affilié à la structure.
    Le Super Admin ne peut pas contourner cette règle sur les données métiers des clients.
    """

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.actif
            and BillingAccessService().organisations_avec_acces(request.user).exists()
        )

    def has_object_permission(self, request, view, obj):
        # Détermination dynamique de l'organisation selon l'objet ciblé
        if isinstance(obj, Organisation):
            organisation = obj
        elif hasattr(obj, "organisation"):
            organisation = obj.organisation
        elif isinstance(obj, Activite):
            organisation = obj.fiche_projet.organisation
        elif isinstance(obj, Compteur):
            organisation = obj.site.organisation
        else:
            return False

        # Le Super Admin doit lui aussi être affilié à l'organisation 
        # pour pouvoir consulter ou interagir avec ses objets métiers (Privacy by Design).
        return BillingAccessService().organisations_avec_acces(request.user).filter(
            pk=organisation.pk,
            membres__utilisateur=request.user,
        ).exists()


class PeutGererOrganisation(permissions.BasePermission):
    """Permission dédiée à OrganisationViewSet — l'objet Organisation lui-même
    (nom, secteur, statut, défaut de paiement), pas les objets métier qu'elle
    contient.

    Contrairement à EstMembreDeLOrganisation, le Super Admin PEUT accéder à
    n'importe quelle organisation ici : valider un signup, suspendre un
    compte impayé, changer un statut, est le rôle même de la console
    plateforme. C'est distinct de l'accès aux données métier privées des
    clients (sites, compteurs, fiches projet) qui reste interdit au Super
    Admin sans affiliation, comme partout ailleurs dans ce module.
    """

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and request.user.actif

    def has_object_permission(self, request, view, obj):
        if getattr(request.user, "role", None) == "SUPER_ADMIN":
            return True
        return BillingAccessService().organisations_avec_acces(request.user).filter(
            pk=obj.pk,
            membres__utilisateur=request.user,
        ).exists()


class OrganisationScopedViewSet(viewsets.ModelViewSet):
    """Classe de base abstraite appliquant l'aveuglement par défaut.

    Tous les rôles (y compris Super Admin) ne voient en base de données 
    que les lignes des organisations auxquelles ils sont rattachés.
    """
    permission_classes = [EstMembreDeLOrganisation]
    organisation_field = "organisation"

    def get_queryset(self):
        queryset = self.queryset
        user = self.request.user

        # Récupération de la liste des organisations de l'utilisateur connecté
        organisations = BillingAccessService().organisations_avec_acces(user)

        # Application dynamique du filtre selon la profondeur de la relation dans le modèle
        if self.organisation_field == "organisation":
            return queryset.filter(organisation__in=organisations)
            
        return queryset.filter(**{f"{self.organisation_field}__organisation__in": organisations})


class OrganisationViewSet(viewsets.ModelViewSet):
    """Contrôleur gérant le cycle de vie des organisations clientes.

    - Le Super Admin liste, consulte et modifie UNIQUEMENT le statut/défaut de
      paiement (automatisable par n8n) de N'IMPORTE QUELLE organisation — c'est
      la console plateforme, pas un accès aux données métier des clients.
    - La suppression physique est formellement interdite.
    """
    queryset = Organisation.objects.all().order_by("nom")
    serializer_class = OrganisationSerializer
    permission_classes = [PeutGererOrganisation]

    def get_queryset(self):
        # Le Super Admin conserve le droit de lister toutes les structures de la plateforme
        if self.request.user.role == "SUPER_ADMIN":
            queryset = self.queryset
            # Filtres optionnels pour le polling n8n (ex. GET .../?defaut_paiement=true
            # pour ne récupérer que les organisations à relancer).
            statut = self.request.query_params.get("statut")
            if statut:
                queryset = queryset.filter(statut=statut)
            defaut_paiement = self.request.query_params.get("defaut_paiement")
            if defaut_paiement is not None:
                queryset = queryset.filter(defaut_paiement=defaut_paiement.lower() in ("true", "1"))
            return queryset
        return self.queryset.filter(
            pk__in=BillingAccessService()
            .organisations_avec_acces(self.request.user)
            .values("pk")
        )

    def perform_create(self, serializer):
        """Réservé aux ADMIN_ORGANISATION — un UTILISATEUR_ORGANISATION/CONSULTANT
        est censé être invité dans une organisation existante, jamais en créer
        une lui-même. La création de l'Organisation ET le lien
        UtilisateurOrganisation se font dans la même transaction : sans ça,
        un échec entre les deux étapes laisserait une organisation orpheline
        qu'aucun utilisateur ne pourrait jamais retrouver ni gérer."""
        if getattr(self.request.user, "role", None) != "ADMIN_ORGANISATION":
            raise PermissionDenied("Seul un administrateur d'organisation peut créer une nouvelle structure.")

        with transaction.atomic(): # type: ignore
            organisation = serializer.save()
            UtilisateurOrganisation.objects.create(organisation=organisation, utilisateur=self.request.user)

    def update(self, request, *args, **kwargs):
        """Restreint les modifications d'accès aux seules contraintes financières (Abonnement)."""
        acteur = request.user
        instance = self.get_object()

        # SÉCURITÉ : Un script (n8n ou autre) qui a besoin de cette route doit 
        # s'authentifier avec un compte de service ayant réellement le rôle SUPER_ADMIN.
        if acteur.role == "SUPER_ADMIN":
            champs_autorises = {"statut", "defaut_paiement"}
            champs_interdits = set(request.data.keys()) - champs_autorises
            if champs_interdits:
                raise PermissionDenied(
                    "Le Super Admin ne peut pas modifier les informations d'une organisation."
                )

            nouveau_statut = request.data.get("statut")
            nouveau_defaut = request.data.get("defaut_paiement")
            email_activation_envoye = False

            # 1. Traitement du flag de paiement envoyé par n8n ou le Super Admin
            if nouveau_defaut is not None:
                instance.defaut_paiement = nouveau_defaut

            # 2. Traitement du changement de statut (Suspension / Réactivation)
            if nouveau_statut and instance.statut != nouveau_statut:
                if nouveau_statut not in Organisation.Statut.values:
                    raise ValidationError({"statut": "Statut d’organisation invalide."})

                demande_en_attente = instance.statut == Organisation.Statut.EN_ATTENTE
                if demande_en_attente and nouveau_statut != Organisation.Statut.ACTIVE:
                    raise PermissionDenied("Une demande en attente ne peut être qu’approuvée ou laissée en attente.")
                if (
                    demande_en_attente
                    and nouveau_statut == Organisation.Statut.ACTIVE
                    and instance.details_demande.get("inscription_autonome")
                ):
                    raise ValidationError(
                        {"statut": "Le compte démarre son essai après activation du lien envoyé par e-mail."}
                    )
                if demande_en_attente and nouveau_statut == Organisation.Statut.ACTIVE:
                    from billing.models import Plan
                    plan_id_demande = instance.details_demande.get("plan_id")
                    plan_configure = (
                        Plan.objects.filter(pk=plan_id_demande, actif=True).exists()
                        if plan_id_demande
                        else Plan.objects.filter(code="standard", actif=True).exists()
                    )
                    if not plan_configure:
                        raise ValidationError({
                            "plan": "La formule demandée n’est plus active ou le plan d’essai standard n’est pas configuré."
                        })
                
                # Tentative de Suspension : Impossible si le client est à jour
                if nouveau_statut == Organisation.Statut.SUSPENDUE:
                    if not instance.defaut_paiement and not nouveau_defaut:
                        raise PermissionDenied(
                            "Action refusée. L'organisation est à jour dans ses paiements."
                        )
                
                # Tentative de Réactivation : Impossible si le défaut n'est pas résolu
                elif nouveau_statut == Organisation.Statut.ACTIVE and instance.statut == Organisation.Statut.SUSPENDUE:
                    if instance.defaut_paiement:
                        raise PermissionDenied(
                            "Action refusée. Le défaut de paiement doit d'abord être régularisé."
                        )

                instance.statut = nouveau_statut

                if demande_en_attente and nouveau_statut == Organisation.Statut.ACTIVE:
                    admins = Utilisateur.objects.filter(
                        organisations_membres__organisation=instance,
                        role=Utilisateur.Role.ADMIN_ORGANISATION,
                        actif=False,
                    ).distinct()
                    for admin in admins:
                        try:
                            envoyer_email_activation(
                                admin,
                                sujet="Votre demande EcoScan est approuvée",
                                introduction=(
                                    "Votre demande d’accès à EcoScan a été approuvée. "
                                    "Votre compte administrateur est prêt à être activé."
                                ),
                            )
                            email_activation_envoye = True
                        except Exception as exc:
                            raise EmailActivationDeliveryError() from exc

                # Do not hold SQLite's write lock while waiting for the SMTP server.
                with transaction.atomic():
                    instance.save()

            else:
                instance.save()

            serializer = self.get_serializer(instance)
            data = serializer.data
            data["activation_email_sent"] = email_activation_envoye
            return Response(data)

        # Un ADMIN_ORGANISATION membre de sa propre structure peut modifier ses
        # informations descriptives (nom, secteur, localisation), jamais son statut.
        est_membre = UtilisateurOrganisation.objects.filter(
            organisation=instance, utilisateur=acteur
        ).exists()
        if not est_membre:
            raise PermissionDenied("Vous n'avez pas l'autorisation de modifier les données de ce client.")

        for champ in ("nom", "secteur", "localisation"):
            if champ in request.data:
                setattr(instance, champ, request.data[champ])
        instance.save()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def perform_destroy(self, instance):
        """Interdit la suppression définitive d'une organisation en production."""
        raise PermissionDenied(
            "La suppression d'une organisation est interdite pour préserver l'historique des données. "
            "Veuillez suspendre ou archiver son accès."
        )


class UtilisateurOrganisationViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les affiliations et liaisons des membres à leurs organisations."""
    queryset = UtilisateurOrganisation.objects.select_related("organisation", "utilisateur")
    serializer_class = UtilisateurOrganisationSerializer


class SiteViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les sites physiques de manière hermétique par organisation."""
    queryset = Site.objects.select_related("organisation").order_by("nom")
    serializer_class = SiteSerializer


class FicheProjetViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les fiches projets et les audits de l'organisation."""
    queryset = FicheProjet.objects.select_related("organisation").order_by("nom")
    serializer_class = FicheProjetSerializer


class ActiviteViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les activités opérationnelles découlant des fiches projets."""
    queryset = Activite.objects.select_related("fiche_projet__organisation").order_by("nom")
    serializer_class = ActiviteSerializer
    organisation_field = "fiche_projet"


class CompteurViewSet(OrganisationScopedViewSet):
    """Contrôleur gérant les équipements de mesure énergétique rattachés aux sites."""
    queryset = Compteur.objects.select_related("site__organisation").prefetch_related("activites").order_by("reference")
    serializer_class = CompteurSerializer
    organisation_field = "site"


class ConfigurationSecuriteView(APIView):
    """GET accessible à tout membre de l'organisation (pour savoir si le 2FA
    est exigé) ; PATCH réservé aux admins."""
    permission_classes = [permissions.IsAuthenticated, EstAbonnementActif]

    def get(self, request):
        organisation = BillingAccessService().organisations_avec_acces(request.user).first()
        if organisation is None:
            return Response({"error": "Aucune organisation associée."}, status=status.HTTP_409_CONFLICT)
        config, _ = ConfigurationSecurite.objects.get_or_create(organisation=organisation)
        return Response(ConfigurationSecuriteSerializer(config).data)

    def patch(self, request):
        organisation = BillingAccessService().organisations_avec_acces(request.user).first()
        if organisation is None:
            return Response({"error": "Aucune organisation associée."}, status=status.HTTP_409_CONFLICT)

        if getattr(request.user, "role", None) != "ADMIN_ORGANISATION":
            return Response({"error": "Seul un administrateur peut modifier cette politique."}, status=status.HTTP_403_FORBIDDEN)

        config, _ = ConfigurationSecurite.objects.get_or_create(organisation=organisation)
        serializer = ConfigurationSecuriteSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class OnboardingSimpleView(APIView):
    """Organisation + site + compteur en une seule transaction (mobile)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        if getattr(user, "role", None) != "ADMIN_ORGANISATION":
            return Response({"error": "Réservé aux administrateurs d'organisation."}, status=403)
        if Organisation.objects.filter(membres__utilisateur=user).exists():
            return Response({"error": "Vous avez déjà une organisation."}, status=409)

        d = request.data
        reference = (d.get("reference_compteur") or "").strip()
        if not reference:
            return Response({"error": "Le numéro du compteur est requis."}, status=400)

        with transaction.atomic():
            org = Organisation.objects.create(
                nom=(d.get("nom") or "").strip() or "Mon activité",
                secteur="Commerce", localisation="Sénégal",
                statut=Organisation.Statut.EN_ATTENTE, type_compte="SIMPLIFIE",
            )
            UtilisateurOrganisation.objects.create(organisation=org, utilisateur=user)
            site = Site.objects.create(
                organisation=org, nom="Site principal",
                adresse=(d.get("adresse") or "").strip() or "À préciser",
                pays="Sénégal", fuseau_horaire="Africa/Dakar",
            )
            compteur = Compteur.objects.create(
                site=site, reference=reference, type_energie="ELECTRICITE",
                unite="kWh", statut_synchronisation="MANUEL",
            )
        return Response({"organisation": str(org.id), "site": str(site.id), "compteur": str(compteur.id)}, status=201)




class InternalAIContextView(APIView):
    """Endpoint interne appelé exclusivement par le service FastAPI Assistant AI.
    
    Transmet le contexte métier complet et les données de consommation récentes
    (notamment les créneaux horaires) en échange du token de service sécurisé.
    """
    authentication_classes = []  # Pas de JWT utilisateur
    permission_classes = []      # Sécurisé par token interne

    def get(self, request, organisation_id):
        token_attendu = getattr(settings, "DJANGO_INTERNAL_TOKEN", "")
        token_recu = request.headers.get("X-Internal-Service-Token")

        if not token_recu or token_recu != token_attendu:
            return JsonResponse({"detail": "Token d'authentification interne invalide."}, status=401)

        try:
            org = Organisation.objects.get(id=organisation_id)
        except (Organisation.DoesNotExist, ValueError):
            return JsonResponse({"detail": "Organisation introuvable."}, status=404)

        # Récupération des relevés de consommation récents avec leurs créneaux
        releves_creneaux = list(
            DonneeEnergetique.objects.filter(compteur__site__organisation=org)
            .order_by("-date_releve", "-periode_debut")[:30]
            .values("date_releve", "creneau", "valeur", "unite", "compteur__reference")
        )

        # Récupération des anomalies récentes
        anomalies = list(
            Anomalie.objects.filter(organisation=org)
            .order_by("-date_detection")[:5]
            .values("type", "severite", "ecart_pourcentage", "statut")
        )

        # Récupération des objectifs
        objectifs = list(
            Objectif.objects.filter(organisation=org)
            .order_by("-date_debut")[:5]
            .values("nom", "type", "valeur_cible", "unite", "progression_actuelle", "statut")
        )

        # Récupération des recommandations récentes
        recommandations = list(
            Recommandation.objects.filter(objectif__organisation=org)
            .order_by("-id")[:5]
            .values("titre", "description", "impact_estime", "economie_estimee", "unite", "priorite", "statut")
        )

        # Récupération des mémoires stratégiques
        memoires = list(
            MemoireStrategique.objects.filter(organisation=org)
            .order_by("-date_creation")[:5]
            .values("titre", "signal_initial", "hypothese_texte", "action_texte", "impact_attendu_fcfa", "impact_mesure_fcfa", "statut")
        )

        contexte_metier = {
            "organisation": {
                "id": str(org.id),
                "nom": org.nom,
                "secteur": org.secteur,
                "localisation": org.localisation,
            },
            "compteurs": [
                {
                    "id": str(c.id),
                    "reference": c.reference,
                    "site": c.site.nom,
                    "puissance_souscrite_kva": str(c.puissance_souscrite_kva) if c.puissance_souscrite_kva else None,
                }
                for site in org.sites.all()
                for c in site.compteurs.all()
            ],
            "donnees_creneaux_recents": [
                {
                    "date": str(r["date_releve"]),
                    "creneau": r["creneau"] or "Standard",
                    "consommation": float(r["valeur"]),
                    "unite": r["unite"],
                    "compteur": r["compteur__reference"],
                }
                for r in releves_creneaux
            ],
            "anomalies_recentes": anomalies,
            "objectifs": [
                {
                    "titre": o["nom"],
                    "type": o["type"],
                    "valeur_cible": float(o["valeur_cible"]) if o["valeur_cible"] else None,
                    "unite": o["unite"],
                    "progression": float(o["progression_actuelle"]) if o["progression_actuelle"] else 0,
                    "statut": o["statut"],
                }
                for o in objectifs
            ],
            "recommandations": [
                {
                    "titre": r["titre"],
                    "description": r["description"],
                    "impact_estime": float(r["impact_estime"]) if r["impact_estime"] else None,
                    "economie_estimee": float(r["economie_estimee"]) if r["economie_estimee"] else None,
                    "unite": r["unite"],
                    "priorite": r["priorite"],
                    "statut": r["statut"],
                }
                for r in recommandations
            ],
            "memoires_strategiques": [
                {
                    "titre": m["titre"],
                    "signal_initial": m["signal_initial"],
                    "hypothese_texte": m["hypothese_texte"],
                    "action_texte": m["action_texte"],
                    "impact_attendu_fcfa": float(m["impact_attendu_fcfa"]) if m["impact_attendu_fcfa"] else None,
                    "impact_mesure_fcfa": float(m["impact_mesure_fcfa"]) if m["impact_mesure_fcfa"] else None,
                    "statut": m["statut"],
                }
                for m in memoires
            ],
        }

        return JsonResponse(contexte_metier, status=200)