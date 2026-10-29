from __future__ import annotations

import hashlib
import math
import re
import statistics
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import fitz
import pytesseract
from PIL import Image

from app.retrieval.embeddings import iter_passage_chunks

MAX_OCR_PIXELS = 16_000_000
MAX_OCR_SCALE = 2.0
MIN_OCR_SCALE = 0.5
MIN_OCR_WORDS = 20
MIN_OCR_ALNUM_CHARS = 120
MIN_OCR_MEDIAN_CONFIDENCE = 50.0
MIN_OCR_WORDS_PER_MEGAPIXEL = 12.0
MIN_OCR_VERTICAL_COVERAGE = 0.20
MAX_OCR_SECONDS_PER_PAGE = 120


@dataclass
class ExtractedPage:
    page_number: int
    content: str
    ocr_used: bool
    ocr_status: str
    status: str


@dataclass(frozen=True)
class OcrMetrics:
    text: str
    word_count: int
    alphanumeric_chars: int
    median_confidence: float
    words_per_megapixel: float
    vertical_coverage: float


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pdf_page_count(path: str | Path) -> int:
    with fitz.open(str(path)) as pdf:
        return int(pdf.page_count)


def _ocr_metrics(data: dict[str, Any], image_width: int, image_height: int) -> OcrMetrics:
    words: list[str] = []
    confidences: list[float] = []
    raw_words = data.get("text", [])
    raw_confidences = data.get("conf", [])
    tops: list[int] = []
    bottoms: list[int] = []
    raw_tops = data.get("top", [])
    raw_heights = data.get("height", [])
    for index, (raw_word, raw_confidence) in enumerate(zip(raw_words, raw_confidences)):
        word = normalize_text(str(raw_word))
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            continue
        if not word or confidence < 0 or not any(char.isalnum() for char in word):
            continue
        words.append(word)
        confidences.append(confidence)
        try:
            top = int(float(raw_tops[index]))
            height = int(float(raw_heights[index]))
        except (IndexError, TypeError, ValueError):
            continue
        if height > 0 and image_height > 0:
            clipped_top = max(0, min(image_height, top))
            clipped_bottom = max(clipped_top, min(image_height, top + height))
            if clipped_bottom > clipped_top:
                tops.append(clipped_top)
                bottoms.append(clipped_bottom)
    result = normalize_text(" ".join(words))
    median_confidence = statistics.median(confidences) if confidences else 0.0
    image_megapixels = max((image_width * image_height) / 1_000_000, 0.001)
    vertical_coverage = (
        max(0, max(bottoms) - min(tops)) / image_height if tops and image_height > 0 else 0.0
    )
    return OcrMetrics(
        text=result,
        word_count=len(words),
        alphanumeric_chars=sum(char.isalnum() for char in result),
        median_confidence=float(median_confidence),
        words_per_megapixel=len(words) / image_megapixels,
        vertical_coverage=float(vertical_coverage),
    )


def _ocr_quality_passes(ocr: OcrMetrics) -> bool:
    return (
        ocr.word_count >= MIN_OCR_WORDS
        and ocr.alphanumeric_chars >= MIN_OCR_ALNUM_CHARS
        and ocr.median_confidence >= MIN_OCR_MEDIAN_CONFIDENCE
        and ocr.words_per_megapixel >= MIN_OCR_WORDS_PER_MEGAPIXEL
        and ocr.vertical_coverage >= MIN_OCR_VERTICAL_COVERAGE
    )


def _ocr_text_layer_mismatch(ocr: OcrMetrics, useful_chars: int, image_dominant: bool) -> bool:
    return (
        image_dominant
        and useful_chars >= 80
        and ocr.alphanumeric_chars < max(40, int(useful_chars * 0.6))
    )


def _ocr_rank(ocr: OcrMetrics, useful_chars: int, image_dominant: bool) -> tuple[int, float, int, int]:
    trusted = _ocr_quality_passes(ocr) and not _ocr_text_layer_mismatch(ocr, useful_chars, image_dominant)
    return (int(trusted), ocr.median_confidence, ocr.alphanumeric_chars, ocr.word_count)


def _retry_rotated_ocr(image: Image.Image, ocr_language: str) -> list[OcrMetrics]:
    """Try the three non-zero orientations when the first OCR pass is uncertain."""
    results: list[OcrMetrics] = []
    for angle in (90, 180, 270):
        rotated = image.rotate(angle, expand=True)
        try:
            data = pytesseract.image_to_data(
                rotated,
                lang=ocr_language,
                output_type=pytesseract.Output.DICT,
                config="--oem 1 --psm 11",
                timeout=30,
            )
        except Exception:
            continue
        results.append(_ocr_metrics(data, rotated.width, rotated.height))
    return results


def iter_pdf_pages(
    path: str | Path,
    ocr_language: str = "ita+eng",
    skip_page_numbers: set[int] | None = None,
) -> Iterator[ExtractedPage]:
    """Yield pages incrementally, optionally skipping already-complete pages before OCR."""
    skipped = skip_page_numbers or set()
    with fitz.open(str(path)) as pdf:
        for page_index in range(pdf.page_count):
            if page_index + 1 in skipped:
                continue
            # The PyMuPDF stubs omit several runtime Page methods used below.
            page: Any = pdf.load_page(page_index)
            text = normalize_text(page.get_text("text", sort=True))
            native_text = text
            useful_chars = sum(char.isalnum() for char in native_text)
            page_area = max(float(page.rect.width * page.rect.height), 1.0)
            image_area = 0.0
            try:
                for image in page.get_images(full=True):
                    for image_rect in page.get_image_rects(image[0]):
                        clipped = image_rect & page.rect
                        image_area += float(clipped.width * clipped.height)
            except Exception:
                image_area = 0.0
            image_fraction = min(image_area / page_area, 1.0)
            image_dominant = image_fraction >= 0.45

            # A full-page scan can carry a plausible but incomplete text layer; always OCR it.
            ocr_used = useful_chars < 80 or image_dominant
            ocr_status = "not-needed"
            status = "processed"
            if ocr_used:
                scale = min(MAX_OCR_SCALE, math.sqrt((MAX_OCR_PIXELS * 0.8) / page_area))
                if scale < MIN_OCR_SCALE:
                    ocr_status = "error"
                    status = "error"
                else:
                    try:
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        if pixmap.width * pixmap.height > MAX_OCR_PIXELS:
                            raise ValueError("Raster OCR oltre il limite di pixel consentito.")
                        image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
                        ocr: OcrMetrics | None = None
                        try:
                            ocr_data = pytesseract.image_to_data(
                                image,
                                lang=ocr_language,
                                output_type=pytesseract.Output.DICT,
                                timeout=MAX_OCR_SECONDS_PER_PAGE,
                            )
                            ocr = _ocr_metrics(ocr_data, image.width, image.height)
                        except Exception:
                            # A timeout in the default orientation may still be recoverable below.
                            pass
                        if image_dominant and (
                            ocr is None
                            or not _ocr_quality_passes(ocr)
                            or _ocr_text_layer_mismatch(ocr, useful_chars, image_dominant)
                        ):
                            rotated_results = _retry_rotated_ocr(image, ocr_language)
                            candidates = ([ocr] if ocr is not None else []) + rotated_results
                            if candidates:
                                ocr = max(
                                    candidates,
                                    key=lambda candidate: _ocr_rank(candidate, useful_chars, image_dominant),
                                )
                        if ocr is None:
                            raise RuntimeError("OCR non riuscito nell'orientamento iniziale e nei tentativi ruotati.")
                        ocr_text = ocr.text
                        ocr_chars = ocr.alphanumeric_chars
                        quality_ok = _ocr_quality_passes(ocr)
                        text_layer_mismatch = _ocr_text_layer_mismatch(ocr, useful_chars, image_dominant)
                        if quality_ok and not text_layer_mismatch:
                            ocr_status = "success"
                            if image_dominant or ocr_chars > useful_chars:
                                text = ocr_text
                        else:
                            ocr_status = "uncertain" if ocr_text or native_text else "empty"
                            # Keep whichever text exists for review, but the coverage gate rejects it.
                            if not native_text:
                                text = ocr_text
                        if not text:
                            status = "empty"
                    except Exception:
                        ocr_status = "error"
                        status = "error"
            elif not text:
                status = "empty"

            yield ExtractedPage(
                page_number=page_index + 1,
                content=text,
                ocr_used=ocr_used,
                ocr_status=ocr_status,
                status=status,
            )


def extract_pdf_pages(path: str | Path, ocr_language: str = "ita+eng") -> list[ExtractedPage]:
    """Compatibility helper for small tests; production ingestion uses the iterator."""
    return list(iter_pdf_pages(path, ocr_language=ocr_language))


def chunk_page(
    text: str,
    max_tokens: int = 460,
    overlap_tokens: int = 64,
) -> Iterator[tuple[int, int, str]]:
    """Yield E5-safe chunks incrementally, retaining original character offsets."""
    return iter_passage_chunks(normalize_text(text), max_tokens=max_tokens, overlap_tokens=overlap_tokens)
