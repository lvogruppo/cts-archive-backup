from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import fitz
import pytesseract
from PIL import Image

from app.retrieval.embeddings import split_passage_chunks


@dataclass
class ExtractedPage:
    page_number: int
    content: str
    ocr_used: bool
    ocr_status: str
    status: str


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_pdf_pages(path: str | Path, ocr_language: str = "ita+eng") -> list[ExtractedPage]:
    pages: list[ExtractedPage] = []
    with fitz.open(str(path)) as pdf:
        for page_index in range(pdf.page_count):
            # The PyMuPDF stubs omit several runtime Page methods used below.
            page: Any = pdf.load_page(page_index)
            text = normalize_text(page.get_text("text", sort=True))
            useful_chars = sum(char.isalnum() for char in text)
            page_area = max(float(page.rect.width * page.rect.height), 1.0)
            max_image_fraction = 0.0
            try:
                for image in page.get_images(full=True):
                    for image_rect in page.get_image_rects(image[0]):
                        clipped = image_rect & page.rect
                        max_image_fraction = max(max_image_fraction, float(clipped.width * clipped.height) / page_area)
            except Exception:
                max_image_fraction = 0.0
            ocr_used = useful_chars < 80 or (useful_chars < 400 and max_image_fraction >= 0.45)
            ocr_status = "not-needed"
            status = "processed"
            if ocr_used:
                try:
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    image = Image.open(BytesIO(pixmap.tobytes("png")))
                    ocr_text = normalize_text(pytesseract.image_to_string(image, lang=ocr_language))
                    ocr_chars = sum(char.isalnum() for char in ocr_text)
                    if ocr_chars >= 10:
                        if ocr_chars > useful_chars:
                            text = ocr_text
                        ocr_status = "success"
                    else:
                        ocr_status = "empty"
                        status = "empty" if not text else "processed"
                    if ocr_chars < 10 and not text:
                        text = ocr_text
                except Exception:
                    ocr_status = "error"
                    status = "error"
            elif not text:
                status = "empty"
            pages.append(
                ExtractedPage(
                    page_number=page_index + 1,
                    content=text,
                    ocr_used=ocr_used,
                    ocr_status=ocr_status,
                    status=status,
                )
            )
    return pages


def chunk_page(
    text: str,
    max_tokens: int = 460,
    overlap_tokens: int = 64,
) -> list[tuple[int, int, str]]:
    """Split one page by the E5 tokenizer, retaining original character offsets."""
    return split_passage_chunks(normalize_text(text), max_tokens=max_tokens, overlap_tokens=overlap_tokens)
