from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import fitz
from PIL import Image

from app.services.ingest import iter_pdf_pages, pdf_page_count


class StreamingIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        image = Image.new("RGB", (20, 20), "white")
        output = BytesIO()
        image.save(output, format="PNG")
        self.white_image = output.getvalue()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_image_page(self, path: Path, native_text: str = "") -> None:
        pdf: Any = fitz.open()
        page = pdf.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=self.white_image)
        if native_text:
            page.insert_textbox(page.rect, native_text, fontsize=6, color=(1, 1, 1))
        pdf.save(path)
        pdf.close()

    def test_text_pdf_is_yielded_with_original_page_number(self) -> None:
        path = self.root / "testo.pdf"
        pdf: Any = fitz.open()
        page = pdf.new_page(width=595, height=842)
        page.insert_text((72, 72), "Verbale CTS. " + "testo scientifico verificabile " * 12)
        pdf.save(path)
        pdf.close()

        self.assertEqual(pdf_page_count(path), 1)
        pages = list(iter_pdf_pages(path))
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].page_number, 1)
        self.assertEqual(pages[0].status, "processed")
        self.assertEqual(pages[0].ocr_status, "not-needed")
        self.assertIn("Verbale CTS", pages[0].content)

    def test_image_dominant_page_is_ocrd_even_with_plausible_native_text(self) -> None:
        path = self.root / "layer-incompleto.pdf"
        self._write_image_page(path, "testo nativo completo del verbale " * 12)
        ocr_data = {"text": ["verbale", "comitato", "scientifico"], "conf": ["92", "91", "89"]}
        with patch("app.services.ingest.pytesseract.image_to_data", return_value=ocr_data) as ocr:
            extracted = list(iter_pdf_pages(path))[0]

        self.assertTrue(ocr.called)
        self.assertTrue(extracted.ocr_used)
        self.assertEqual(extracted.status, "processed")
        self.assertEqual(extracted.ocr_status, "uncertain")
        self.assertIn("nativo", extracted.content)

    def test_low_confidence_ocr_remains_uncertain(self) -> None:
        path = self.root / "scansione.pdf"
        self._write_image_page(path)
        ocr_data = {"text": ["verbale", "comitato", "scientifico"], "conf": ["18", "12", "20"]}
        with patch("app.services.ingest.pytesseract.image_to_data", return_value=ocr_data):
            extracted = list(iter_pdf_pages(path))[0]

        self.assertEqual(extracted.status, "processed")
        self.assertEqual(extracted.ocr_status, "uncertain")
        self.assertTrue(extracted.content)

    def test_high_confidence_but_sparse_scan_remains_uncertain(self) -> None:
        path = self.root / "scansione-sparsa.pdf"
        self._write_image_page(path)
        ocr_data = {
            "text": ["Verbale", "CTS", "Roma"],
            "conf": ["92", "91", "89"],
            "top": ["100", "110", "120"],
            "height": ["20", "20", "20"],
        }
        with patch("app.services.ingest.pytesseract.image_to_data", return_value=ocr_data):
            extracted = list(iter_pdf_pages(path))[0]

        self.assertEqual(extracted.status, "processed")
        self.assertEqual(extracted.ocr_status, "uncertain")
        self.assertIn("Verbale", extracted.content)

    def test_dense_high_confidence_ocr_can_pass_quality_gate(self) -> None:
        path = self.root / "scansione-completa.pdf"
        self._write_image_page(path)
        words = [f"parola{index:02d}" for index in range(60)]
        ocr_data = {
            "text": words,
            "conf": ["92"] * len(words),
            "left": [str(100 + (index % 4) * 150) for index in range(len(words))],
            "top": [str(100 + index * 20) for index in range(len(words))],
            "width": ["80"] * len(words),
            "height": ["18"] * len(words),
        }
        with patch("app.services.ingest.pytesseract.image_to_data", return_value=ocr_data):
            extracted = list(iter_pdf_pages(path))[0]

        self.assertEqual(extracted.status, "processed")
        self.assertEqual(extracted.ocr_status, "success")
        self.assertIn("parola00", extracted.content)

    def test_uncertain_scan_retries_rotated_orientations_without_lowering_thresholds(self) -> None:
        path = self.root / "scansione-ruotata.pdf"
        self._write_image_page(path)
        low_data = {"text": ["parola"], "conf": ["18"]}
        words = [f"parola{index:02d}" for index in range(60)]
        rotated_data = {
            "text": words,
            "conf": ["92"] * len(words),
            "left": [str(100 + (index % 4) * 150) for index in range(len(words))],
            "top": [str(100 + index * 20) for index in range(len(words))],
            "width": ["80"] * len(words),
            "height": ["18"] * len(words),
        }
        with patch("app.services.ingest.pytesseract.image_to_data") as ocr:
            ocr.side_effect = [low_data, low_data, low_data, rotated_data]
            extracted = list(iter_pdf_pages(path))[0]

        self.assertEqual(ocr.call_count, 4)
        self.assertEqual(ocr.call_args_list[-1].kwargs["config"], "--oem 1 --psm 11")
        self.assertEqual(extracted.status, "processed")
        self.assertEqual(extracted.ocr_status, "success")
        self.assertIn("parola00", extracted.content)

    def test_extreme_blank_page_fails_closed_before_ocr_rasterization(self) -> None:
        path = self.root / "pagina-enorme.pdf"
        pdf: Any = fitz.open()
        pdf.new_page(width=10_000, height=10_000)
        pdf.save(path)
        pdf.close()

        page = list(iter_pdf_pages(path))[0]
        self.assertEqual(page.status, "error")
        self.assertEqual(page.ocr_status, "error")
        self.assertTrue(page.ocr_used)

    def test_skip_completed_page_before_ocr_preserves_original_page_number(self) -> None:
        path = self.root / "salta-prima-pagina.pdf"
        pdf: Any = fitz.open()
        scanned = pdf.new_page(width=595, height=842)
        scanned.insert_image(scanned.rect, stream=self.white_image)
        text_page = pdf.new_page(width=595, height=842)
        text_page.insert_text((72, 72), "testo scientifico verificabile del verbale " * 10)
        pdf.save(path)
        pdf.close()

        with patch("app.services.ingest.pytesseract.image_to_data") as ocr:
            pages = list(iter_pdf_pages(path, skip_page_numbers={1}))

        ocr.assert_not_called()
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].page_number, 2)
        self.assertEqual(pages[0].status, "processed")


if __name__ == "__main__":
    unittest.main()
