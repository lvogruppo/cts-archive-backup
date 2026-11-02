from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_NAME = os.getenv("CTS_EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
EMBEDDING_DIM = 384
MAX_MODEL_TOKENS = 512
MAX_PASSAGE_TOKENS = 460
PASSAGE_OVERLAP_TOKENS = 64
CHUNKING_VERSION = "e5-small-token-460-overlap-64-v2"


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    model = SentenceTransformer(MODEL_NAME, device="cpu")
    # Long passages must be tokenized in full before they are split; encode() still uses model.max_seq_length.
    tokenizer: Any = model.tokenizer
    tokenizer.model_max_length = 1_000_000
    return model


def _token_count(tokenizer: Any, text: str) -> int:
    encoded = tokenizer(text, add_special_tokens=True, truncation=False, verbose=False)
    return len(encoded["input_ids"])


def split_passage_chunks(
    text: str,
    max_tokens: int = MAX_PASSAGE_TOKENS,
    overlap_tokens: int = PASSAGE_OVERLAP_TOKENS,
) -> list[tuple[int, int, str]]:
    """Split text by the model tokenizer, preserving source character offsets and overlap."""
    text = text.strip()
    if not text:
        return []

    model = _model()
    tokenizer: Any = model.tokenizer
    model_limit = min(int(model.max_seq_length), MAX_MODEL_TOKENS)
    content_budget = min(max_tokens, model_limit - 32)
    if content_budget < 1:
        raise ValueError("Lunghezza massima E5 non sufficiente per il prefisso passage.")
    overlap_tokens = max(0, min(overlap_tokens, content_budget - 1))

    encoded = tokenizer(text, add_special_tokens=False, truncation=False, return_offsets_mapping=True, verbose=False)
    raw_offsets = encoded.get("offset_mapping", [])
    offsets = [(int(start), int(end)) for start, end in raw_offsets if int(end) > int(start)]
    if not offsets:
        candidate = text
        if _token_count(tokenizer, f"passage: {candidate}") > model_limit:
            raise ValueError("Testo non segmentabile entro il limite token di E5.")
        return [(0, len(text), candidate)]

    chunks: list[tuple[int, int, str]] = []
    token_start = 0
    token_count = len(offsets)
    while token_start < token_count:
        token_end = min(token_start + content_budget, token_count)
        char_start = 0 if token_start == 0 else offsets[token_start][0]
        while token_end > token_start:
            char_end = len(text) if token_end == token_count else offsets[token_end][0]
            candidate = text[char_start:char_end].strip()
            encoded_length = _token_count(tokenizer, f"passage: {candidate}")
            if candidate and encoded_length <= model_limit:
                chunks.append((char_start, char_end, candidate))
                break
            token_end -= 1
        if token_end <= token_start:
            raise ValueError("Impossibile segmentare un passaggio entro il limite token di E5.")
        if token_end >= token_count:
            break
        token_start = max(token_start + 1, token_end - overlap_tokens)
    return chunks


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
    return result
