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
ORANGE ci-dessous), extraites directement du logo fourni. Le logo est livré
dans assets/logo_ecoscan.png ; surchargeable sans
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
from datetime import timedelta
from xml.sax.saxutils import escape

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable

from analysis.models import Anomalie, Livrable, Recommandation, ResultatMetrique

logger = logging.getLogger(__name__)

MAX_JOURS_HISTORIQUE = 90
MAX_LIGNES_PAR_SECTION = 15
ECOSCAN_NAVY = colors.HexColor("#0B2E6D")     # texte, titres, en-têtes de tableau
ECOSCAN_TEAL = colors.HexColor("#0E8596")     # filets, accents secondaires
ECOSCAN_ORANGE = colors.HexColor("#F5901F")   # mise en avant ponctuelle (chiffres clés, alertes)
ECOSCAN_GRIS_CLAIR = colors.HexColor("#F3F5F8")  # fond alterné des tableaux (neutre, pas une 4e couleur de marque)

CHEMIN_LOGO_DEFAUT = os.path.join(settings.BASE_DIR, "assets", "logo_ecoscan.png")

TITRES_TEMPLATE = {
    "COMPTABLE": "Bilan énergétique — Rapport comptable",
    "BANQUE": "Dossier de financement — Synthèse énergétique",
    "INVESTISSEUR": "Business case — Opportunité d'investissement énergétique",
    "MEMOIRE": "Mémoire stratégique — Synthèse EcoScan",
}

# Ce que chaque destinataire regarde en priorité — un comptable veut des
# chiffres vérifiables, une banque un dossier de financement, un investisseur
# un potentiel de gain. Le contenu de base (métriques + recommandations) est
# commun ; seules les sections additionnelles et le ton du résumé varient.
SECTIONS_PAR_TYPE = {
    "COMPTABLE": ("metriques", "anomalies"),
    "BANQUE": ("metriques", "recommandations", "anomalies"),
    "INVESTISSEUR": ("recommandations", "metriques"),
    "MEMOIRE": ("memoire",),
}


def _styles():
    base = getSampleStyleSheet()
    base["Normal"].fontName = "Helvetica"
    base["Normal"].fontSize = 10
    base["Normal"].leading = 15
    base["Normal"].textColor = colors.HexColor("#334155")
    base["Normal"].spaceAfter = 3
    base.add(ParagraphStyle(
        name="EcoScanTitre", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=21, leading=25, textColor=ECOSCAN_NAVY, spaceAfter=5, alignment=0,
    ))
    base.add(ParagraphStyle(
        name="EcoScanKicker", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=8, leading=10, textColor=ECOSCAN_TEAL, spaceAfter=5,
    ))
    base.add(ParagraphStyle(
        name="EcoScanSousTitre", parent=base["Normal"], fontSize=9.5,
        leading=13, textColor=colors.HexColor("#64748B"), spaceAfter=0,
    ))
    base.add(ParagraphStyle(
        name="EcoScanSection", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=15, textColor=ECOSCAN_NAVY, spaceBefore=18, spaceAfter=8,
    ))
    base.add(ParagraphStyle(
        name="EcoScanMemoireTitre", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=17, leading=21, textColor=ECOSCAN_NAVY, spaceBefore=1, spaceAfter=12,
    ))
    base.add(ParagraphStyle(
        name="EcoScanCardLabel", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=8, leading=11, textColor=ECOSCAN_TEAL, spaceAfter=0,
    ))
    base.add(ParagraphStyle(
        name="EcoScanCardBody", parent=base["Normal"], fontSize=9.5,
        leading=14, textColor=colors.HexColor("#334155"), spaceAfter=0,
    ))
    base.add(ParagraphStyle(
        name="EcoScanImpactLabel", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=8, leading=10, textColor=colors.HexColor("#64748B"), spaceAfter=4,
    ))
    base.add(ParagraphStyle(
        name="EcoScanImpactValue", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=14, leading=17, textColor=ECOSCAN_NAVY, spaceAfter=0,
    ))
    return base


def _chemin_logo():
    """Utilise le logo de marque par défaut si aucun chemin valide n'est configuré."""
    chemin_configure = getattr(settings, "ECOSCAN_REPORT_LOGO_PATH", "")
    if chemin_configure and os.path.isfile(chemin_configure):
        return chemin_configure
    return CHEMIN_LOGO_DEFAUT if os.path.isfile(CHEMIN_LOGO_DEFAUT) else chemin_configure


def _entete(livrable, organisation, fiche_projet, styles):
    """Bandeau de marque et cartouche de contexte du document."""
    elements = []

    chemin_logo = _chemin_logo()
    logo_flowable = None
    if chemin_logo and os.path.isfile(chemin_logo):
        try:
            logo_flowable = Image(chemin_logo, width=1.8 * cm, height=1.8 * cm)
        except (OSError, ValueError):
            logger.warning("Logo introuvable ou illisible (%s) — en-tête généré sans logo.", chemin_logo)
    else:
        logger.warning("ECOSCAN_REPORT_LOGO_PATH introuvable (%s) — en-tête généré sans logo.", chemin_logo)

    titre_bloc = [
        Paragraph("ECOSCAN  /  PILOTAGE ÉNERGÉTIQUE", styles["EcoScanKicker"]),
        Paragraph(escape(TITRES_TEMPLATE.get(livrable.type, livrable.nom)), styles["EcoScanTitre"]),
        Paragraph("Analyse claire, décisions éclairées.", styles["EcoScanSousTitre"]),
    ]

    entete_table = Table(
        [[logo_flowable or "", titre_bloc]],
        colWidths=[2.4 * cm, 14.6 * cm],
    )
    entete_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F7FB")),
        ("BACKGROUND", (0, 0), (0, 0), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E2EAF2")),
        ("LINEBEFORE", (1, 0), (1, 0), 2, ECOSCAN_TEAL),
        ("LEFTPADDING", (0, 0), (0, 0), 9),
        ("RIGHTPADDING", (0, 0), (0, 0), 9),
        ("LEFTPADDING", (1, 0), (1, 0), 14),
        ("RIGHTPADDING", (1, 0), (1, 0), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(entete_table)
    elements.extend([Spacer(1, 0.32 * cm), HRFlowable(
        width="100%", thickness=1.2, color=ECOSCAN_TEAL, spaceAfter=0.28 * cm,
    )])

    periode = (
        "Non applicable — mémoire stratégique"
        if livrable.type == "MEMOIRE"
        else f"{MAX_JOURS_HISTORIQUE} derniers jours"
    )
    infos = [
        [Paragraph("ORGANISATION", styles["EcoScanCardLabel"]),
         Paragraph("ÉMIS LE", styles["EcoScanCardLabel"])],
        [Paragraph(escape(organisation.nom if organisation else "—"), styles["EcoScanCardBody"]),
         Paragraph(f"{timezone.localtime():%d/%m/%Y à %H:%M}", styles["EcoScanCardBody"])],
        [Paragraph("FICHE PROJET", styles["EcoScanCardLabel"]),
         Paragraph("PÉRIODE DES DONNÉES", styles["EcoScanCardLabel"])],
        [Paragraph(escape(fiche_projet.nom if fiche_projet else "Aucun projet associé"), styles["EcoScanCardBody"]),
         Paragraph(escape(periode), styles["EcoScanCardBody"])],
    ]
    table_infos = Table(infos, colWidths=[8.5 * cm, 8.5 * cm])
    table_infos.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2EAF2")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#E2EAF2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(table_infos)
    elements.append(Spacer(1, 0.2 * cm))
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


def _contenu_memoire(memoire, styles):
    if not memoire:
        return [Paragraph("Aucune mémoire stratégique associée à ce rapport.", styles["Normal"])]

    elements = [Paragraph(escape(memoire.titre), styles["EcoScanMemoireTitre"])]
    impacts = []
    if memoire.impact_attendu_fcfa is not None:
        impacts.append(("IMPACT ATTENDU", f"{memoire.impact_attendu_fcfa} FCFA"))
    if memoire.impact_mesure_fcfa is not None:
        impacts.append(("IMPACT MESURÉ", f"{memoire.impact_mesure_fcfa} FCFA"))
    if impacts:
        impact_cards = []
        for libelle, valeur in impacts:
            impact_cards.append(Table(
                [[Paragraph(escape(libelle), styles["EcoScanImpactLabel"])],
                 [Paragraph(escape(valeur), styles["EcoScanImpactValue"])]],
                colWidths=[8.2 * cm],
            ))
        impact_table = Table(
            [impact_cards],
            colWidths=[8.5 * cm] * len(impact_cards),
        )
        impact_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F7FB")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E2EAF2")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2EAF2")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        elements.extend([impact_table, Spacer(1, 0.35 * cm)])

    blocs = [
        ("Signal initial", memoire.signal_initial),
        ("Hypothèse", memoire.hypothese_texte),
        ("Action menée", memoire.action_texte),
        ("Statut", memoire.get_statut_display()),
    ]
    for libelle, valeur in blocs:
        if not valeur:
            continue
        bloc = Table(
            [[Paragraph(escape(libelle.upper()), styles["EcoScanCardLabel"])],
             [Paragraph(escape(str(valeur)), styles["EcoScanCardBody"])]],
            colWidths=[17 * cm],
        )
        bloc.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2EAF2")),
            ("LINEBEFORE", (0, 0), (0, -1), 2, ECOSCAN_TEAL),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 11),
            ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
            ("TOPPADDING", (0, 1), (-1, 1), 2),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
        ]))
        elements.extend([bloc, Spacer(1, 0.16 * cm)])
    return elements


SECTION_BUILDERS = {
    "metriques": ("Indicateurs de performance récents", _table_metriques),
    "recommandations": ("Recommandations décidées", _table_recommandations),
    "anomalies": ("Anomalies détectées", _table_anomalies),
    "memoire": ("Mémoire stratégique", _contenu_memoire),
}


def _pied_de_page(canvas, doc):
    """Filet et mention en pied de chaque page — discret, dans la charte."""
    canvas.saveState()
    canvas.setStrokeColor(ECOSCAN_TEAL)
    canvas.setLineWidth(0.8)
    canvas.line(2 * cm, 1.5 * cm, A4[0] - 2 * cm, 1.5 * cm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#94A3B8"))
    canvas.drawString(2 * cm, 1.1 * cm, f"Généré par EcoScan le {timezone.localtime():%d/%m/%Y}")
    canvas.drawRightString(A4[0] - 2 * cm, 1.1 * cm, f"Page {doc.page}")
    canvas.restoreState()


def generer_pdf_livrable(livrable: Livrable) -> ContentFile:
    """Construit le PDF du livrable à partir des données réelles de
    l'organisation (métriques, recommandations décidées, anomalies) — jamais
    de contenu inventé. Lève une exception si la construction échoue ; c'est
    à l'appelant (LivrableViewSet.generer) de décider comment y réagir."""
    fiche_projet = livrable.fiche_projet
    organisation = livrable.organisation
    horizon = timezone.now() - timedelta(days=MAX_JOURS_HISTORIQUE)

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

    donnees = {
        "metriques": metriques,
        "recommandations": recommandations,
        "anomalies": anomalies,
        "memoire": livrable.memoire,
    }
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