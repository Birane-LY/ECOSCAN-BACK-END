"""
Ce fichier prend en charge exclusivement la lecture des flux binaires originaux
"""

import io
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None

try:
    from pdf2image import convert_from_bytes
except ImportError:
    convert_from_bytes = None


class OCRServiceError(Exception):
    """Exception personnalisée pour les erreurs survenues lors de l'OCR ou du traitement."""
    pass


class MoteurOCRService:
    """Gère la rasterisation et la numérisation brute des fichiers sources."""

    EXTENSIONS_IMAGE = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
    EXTENSIONS_PDF = {".pdf"}

    def extraire_texte_brut(self, fichier_source) -> str:
        """Résout l'extraction textuelle selon l'extension réelle du fichier."""
        mime = fichier_source.mime_type
        fichier = fichier_source.fichier
        extension = Path(fichier_source.fichier.name.lower()).suffix

        try:
            if extension in {".txt", ".csv"}:
                return self._lire_fichier_texte(fichier)
            elif extension in self.EXTENSIONS_IMAGE:
                return self._ocr_image(fichier)
            elif extension in self.EXTENSIONS_PDF:
                return self._ocr_pdf(fichier)
            else:
                raise OCRServiceError(f"Extension de fichier non prise en charge par l'OCR : {extension}")
        except OCRServiceError:
            raise
        except Exception as e:
            logger.exception("Échec de l'extraction brute pour le fichier %s", fichier_source.id)
            raise OCRServiceError(f"Erreur lors de la lecture du fichier source : {e}") from e

    @staticmethod
    def _lire_fichier_texte(fichier) -> str:
        fichier.open("rb")
        try:
            return fichier.read().decode("utf-8", errors="ignore")
        finally:
            fichier.close()

    def _ocr_image(self, fichier) -> str:
        if pytesseract is None or Image is None:
            raise OCRServiceError("Moteur OCR Tesseract ou Pillow non configuré sur le serveur.")
        fichier.open("rb")
        try:
            image = Image.open(io.BytesIO(fichier.read()))
            return pytesseract.image_to_string(image, lang="fra")
        finally:
            fichier.close()

    def _ocr_pdf(self, fichier) -> str:
        """Pipeline optimisé avec court-circuit natif pdftotext."""
        fichier.open("rb")
        try:
            contenu_pdf = fichier.read()
        finally:
            fichier.close()

        try:
            resultat = subprocess.run(
                ["pdftotext", "-layout", "-", "-"],
                input=contenu_pdf,
                capture_output=True,
                check=True,
            )
            texte_natif = resultat.stdout.decode("utf-8", errors="replace").strip()
            if texte_natif and len(texte_natif) > 50:
                return texte_natif
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

        if pytesseract is None or Image is None or convert_from_bytes is None:
            raise OCRServiceError("Moteur OCR non configuré pour les PDF scannés.")

        pages = convert_from_bytes(contenu_pdf, dpi=200, first_page=1, last_page=2)
        if not pages:
            raise OCRServiceError("Le PDF ne contient aucune page exploitable.")

        textes = [pytesseract.image_to_string(page, lang="fra") for page in pages]
        return "\n".join(textes)
