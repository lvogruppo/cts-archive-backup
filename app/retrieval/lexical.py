from __future__ import annotations

import threading
from typing import Any, cast

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import Chunk


class LexicalIndex:
    """Indice TF-IDF volatile e ricostruibile; nessun FULLTEXT o servizio esterno richiesto."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._signature: tuple[int, int | None] | None = None
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix: csr_matrix | None = None
        self._ids = np.empty((0,), dtype=np.int64)

    def _refresh_if_changed(self, db: Session) -> None:
        count, max_id = db.query(func.count(Chunk.id), func.max(Chunk.id)).filter(Chunk.content != "").one()
        signature = (int(count or 0), int(max_id) if max_id is not None else None)
        with self._lock:
            if signature == self._signature:
                return
            rows = (
                db.query(Chunk.id, Chunk.content)
                .filter(Chunk.content != "")
                .order_by(Chunk.id.asc())
                .all()
            )
            ids = np.asarray([int(row[0]) for row in rows], dtype=np.int64)
            texts = [row[1] or "" for row in rows]
            vectorizer = TfidfVectorizer(
                lowercase=True,
                strip_accents="unicode",
                ngram_range=(1, 2),
                sublinear_tf=True,
                dtype=cast(Any, np.float32),
                token_pattern=r"(?u)\b\w[\w’'-]{1,}\b",
            )
            try:
                matrix = cast(csr_matrix, vectorizer.fit_transform(texts)).tocsr() if texts else csr_matrix((0, 0), dtype=np.float32)
            except ValueError:
                vectorizer = None
                matrix = csr_matrix((0, 0), dtype=np.float32)
            self._ids = ids
            self._vectorizer = vectorizer
            self._matrix = matrix
            self._signature = signature

    def size(self, db: Session) -> int:
        self._refresh_if_changed(db)
        return int(self._ids.size)

    def search(
        self,
        db: Session,
        query: str,
        limit: int,
        allowed_ids: set[int] | None = None,
    ) -> list[tuple[int, float]]:
        self._refresh_if_changed(db)
        with self._lock:
            if self._vectorizer is None or self._matrix is None or self._ids.size == 0:
                return []
            query_vector = cast(csr_matrix, self._vectorizer.transform([query]))
            if query_vector.nnz == 0:
                return []
            scores = np.asarray((self._matrix @ query_vector.T).toarray()).ravel()
            positions = np.arange(self._ids.size)
            if allowed_ids is not None:
                positions = positions[np.isin(self._ids, np.fromiter(allowed_ids, dtype=np.int64))]
            if positions.size == 0:
                return []
            candidate_scores = scores[positions]
            positive = candidate_scores > 0
            positions = positions[positive]
            candidate_scores = candidate_scores[positive]
            if positions.size == 0:
                return []
            take = min(max(1, limit), int(candidate_scores.size))
            top = np.argpartition(-candidate_scores, take - 1)[:take]
            top = top[np.argsort(-candidate_scores[top])]
            return [(int(self._ids[positions[p]]), float(candidate_scores[p])) for p in top]


lexical_index = LexicalIndex()
