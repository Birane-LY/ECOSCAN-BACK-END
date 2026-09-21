from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from energy.models import AchatWoyofal, ReleveSolde

FENETRE_JOURS = 14


def estimer_autonomie(compteur) -> dict:
    maintenant = timezone.now()
    releves = list(
        ReleveSolde.objects.filter(compteur=compteur, date_releve__gte=maintenant - timedelta(days=FENETRE_JOURS))
        .order_by("date_releve")
    )
    if not releves:
        return {"jours_restants": None, "raison": "Aucun relevé de solde : saisissez le solde affiché par votre compteur."}

    dernier, premier = releves[-1], releves[0]
    base = {"solde_kwh": float(dernier.kwh_restants), "date_dernier_releve": dernier.date_releve.isoformat()}

    if len(releves) < 2:
        return {**base, "jours_restants": None,
                "raison": "Un seul relevé : saisissez le solde à nouveau demain pour estimer votre consommation."}

    duree_j = Decimal(str((dernier.date_releve - premier.date_releve).total_seconds() / 86400))
    if duree_j < 1:
        return {**base, "jours_restants": None, "raison": "Relevés trop rapprochés (moins de 24 h)."}

    recharges = AchatWoyofal.objects.filter(
        compteur=compteur, date_achat__gt=premier.date_releve, date_achat__lte=dernier.date_releve
    ).aggregate(t=Sum("kwh_credites"))["t"] or Decimal("0")

    conso = premier.kwh_restants + recharges - dernier.kwh_restants
    if conso <= 0:
        return {**base, "jours_restants": None,
                "raison": "Consommation non mesurable : vérifiez vos relevés et que toutes vos recharges sont enregistrées."}

    taux = conso / duree_j                       # kWh / jour
    autonomie_au_releve = dernier.kwh_restants / taux
    ecoule = Decimal(str((maintenant - dernier.date_releve).total_seconds() / 86400))
    jours = max(Decimal("0"), autonomie_au_releve - ecoule)

    return {
        **base,
        "jours_restants": float(round(jours, 1)),
        "conso_kwh_par_jour": float(round(taux, 2)),
        "date_epuisement_estimee": (maintenant + timedelta(days=float(jours))).isoformat(),
        # Honnêteté : peu de relevés ou fenêtre courte = estimation fragile
        "fiabilite": "moyenne" if (len(releves) >= 3 and duree_j >= 3) else "faible",
        "releve_ancien": ecoule > 3,
    }