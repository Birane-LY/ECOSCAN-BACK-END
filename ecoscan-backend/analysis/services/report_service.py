"""
Génération du fichier PDF d'un Livrable.

Appelé depuis LivrableViewSet.generer() (views.py), PAS depuis Livrable.generer()
lui-même — ce dernier reste un simple changement d'état (statut + date), comme
Recommandation.generer() ou Action.suivre() : cohérent avec le reste du module,
où toute logique avec effet de bord/IO (appel IA, indexation RAG...) vit dans
services/, jamais sur le modèle (voir hypothesis.py, memory_service.py).

DÉPENDANCE À AJOUTER : ce module utilise reportlab (pur Python, aucune
dépendance système comme Pango/Cairo — contrairement à WeasyPrint — donc
portable sur n'importe quel hébergement sans configuration supplémentaire).
    pip install reportlab

CHARTE GRAPHIQUE : logo et 3 couleurs de marque (voir ECOSCAN_NAVY / TEAL /
ORANGE ci-dessous), extraites directement du logo fourni. Le logo est livré à
côté de ce fichier (services/assets/logo_ecoscan.png) ; surchargeable sans
toucher au code via settings.ECOSCAN_REPORT_LOGO_PATH si un autre logo doit
être utilisé plus tard (marque blanche, refonte...).

LIMITE ASSUMÉE : le modèle Livrable n'a pas de periode_debut/periode_fin —
seulement un `nom` libre (ex. "EcoScan_Rapport_Septembre 2026") qui encode la
période choisie par l'utilisateur sous forme de texte, jamais de façon
interrogeable. Ce rapport ne peut donc PAS filtrer précisément "les données de
la période demandée" : il présente les données les plus récentes disponibles
(bornées par MAX_JOURS_HISTORIQUE ci-dessous), et l'indique explicitement dans
le document plutôt que de laisser croire à un filtrage par période qui n'existe
pas. Si le filtrage par période réelle devient nécessaire, il faut ajouter
periode_debut/periode_fin à Livrable (migration) et les faire remonter depuis
ReportsTool.jsx jusqu'à la création du Livrable.
"""

import io
import logging
import os

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable,
)

from analysis.models import Anomalie, Livrable, Recommandation, ResultatMetrique

logger = logging.getLogger(__name__)

MAX_JOURS_HISTORIQUE = 90
MAX_LIGNES_PAR_SECTION = 15
ECOSCAN_NAVY = colors.HexColor("#0B2E6D")     # texte, titres, en-têtes de tableau
ECOSCAN_TEAL = colors.HexColor("#0E8596")     # filets, accents secondaires
ECOSCAN_ORANGE = colors.HexColor("#F5901F")   # mise en avant ponctuelle (chiffres clés, alertes)
ECOSCAN_GRIS_CLAIR = colors.HexColor("#F3F5F8")  # fond alterné des tableaux (neutre, pas une 4e couleur de marque)

CHEMIN_LOGO_DEFAUT = os.path.join(os.path.dirname(__file__), "assets", "logo_ecoscan.png")

TITRES_TEMPLATE = {
    "COMPTABLE": "Bilan énergétique — Rapport comptable",
    "BANQUE": "Dossier de financement — Synthèse énergétique",
    "INVESTISSEUR": "Business case — Opportunité d'investissement énergétique",
}

# Ce que chaque destinataire regarde en priorité — un comptable veut des
# chiffres vérifiables, une banque un dossier de financement, un investisseur
# un potentiel de gain. Le contenu de base (métriques + recommandations) est
# commun ; seules les sections additionnelles et le ton du résumé varient.
SECTIONS_PAR_TYPE = {
    "COMPTABLE": ("metriques", "anomalies"),
    "BANQUE": ("metriques", "recommandations", "anomalies"),
    "INVESTISSEUR": ("recommandations", "metriques"),
}


def _styles():
    base = getSampleStyleSheet()
    base.add(ParagraphStyle(name="EcoScanTitre", parent=base["Title"], fontSize=19, textColor=ECOSCAN_NAVY, spaceAfter=4, alignment=0))
    base.add(ParagraphStyle(name="EcoScanSousTitre", parent=base["Normal"], fontSize=10, textColor=colors.HexColor("#5B6B82"), spaceAfter=2))
    base.add(ParagraphStyle(name="EcoScanSection", parent=base["Heading2"], fontSize=13, textColor=ECOSCAN_NAVY, spaceBefore=18, spaceAfter=8))
    base.add(ParagraphStyle(name="EcoScanPied", parent=base["Normal"], fontSize=8, textColor=colors.HexColor("#94A3B8")))
    return base


def _entete(livrable, organisation, fiche_projet, styles):
    """Bandeau d'ouverture : logo, titre, et bloc d'identification du rapport
    (organisation, projet, date de génération, période couverte) — c'est ce
    bloc qui doit permettre d'identifier le document sans ambiguïté même
    imprimé seul, hors de tout contexte applicatif."""
    elements = []

    chemin_logo = getattr(settings, "ECOSCAN_REPORT_LOGO_PATH", CHEMIN_LOGO_DEFAUT)
    logo_flowable = None
    if chemin_logo and os.path.exists(chemin_logo):
        try:
            logo_flowable = Image(chemin_logo, width=2.4 * cm, height=2.4 * cm)
        except Exception:
            logger.warning("Logo introuvable ou illisible (%s) — en-tête généré sans logo.", chemin_logo)
    else:
        logger.warning("ECOSCAN_REPORT_LOGO_PATH introuvable (%s) — en-tête généré sans logo.", chemin_logo)

    titre_bloc = [Paragraph(TITRES_TEMPLATE.get(livrable.type, livrable.nom), styles["EcoScanTitre"])]
    titre_bloc.append(Paragraph("EcoScan — Pilotage énergétique", styles["EcoScanSousTitre"]))

    entete_table = Table(
        [[logo_flowable or "", titre_bloc]],
        colWidths=[3 * cm, None],
    )
    entete_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 10),
    ]))
    elements.append(entete_table)
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(HRFlowable(width="100%", thickness=1.4, color=ECOSCAN_TEAL, spaceAfter=10))

    # Bloc d'identification — organisation, projet, date, période : exactement
    # ce qui doit être renseigné pour qu'un rapport imprimé reste traçable.
    infos = [
        ["Organisation", organisation.nom if organisation else "—"],
        ["Fiche projet", fiche_projet.nom if fiche_projet else "—"],
        ["Date du rapport", f"{timezone.now():%d/%m/%Y à %H:%M}"],
        ["Période couverte", f"{MAX_JOURS_HISTORIQUE} derniers jours"],
    ]
    table_infos = Table(infos, colWidths=[4 * cm, 10.6 * cm])
    table_infos.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), ECOSCAN_NAVY),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1F2937")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E2E8F0")),
    ]))
    elements.append(table_infos)
    elements.append(Spacer(1, 0.4 * cm))
    return elements


def _style_tableau(couleur_entete=ECOSCAN_NAVY):
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), couleur_entete),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ECOSCAN_GRIS_CLAIR]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ])


def _table_metriques(metriques, styles):
    if not metriques:
        return [Paragraph("Aucune métrique fiable disponible sur la période récente.", styles["Normal"])]

    data = [["Métrique", "Valeur", "Période", "Qualité"]]
    for m in metriques[:MAX_LIGNES_PAR_SECTION]:
        data.append([
            m.code_metrique.replace("_", " ").capitalize(),
            f"{m.valeur} {m.unite}" if m.valeur is not None else "—",
            m.periode_fin.strftime("%d/%m/%Y"),
            m.get_statut_qualite_display(),
        ])
    table = Table(data, colWidths=[6 * cm, 3.5 * cm, 3.5 * cm, 3 * cm])
    table.setStyle(_style_tableau())
    return [table]


def _table_recommandations(recommandations, styles):
    if not recommandations:
        return [Paragraph("Aucune recommandation décidée pour le moment.", styles["Normal"])]

    data = [["Titre", "Économie estimée", "Priorité", "Statut"]]
    for r in recommandations[:MAX_LIGNES_PAR_SECTION]:
        data.append([r.titre, f"{r.economie_estimee} {r.unite}", r.get_priorite_display(), r.get_statut_display()])
    table = Table(data, colWidths=[7 * cm, 4 * cm, 2.5 * cm, 2.5 * cm])
    style = _style_tableau()
    style.add("TEXTCOLOR", (1, 1), (1, -1), ECOSCAN_ORANGE)
    style.add("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold")
    table.setStyle(style)
    return [table]


def _table_anomalies(anomalies, styles):
    if not anomalies:
        return [Paragraph("Aucune anomalie détectée sur la période récente.", styles["Normal"])]

    data = [["Type", "Sévérité", "Écart", "Détectée le"]]
    for a in anomalies[:MAX_LIGNES_PAR_SECTION]:
        data.append([
            a.type.replace("_", " ").capitalize(),
            a.get_severite_display(),
            f"{a.ecart_pourcentage} %",
            a.date_detection.strftime("%d/%m/%Y"),
        ])
    table = Table(data, colWidths=[6 * cm, 4 * cm, 3 * cm, 3 * cm])
    table.setStyle(_style_tableau())
    return [table]


SECTION_BUILDERS = {
    "metriques": ("Indicateurs de performance récents", _table_metriques),
    "recommandations": ("Recommandations décidées", _table_recommandations),
    "anomalies": ("Anomalies détectées", _table_anomalies),
}


def _pied_de_page(canvas, doc):
    """Filet et mention en pied de chaque page — discret, dans la charte."""
    canvas.saveState()
    canvas.setStrokeColor(ECOSCAN_TEAL)
    canvas.setLineWidth(0.8)
    canvas.line(2 * cm, 1.5 * cm, A4[0] - 2 * cm, 1.5 * cm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#94A3B8"))
    canvas.drawString(2 * cm, 1.1 * cm, f"Généré par EcoScan le {timezone.now():%d/%m/%Y}")
    canvas.drawRightString(A4[0] - 2 * cm, 1.1 * cm, f"Page {doc.page}")
    canvas.restoreState()


def generer_pdf_livrable(livrable: Livrable) -> ContentFile:
    """Construit le PDF du livrable à partir des données réelles de
    l'organisation (métriques, recommandations décidées, anomalies) — jamais
    de contenu inventé. Lève une exception si la construction échoue ; c'est
    à l'appelant (LivrableViewSet.generer) de décider comment y réagir."""
    fiche_projet = livrable.fiche_projet
    organisation = fiche_projet.organisation if fiche_projet else None
    horizon = timezone.now() - timezone.timedelta(days=MAX_JOURS_HISTORIQUE)

    metriques = list(
        ResultatMetrique.objects.filter(organisation=organisation, periode_fin__gte=horizon)
        .order_by("-periode_fin")
    ) if organisation else []
    recommandations = list(
        Recommandation.objects.filter(objectif__organisation=organisation, statut=Recommandation.Statut.DECIDEE)
        .order_by("-date_decision")
    ) if organisation else []
    anomalies = list(
        Anomalie.objects.filter(organisation=organisation, date_detection__gte=horizon)
        .order_by("-date_detection")
    ) if organisation else []

    donnees = {"metriques": metriques, "recommandations": recommandations, "anomalies": anomalies}
    sections = SECTIONS_PAR_TYPE.get(livrable.type, ("metriques", "recommandations", "anomalies"))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=2.2 * cm,
    )
    styles = _styles()
    elements = _entete(livrable, organisation, fiche_projet, styles)

    for cle in sections:
        titre, builder = SECTION_BUILDERS[cle]
        elements.append(Paragraph(titre, styles["EcoScanSection"]))
        elements.extend(builder(donnees[cle], styles))
        elements.append(Spacer(1, 0.3 * cm))

    doc.build(elements, onFirstPage=_pied_de_page, onLaterPages=_pied_de_page)
    buffer.seek(0)
    nom_fichier = f"{livrable.nom.replace(' ', '_')}.pdf"
    return ContentFile(buffer.read(), name=nom_fichier)