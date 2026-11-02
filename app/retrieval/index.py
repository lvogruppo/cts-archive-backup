from __future__ import annotations

import threading

import faiss
import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import Chunk
from app.retrieval.embeddings import EMBEDDING_DIM


class VectorIndex:
    """Indice derivato e volatile; MySQL resta la fonte persistente dei vettori."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._signature: tuple[int, int | None] | None = None
        self._index: faiss.Index | None = None
        self._ids = np.empty((0,), dtype=np.int64)
        self._vectors = np.empty((0, EMBEDDING_DIM), dtype=np.float32)

    def _refresh_if_changed(self, db: Session) -> None:
        count, max_id = db.query(func.count(Chunk.id), func.max(Chunk.id)).filter(
            Chunk.embedding.is_not(None), Chunk.embedding_dim == EMBEDDING_DIM
        ).one()
        signature = (int(count or 0), int(max_id) if max_id is not None else None)
        with self._lock:
            if signature == self._signature:
                return
            rows = (
                db.query(Chunk.id, Chunk.embedding)
                .filter(Chunk.embedding.is_not(None), Chunk.embedding_dim == EMBEDDING_DIM)
                .order_by(Chunk.id.asc())
                .all()
            )
            ids: list[int] = []
            vectors: list[np.ndarray] = []
            for chunk_id, raw in rows:
                if not raw:
                    continue
                vector = np.frombuffer(raw, dtype=np.float32)
                if vector.size != EMBEDDING_DIM:
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
            self._signature = signature

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
