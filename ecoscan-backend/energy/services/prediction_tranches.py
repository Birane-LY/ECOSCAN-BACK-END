from datetime import date
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from energy.models import AchatWoyofal, BaremeTarifaire

CATEGORIE_DEFAUT = "DOMESTIQUE_PETITE_PUISSANCE"


def bareme_actuel(categorie: str = CATEGORIE_DEFAUT, a_la_date: Optional[date] = None):
    a_la_date = a_la_date or date.today()
    return list(
        BaremeTarifaire.objects.filter(categorie=categorie, date_entree_vigueur__lte=a_la_date)
        .filter(models.Q(date_fin_vigueur__isnull=True) | models.Q(date_fin_vigueur__gte=a_la_date))
        .order_by("ordre")
    )


def cumul_kwh_mois(compteur) -> Decimal:
    debut = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = AchatWoyofal.objects.filter(compteur=compteur, date_achat__gte=debut) \
        .aggregate(t=Sum("kwh_credites"))["t"]
    return total or Decimal("0")


def _montant_net(montant: Decimal) -> Decimal:
    pct = Decimal(str(getattr(settings, "WOYOFAL_PRELEVEMENTS_PCT", "0")))
    return montant * (Decimal("1") - pct / Decimal("100"))


def etat_tranche(compteur) -> dict:
    bareme = bareme_actuel()
    cumul = cumul_kwh_mois(compteur)
    if not bareme:
        return {"erreur": "Aucun barème configuré.", "cumul_kwh": float(cumul)}
    for p in bareme:
        if p.kwh_max is None or cumul < p.kwh_max:
            return {
                "cumul_kwh": float(cumul),
                "tranche": p.nom_tranche,
                "prix_fcfa_par_kwh": float(p.prix_fcfa_par_kwh),
                "kwh_restants_dans_tranche": float(p.kwh_max - cumul) if p.kwh_max else None,
                "prochain_prix": next((float(q.prix_fcfa_par_kwh) for q in bareme if q.ordre > p.ordre), None),
            }


def predire_kwh(compteur, montant_fcfa: Decimal) -> dict:
    bareme = bareme_actuel()
    if not bareme:
        return {"kwh_estimes": None, "detail_paliers": [], "erreur": "Aucun barème configuré."}

    cumul_avant = cumul_kwh_mois(compteur)
    reste = _montant_net(Decimal(str(montant_fcfa)))
    cumul = cumul_avant
    total, detail = Decimal("0"), []

    for p in bareme:
        if reste <= 0:
            break
        if p.kwh_max is not None and cumul >= p.kwh_max:
            continue
        dispo = (p.kwh_max - cumul) if p.kwh_max is not None else None
        cout_max = dispo * p.prix_fcfa_par_kwh if dispo is not None else None
        if cout_max is not None and reste >= cout_max:
            kwh, cout = dispo, cout_max
        else:
            kwh, cout = reste / p.prix_fcfa_par_kwh, reste
        detail.append({"tranche": p.nom_tranche, "kwh": float(round(kwh, 3)),
                       "prix_fcfa_par_kwh": float(p.prix_fcfa_par_kwh), "cout_fcfa": float(round(cout, 2))})
        total += kwh
        cumul += kwh
        reste -= cout

    reco = None
    if len(detail) > 1:
        reco = (f"Cet achat franchit {len(detail) - 1} seuil(s) : une partie sera facturée à "
                f"{detail[-1]['prix_fcfa_par_kwh']} FCFA/kWh au lieu de {detail[0]['prix_fcfa_par_kwh']}.")

    return {"cumul_mensuel_avant_kwh": float(cumul_avant), "kwh_estimes": float(round(total, 3)),
            "detail_paliers": detail, "recommandation": reco,
            "prelevements_pct": float(getattr(settings, "WOYOFAL_PRELEVEMENTS_PCT", 0))}