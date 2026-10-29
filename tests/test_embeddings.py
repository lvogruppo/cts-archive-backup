from __future__ import annotations

import re
import unittest
from unittest.mock import patch

import numpy as np

import app.retrieval.embeddings as embeddings


class _FakeTokenizer:
    def __init__(self) -> None:
        self.max_input_chars = 0
        self.model_max_length = 512

    def __call__(
        self,
        text: str,
        add_special_tokens: bool = True,
        truncation: bool = False,
        return_offsets_mapping: bool = False,
        verbose: bool = False,
    ) -> dict[str, list]:
        self.max_input_chars = max(self.max_input_chars, len(text))
        matches = list(re.finditer(r"\S+", text))
        if return_offsets_mapping:
            return {"offset_mapping": [(match.start(), match.end()) for match in matches]}
        count = len(matches) + (2 if add_special_tokens else 0)
        return {"input_ids": list(range(count))}


class _FakeModel:
    def __init__(self) -> None:
        self.tokenizer = _FakeTokenizer()
        self.max_seq_length = 512

    def encode(self, texts, **kwargs):
        return np.full((len(texts), embeddings.EMBEDDING_DIM), np.nan, dtype=np.float32)


class EmbeddingTests(unittest.TestCase):
    def test_long_passage_is_fully_covered_in_bounded_tokenizer_windows(self) -> None:
        model = _FakeModel()
        text = "parola " * 4_000
        with patch.object(embeddings, "_model", return_value=model):
            chunks = embeddings.split_passage_chunks(text, max_tokens=30, overlap_tokens=5)

        self.assertGreater(len(chunks), 3)
        self.assertLessEqual(model.tokenizer.max_input_chars, embeddings.TOKENIZER_WINDOW_CHARS)
        for _, _, content in chunks:
            encoded_length = len(model.tokenizer(text=f"passage: {content}")["input_ids"])
            self.assertLessEqual(encoded_length, 512)

        covered_end = 0
        for start, end, _ in sorted(chunks):
            if start > covered_end:
                self.assertFalse(text[covered_end:start].strip(), "il chunking ha perso del testo non bianco")
            covered_end = max(covered_end, end)
        self.assertFalse(text[covered_end:].strip(), "il chunking non ha coperto la fine del testo")

    def test_non_finite_model_output_is_rejected(self) -> None:
        with patch.object(embeddings, "_model", return_value=_FakeModel()):
            with self.assertRaisesRegex(ValueError, "non finito"):
                embeddings.embed_texts(["testo"], "passage")


if __name__ == "__main__":
    unittest.main()
