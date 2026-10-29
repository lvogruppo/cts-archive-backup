from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.database import Chunk, Document, Page
from app.retrieval.embeddings import embed_texts
from app.retrieval.index import vector_index
from app.retrieval.lexical import lexical_index


def _document_filter(db: Session, date_from: str | None, date_to: str | None, meeting_number: str | None) -> set[int] | None:
    if not any((date_from, date_to, meeting_number)):
        return None
    query = db.query(Chunk.id).join(Document, Document.id == Chunk.document_id)
    if date_from:
        query = query.filter(Document.meeting_date >= date.fromisoformat(date_from))
    if date_to:
        query = query.filter(Document.meeting_date <= date.fromisoformat(date_to))
    if meeting_number:
        query = query.filter(Document.meeting_number == meeting_number.strip())
    return {int(row[0]) for row in query.all()}


def search_chunks(
    db: Session,
    query: str,
    eligible_document_ids: set[int],
    limit: int = 12,
    date_from: str | None = None,
    date_to: str | None = None,
    meeting_number: str | None = None,
    per_document_limit: int = 3,
) -> list[dict[str, Any]]:
    query = query.strip()
    if not query or not eligible_document_ids:
        return []
    allowed_ids = {
        int(row[0])
        for row in db.query(Chunk.id).filter(Chunk.document_id.in_(eligible_document_ids)).all()
    }
    filtered_ids = _document_filter(db, date_from, date_to, meeting_number)
    if filtered_ids is not None:
        allowed_ids.intersection_update(filtered_ids)
    if not allowed_ids:
        return []

    # Both in-memory indexes are searched only within complete current-manifest chunks.
    candidate_limit = max(120, limit * 12)
    lexical = lexical_index.search(db, query, limit=candidate_limit, allowed_ids=allowed_ids)
    query_vector = embed_texts([query], "query")
    semantic = vector_index.search(db, query_vector, limit=candidate_limit, allowed_ids=allowed_ids)

    fused: dict[int, float] = {}
    for rank, (chunk_id, _) in enumerate(lexical, start=1):
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (60 + rank)
    for rank, (chunk_id, _) in enumerate(semantic, start=1):
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (60 + rank)
    candidate_ids = [chunk_id for chunk_id, _ in sorted(fused.items(), key=lambda item: item[1], reverse=True)]
    if not candidate_ids:
        return []

    records = (
        db.query(Chunk, Page, Document)
        .join(Page, Page.id == Chunk.page_id)
        .join(Document, Document.id == Chunk.document_id)
        .filter(Chunk.id.in_(candidate_ids), Document.id.in_(eligible_document_ids))
        .all()
    )
    by_id = {int(chunk.id): (chunk, page, document) for chunk, page, document in records}
    ranked_ids: list[int] = []
    document_counts: dict[int, int] = {}
    max_per_document = max(1, per_document_limit)
    for chunk_id in candidate_ids:
        triple = by_id.get(chunk_id)
        if not triple:
            continue
        document_id = int(triple[2].id)
        if document_counts.get(document_id, 0) >= max_per_document:
            continue
        document_counts[document_id] = document_counts.get(document_id, 0) + 1
        ranked_ids.append(chunk_id)
        if len(ranked_ids) >= limit:
            break
    output: list[dict[str, Any]] = []
    for index, chunk_id in enumerate(ranked_ids, start=1):
        triple = by_id.get(chunk_id)
        if not triple:
            continue
        chunk, page, document = triple
        output.append(
            {
                "source_id": f"S{index}",
                "chunk_id": int(chunk.id),
                "title": document.title,
                "meeting_number": document.meeting_number,
                "meeting_date": document.meeting_date.isoformat() if document.meeting_date else None,
                "page": page.page_number,
                "excerpt": chunk.content[:1800],
                "drive_url": document.drive_url,
                "document_status": document.status,
                "ocr_status": page.ocr_status,
                "rank_score": round(float(fused[chunk_id]), 6),
            }
        )
    return output
