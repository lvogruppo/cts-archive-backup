from __future__ import annotations

import threading

import faiss
import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import Chunk
from app.retrieval.embeddings import EMBEDDING_DIM


class VectorIndex:
    """Indice derivato e volatile; MySQL resta la fonte persistente dei vettori."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._signature: tuple[int, int | None, int, int] | None = None
        self._bind: object | None = None
        self._index: faiss.Index | None = None
        self._ids = np.empty((0,), dtype=np.int64)
        self._vectors = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        self._invalid_document_ids: set[int] = set()

    @staticmethod
    def _database_signature(db: Session) -> tuple[int, int | None, int, int]:
        # Digest each stored blob in MySQL so an in-place update invalidates the
        # in-memory index even when row count and max ID stay unchanged.
        row = db.execute(
            text(
                """
                SELECT COUNT(*), MAX(id),
                       COALESCE(SUM(fingerprint), 0),
                       COALESCE(BIT_XOR(fingerprint), 0)
                FROM (
                    SELECT id, CRC32(CONCAT(id, ':', SHA2(embedding, 256))) AS fingerprint
                    FROM cts_chunks
                    WHERE embedding IS NOT NULL
                ) AS vector_hashes
                """
            )
        ).one()
        return (
            int(row[0] or 0),
            int(row[1]) if row[1] is not None else None,
            int(row[2] or 0),
            int(row[3] or 0),
        )

    def _refresh_if_changed(self, db: Session) -> None:
        signature = self._database_signature(db)
        bind = db.get_bind()
        with self._lock:
            if bind is self._bind and signature == self._signature:
                return
            rows = (
                db.query(Chunk.id, Chunk.document_id, Chunk.embedding)
                .filter(Chunk.embedding.is_not(None))
                .order_by(Chunk.id.asc())
                .all()
            )
            ids: list[int] = []
            vectors: list[np.ndarray] = []
            invalid_document_ids: set[int] = set()
            for chunk_id, document_id, raw in rows:
                if not raw or len(raw) != EMBEDDING_DIM * np.dtype(np.float32).itemsize:
                    invalid_document_ids.add(int(document_id))
                    continue
                vector = np.frombuffer(raw, dtype=np.float32)
                if vector.size != EMBEDDING_DIM or not np.isfinite(vector).all():
                    invalid_document_ids.add(int(document_id))
                    continue
                ids.append(int(chunk_id))
                vectors.append(vector.copy())
            matrix = np.vstack(vectors).astype(np.float32, copy=False) if vectors else np.empty((0, EMBEDDING_DIM), dtype=np.float32)
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(EMBEDDING_DIM))
            if ids:
                index.add_with_ids(matrix, np.asarray(ids, dtype=np.int64))
            self._index = index
            self._ids = np.asarray(ids, dtype=np.int64)
            self._vectors = matrix
            self._invalid_document_ids = invalid_document_ids
            self._signature = signature
            self._bind = bind

    def invalid_document_ids(self, db: Session) -> set[int]:
        self._refresh_if_changed(db)
        with self._lock:
            return set(self._invalid_document_ids)

    def size(self, db: Session) -> int:
        self._refresh_if_changed(db)
        return int(self._ids.size)

    def search(
        self,
        db: Session,
        query_vector: np.ndarray,
        limit: int,
        allowed_ids: set[int] | None = None,
    ) -> list[tuple[int, float]]:
        self._refresh_if_changed(db)
        with self._lock:
            if self._index is None or self._ids.size == 0:
                return []
            if allowed_ids is None:
                k = min(max(1, limit), int(self._ids.size))
                scores, labels = self._index.search(query_vector.astype(np.float32), k)
                return [(int(i), float(s)) for i, s in zip(labels[0], scores[0]) if int(i) >= 0]

            positions = np.fromiter(
                (i for i, chunk_id in enumerate(self._ids) if int(chunk_id) in allowed_ids),
                dtype=np.int64,
            )
            if positions.size == 0:
                return []
            scores = self._vectors[positions] @ query_vector[0]
            take = min(max(1, limit), int(scores.size))
            top_positions = np.argpartition(-scores, take - 1)[:take]
            top_positions = top_positions[np.argsort(-scores[top_positions])]
            return [
                (int(self._ids[positions[p]]), float(scores[p]))
                for p in top_positions
            ]


vector_index = VectorIndex()
