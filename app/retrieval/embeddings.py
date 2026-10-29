from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_NAME = os.getenv("CTS_EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
EMBEDDING_DIM = 384
MAX_MODEL_TOKENS = 512
MAX_PASSAGE_TOKENS = 460
PASSAGE_OVERLAP_TOKENS = 64
CHUNKING_VERSION = "e5-small-token-460-overlap-64-ocr-spatial-v4"
TOKENIZER_WINDOW_CHARS = 12_000
TOKENIZER_WINDOW_OVERLAP_CHARS = 800


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    model = SentenceTransformer(MODEL_NAME, device="cpu")
    # Long passages are handled in bounded windows before encode(); encode() still enforces max_seq_length.
    tokenizer: Any = model.tokenizer
    tokenizer.model_max_length = 1_000_000
    return model


def _token_count(tokenizer: Any, text: str) -> int:
    encoded = tokenizer(text, add_special_tokens=True, truncation=False, verbose=False)
    return len(encoded["input_ids"])


def _char_windows(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    while start < len(text):
        end = min(start + TOKENIZER_WINDOW_CHARS, len(text))
        if end < len(text):
            lower = min(end, start + TOKENIZER_WINDOW_CHARS // 2)
            whitespace = max(text.rfind(" ", lower, end), text.rfind("\n", lower, end))
            if whitespace > start:
                end = whitespace + 1
        yield start, end
        if end >= len(text):
            break
        start = max(start + 1, end - TOKENIZER_WINDOW_OVERLAP_CHARS)


def iter_passage_chunks(
    text: str,
    max_tokens: int = MAX_PASSAGE_TOKENS,
    overlap_tokens: int = PASSAGE_OVERLAP_TOKENS,
) -> Iterator[tuple[int, int, str]]:
    """Yield E5-safe chunks incrementally, preserving source offsets and overlap."""
    text = text.strip()
    if not text:
        return

    model = _model()
    tokenizer: Any = model.tokenizer
    model_limit = min(int(model.max_seq_length), MAX_MODEL_TOKENS)
    content_budget = min(max_tokens, model_limit - 32)
    if content_budget < 1:
        raise ValueError("Lunghezza massima E5 non sufficiente per il prefisso passage.")
    overlap_tokens = max(0, min(overlap_tokens, content_budget - 1))

    seen_offsets: set[tuple[int, int]] = set()
    for window_start, window_end in _char_windows(text):
        fragment = text[window_start:window_end]
        encoded = tokenizer(
            fragment,
            add_special_tokens=False,
            truncation=False,
            return_offsets_mapping=True,
            verbose=False,
        )
        raw_offsets = encoded.get("offset_mapping", [])
        offsets = [(int(start), int(end)) for start, end in raw_offsets if int(end) > int(start)]
        if not offsets:
            if _token_count(tokenizer, f"passage: {fragment}") > model_limit:
                raise ValueError("Testo non segmentabile entro il limite token di E5.")
            candidate = fragment.strip()
            if candidate:
                left_trim = len(fragment) - len(fragment.lstrip())
                right_trim = len(fragment) - len(fragment.rstrip())
                bounds = (window_start + left_trim, window_end - right_trim)
                if bounds not in seen_offsets:
                    seen_offsets.add(bounds)
                    yield bounds[0], bounds[1], candidate
            continue

        token_start = 0
        token_count = len(offsets)
        while token_start < token_count:
            token_end = min(token_start + content_budget, token_count)
            local_start = 0 if token_start == 0 else offsets[token_start][0]
            while token_end > token_start:
                local_end = len(fragment) if token_end == token_count else offsets[token_end][0]
                raw_candidate = fragment[local_start:local_end]
                candidate = raw_candidate.strip()
                encoded_length = _token_count(tokenizer, f"passage: {candidate}") if candidate else model_limit + 1
                if candidate and encoded_length <= model_limit:
                    left_trim = len(raw_candidate) - len(raw_candidate.lstrip())
                    right_trim = len(raw_candidate) - len(raw_candidate.rstrip())
                    bounds = (
                        window_start + local_start + left_trim,
                        window_start + local_end - right_trim,
                    )
                    if bounds not in seen_offsets:
                        seen_offsets.add(bounds)
                        yield bounds[0], bounds[1], candidate
                    break
                token_end -= 1
            if token_end <= token_start:
                raise ValueError("Impossibile segmentare un passaggio entro il limite token di E5.")
            if token_end >= token_count:
                break
            token_start = max(token_start + 1, token_end - overlap_tokens)


def split_passage_chunks(
    text: str,
    max_tokens: int = MAX_PASSAGE_TOKENS,
    overlap_tokens: int = PASSAGE_OVERLAP_TOKENS,
) -> list[tuple[int, int, str]]:
    """Compatibility helper for callers that explicitly need a list."""
    return list(iter_passage_chunks(text, max_tokens=max_tokens, overlap_tokens=overlap_tokens))


def embed_texts(texts: list[str], kind: str) -> np.ndarray:
    if kind not in {"query", "passage"}:
        raise ValueError("kind deve essere query oppure passage")
    if not texts:
        return np.empty((0, EMBEDDING_DIM), dtype=np.float32)

    model = _model()
    tokenizer: Any = model.tokenizer
    model_limit = min(int(model.max_seq_length), MAX_MODEL_TOKENS)
    prefixed = [f"{kind}: {text.strip()}" for text in texts]
    for index, value in enumerate(prefixed):
        token_count = _token_count(tokenizer, value)
        if token_count > model_limit:
            raise ValueError(
                f"Testo {kind} troppo lungo ({token_count} token; massimo {model_limit}); nessun troncamento eseguito."
            )

    vectors = model.encode(
        prefixed,
        batch_size=32,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    result = np.asarray(vectors, dtype=np.float32)
    if result.ndim != 2 or result.shape[1] != EMBEDDING_DIM:
        raise ValueError(f"Dimensione embedding inattesa: {result.shape}")
    if not np.isfinite(result).all():
        raise ValueError("Embedding E5 non finito; vettore rifiutato.")
    return result
