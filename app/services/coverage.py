from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.database import ArchiveSource, Chunk, Document, Page
from app.retrieval.embeddings import CHUNKING_VERSION, EMBEDDING_DIM, MODEL_NAME

MINIMUM_EXPECTED_SOURCE_COUNT = 433


@dataclass(frozen=True)
class CoverageReport:
    complete: bool
    expected_sources: int
    complete_sources: int
    eligible_document_ids: frozenset[int]


def archive_coverage(db: Session) -> CoverageReport:
    """Require the complete expected Drive inventory and verified, indexed pages."""
    sources = db.query(ArchiveSource).all()
    documents = db.query(Document).all()
    if not sources or len(sources) < MINIMUM_EXPECTED_SOURCE_COUNT:
        return CoverageReport(False, len(sources), 0, frozenset())

    docs_by_id: dict[int, Document] = {document.id: document for document in documents}
    docs_by_source: dict[str, list[Document]] = defaultdict(list)
    for document in documents:
        if document.drive_file_id:
            docs_by_source[document.drive_file_id.split("#", 1)[0]].append(document)

    pages_by_document: dict[int, list[Any]] = defaultdict(list)
    page_rows = db.query(Page.id, Page.document_id, Page.page_number, Page.status, Page.ocr_status).all()
    for row in page_rows:
        pages_by_document[row.document_id].append(row)

    chunk_stats: dict[int, tuple[int, int, int, int]] = {}
    for row in db.query(
        Chunk.page_id,
        func.count(Chunk.id),
        func.sum(case((Chunk.embedding.is_not(None), 1), else_=0)),
        func.sum(case((Chunk.embedding_model == MODEL_NAME, 1), else_=0)),
        func.sum(case((Chunk.embedding_dim == EMBEDDING_DIM, 1), else_=0)),
    ).group_by(Chunk.page_id).all():
        chunk_stats[int(row[0])] = tuple(int(value or 0) for value in row[1:5])  # type: ignore[assignment]

    memo: dict[int, bool] = {}

    def document_is_complete(document: Document, visiting: set[int] | None = None) -> bool:
        if document.id in memo:
            return memo[document.id]
        visited = set() if visiting is None else visiting
        if document.id in visited:
            return False
        visited.add(document.id)

        if document.status == "duplicate":
            canonical = docs_by_id.get(document.duplicate_of_id or -1)
            result = bool(
                canonical
                and canonical.status == "processed"
                and canonical.file_hash
                and canonical.file_hash == document.file_hash
                and document_is_complete(canonical, visited)
            )
            memo[document.id] = result
            return result

        if (
            document.status != "processed"
            or document.ingestion_version != CHUNKING_VERSION
            or document.total_pages < 1
            or document.processed_pages != document.total_pages
        ):
            memo[document.id] = False
            return False

        pages = pages_by_document.get(document.id, [])
        if len(pages) != document.total_pages or sorted(page.page_number for page in pages) != list(range(1, document.total_pages + 1)):
            memo[document.id] = False
            return False
        for page in pages:
            if page.status == "processed":
                if page.ocr_status in {"error", "empty"}:
                    memo[document.id] = False
                    return False
                total, embedded, correct_model, correct_dim = chunk_stats.get(page.id, (0, 0, 0, 0))
                if total < 1 or total != embedded or total != correct_model or total != correct_dim:
                    memo[document.id] = False
                    return False
            elif page.status == "empty" and page.ocr_status == "verified-empty":
                # Empty pages count only after an explicit human review of the OCR result.
                continue
            else:
                memo[document.id] = False
                return False

        memo[document.id] = True
        return True

    source_by_id = {source.drive_file_id: source for source in sources}

    def current_canonical(document: Document | None) -> Document | None:
        if (
            document is None
            or document.status != "processed"
            or document.ingestion_version != CHUNKING_VERSION
            or not document.drive_file_id
        ):
            return None
        source = source_by_id.get(document.drive_file_id.split("#", 1)[0])
        if not source or not source.download_verified or source.download_error or not source.drive_md5:
            return None
        return document if document.drive_md5 == source.drive_md5 else None

    complete_sources = 0
    eligible_document_ids: set[int] = set()
    for source in sources:
        if not source.download_verified or source.download_error or not source.drive_md5:
            continue
        candidates = docs_by_source.get(source.drive_file_id, [])
        candidates = [document for document in candidates if document.drive_md5 == source.drive_md5]
        resolved_id: int | None = None
        for candidate in candidates:
            if not document_is_complete(candidate):
                continue
            canonical = docs_by_id.get(candidate.duplicate_of_id or -1) if candidate.status == "duplicate" else candidate
            current = current_canonical(canonical)
            if current and document_is_complete(current):
                resolved_id = current.id
                break
        if resolved_id is not None:
            complete_sources += 1
            eligible_document_ids.add(resolved_id)

    complete = complete_sources == len(sources)
    return CoverageReport(complete, len(sources), complete_sources, frozenset(eligible_document_ids))
