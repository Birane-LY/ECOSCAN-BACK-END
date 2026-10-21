"""
Endpoint INTERNE appelé par le service FastAPI (django_client.get_live_context).

FastAPI appelait /api/internal/organisations/{id}/ai-context/ mais cette route
n'existait pas : l'assistant recevait toujours « Données temps réel
indisponibles ». Cette vue renvoie ce que l'IA doit connaître de l'organisation :
métriques, anomalies, hypothèses, objectifs, recommandations, mémoires, compteurs.

Sécurité : jeton de service partagé (settings.DJANGO_INTERNAL_TOKEN, identique à
DJANGO_INTERNAL_TOKEN côté FastAPI), jamais l'authentification utilisateur.
"""

import logging
import secrets
from datetime import date

from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from organizations.models import Compteur, Organisation

logger = logging.getLogger(__name__)

LIMITE = 10


def _monitoring_sites(organisation):
    from energy_assets.monitoring import obtenir_indicateurs_site

    sites = []
    for site in organisation.sites.order_by("nom")[:LIMITE]:
        summary, _ = obtenir_indicateurs_site(site)
        sites.append({
            "site": summary["site_name"],
            "mode_donnees": summary["telemetry_mode"],
            "puissance_actuelle_kw": summary["current_power_kw"],
            "energie_aujourdhui_kwh": summary["today_energy_kwh"],
            "derniere_mesure": summary["latest_measurement_at"],
            "equipements": [
                {
                    "nom": item["name"],
                    "zone": item["zone_name"],
                    "etat_rapporte": item["reported_state"],
                    "en_ligne": item["is_online"],
                    "puissance_actuelle_kw": item["power_kw"],
                    "mode_telemetrie": item["telemetry_mode"],
                }
                for item in summary["equipment"][:30]
            ],
        })
    return sites


def _section(avertissements: list, nom: str, fabrique):
    """Une section défaillante (champ renommé, table vide...) ne doit pas priver
    l'IA de toutes les autres : on la remplace par [] et on le signale."""
    try:
        return fabrique()
    except Exception as exc:
        logger.exception("ai-context : section '%s' indisponible.", nom)
        avertissements.append(f"{nom}: {type(exc).__name__}")
        return []


class AIContextView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, organisation_id):
        attendu = getattr(settings, "DJANGO_INTERNAL_TOKEN", "") or ""
        recu = request.headers.get("X-Internal-Service-Token", "")
        if not attendu or not secrets.compare_digest(recu, attendu):
            return Response({"error": "Jeton de service invalide."}, status=401)

        organisation = Organisation.objects.filter(id=organisation_id).first()
        if organisation is None:
            return Response({"error": "Organisation introuvable."}, status=404)

        from energy.models import AchatWoyofal, DonneeEnergetique, Objectif, ReleveSolde
        from energy.services.autonomie import estimer_autonomie
        from energy.services.prediction_tranches import etat_tranche
        from .models import Action, Anomalie, Hypothese, MemoireStrategique, Recommandation, ResultatMetrique
        from .services.progression_objectifs import calculer_progressions_organisation

        av: list = []
        data = {
            "date_du_jour": date.today().isoformat(),
            "organisation": getattr(organisation, "nom", str(organisation)),
        }

        data["metriques_recentes"] = _section(av, "metriques", lambda: list(
            ResultatMetrique.objects.filter(organisation=organisation).order_by("-periode_fin")[:LIMITE]
            .values("code_metrique", "valeur", "unite", "periode_fin", "statut_qualite")))
        data["monitoring_sites"] = _section(
            av,
            "monitoring_sites",
            lambda: _monitoring_sites(organisation),
        )
        data["anomalies"] = _section(av, "anomalies", lambda: list(
            Anomalie.objects.filter(organisation=organisation).order_by("-date_detection")[:LIMITE]
            .values("type", "severite", "ecart_pourcentage", "valeur_observee", "valeur_attendue", "statut", "date_detection")))
        data["hypotheses_en_attente"] = _section(av, "hypotheses", lambda: list(
            Hypothese.objects.filter(anomalie__organisation=organisation, statut="PROPOSEE")
            .order_by("-date_creation")[:LIMITE].values("texte", "statut", "date_creation")))
        data["objectifs"] = _section(av, "objectifs", lambda: list(
            Objectif.objects.filter(organisation=organisation)[:LIMITE]
            .values("nom", "type", "valeur_cible", "progression_actuelle", "prevision", "unite", "statut", "date_debut", "date_fin")))
        data["recommandations"] = _section(av, "recommandations", lambda: list(
            Recommandation.objects.filter(objectif__organisation=organisation)[:LIMITE]
            .values("titre", "description", "statut", "economie_estimee", "unite", "priorite", "anomalie_id")))
        data["actions"] = _section(av, "actions", lambda: list(
            Action.objects.filter(recommandation__objectif__organisation=organisation).order_by("date_echeance")[:LIMITE]
            .values("titre", "statut", "date_echeance", "economie_realisee_fcfa", "taux_realisation_impact")))
        # « declaree » = économies estimées des recommandations décidées ; « mesuree » =
        # calculée sur les données réelles. L'IA ne doit jamais les confondre.
        data["progression_objectifs"] = _section(av, "progression_objectifs",
                                                 lambda: calculer_progressions_organisation(organisation))
        data["memoires_strategiques"] = _section(av, "memoires", lambda: list(
            MemoireStrategique.objects.filter(organisation=organisation).order_by("-date_creation")[:LIMITE]
            .values("titre", "statut", "signal_initial", "hypothese_texte", "impact_attendu_fcfa", "impact_mesure_fcfa")))
        def _releves_recents():
            resultat = []
            compteurs = (
                Compteur.objects.filter(site__organisation=organisation)
                .select_related("site")
                .order_by("site__nom", "reference")[:10]
            )
            for compteur in compteurs:
                releves = (
                    DonneeEnergetique.objects.filter(compteur=compteur)
                    .order_by("-periode_fin")[:5]
                    .values(
                        "valeur",
                        "unite",
                        "creneau",
                        "date_releve",
                        "periode_fin",
                        "statut_validation",
                    )
                )
                resultat.extend(
                    {
                        **releve,
                        "compteur_id": str(compteur.id),
                        "compteur": compteur.reference,
                        "site": compteur.site.nom,
                    }
                    for releve in releves
                )
            return resultat

        data["releves_recents"] = _section(av, "releves", _releves_recents)
        data["releves_solde"] = _section(av, "releves_solde", lambda: list(
            ReleveSolde.objects.filter(compteur__site__organisation=organisation)
            .order_by("-date_releve")[:LIMITE].values("kwh_restants", "date_releve")))
        data["achats_woyofal"] = _section(av, "achats_woyofal", lambda: list(
            AchatWoyofal.objects.filter(compteur__site__organisation=organisation)
            .order_by("-date_achat")[:LIMITE].values("date_achat", "kwh_credites", "montant_fcfa")))

        def _compteurs():
            resultat = []
            for compteur in Compteur.objects.filter(site__organisation=organisation)[:3]:
                resultat.append({
                    "reference": compteur.reference,
                    "autonomie": estimer_autonomie(compteur),
                    "tranche": etat_tranche(compteur),
                })
            return resultat
        data["compteurs"] = _section(av, "compteurs", _compteurs)

        def _creneaux():
            """Consommation par créneau du jour (09h/13h/17h...) : ce qui permet à
            l'assistant de répondre à « compare mes créneaux du jour »."""
            from django.utils import timezone
            from .services.metrics_service import MetricsService

            resultat = []
            for compteur in Compteur.objects.filter(site__organisation=organisation)[:3]:
                try:
                    m = MetricsService(organisation).calculer_consommation_par_creneau(compteur, timezone.now())
                    resultat.append({
                        "compteur": compteur.reference,
                        "creneaux": m["value"],
                        "qualite": m["data_quality"]["status"],
                        "message": m.get("message"),
                    })
                except Exception as exc:  # ex. index incohérent : signalé, pas masqué
                    resultat.append({"compteur": compteur.reference, "erreur": type(exc).__name__})
            return resultat
        data["creneaux_du_jour"] = _section(av, "creneaux_du_jour", _creneaux)

        if av:
            data["_avertissements"] = av
        return Response(data)
