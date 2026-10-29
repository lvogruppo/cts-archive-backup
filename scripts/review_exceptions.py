from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import ArchiveSource, Document, Page, SessionLocal  # noqa: E402
from app.retrieval.embeddings import CHUNKING_VERSION  # noqa: E402


def _source_documents(db) -> tuple[dict[str, list[Document]], dict[int, Document]]:
    by_source: dict[str, list[Document]] = {}
    by_id: dict[int, Document] = {}
    for document in db.query(Document).all():
        by_id[document.id] = document
        if document.drive_file_id:
            root_id = document.drive_file_id.split("#", 1)[0]
            by_source.setdefault(root_id, []).append(document)
    return by_source, by_id


def list_exceptions(db) -> dict[str, Any]:
    by_source, by_id = _source_documents(db)
    source_errors: list[dict[str, Any]] = []
    metadata_missing: list[dict[str, Any]] = []
    incomplete_documents: list[dict[str, Any]] = []
    relevant_document_ids: set[int] = set()

    for source in db.query(ArchiveSource).order_by(ArchiveSource.title.asc()).all():
        if source.meeting_number is None or source.meeting_date is None:
            metadata_missing.append(
                {
                    "title": source.title,
                    "drive_url": source.drive_url,
                    "meeting_number": source.meeting_number,
                    "meeting_date": source.meeting_date.isoformat() if source.meeting_date else None,
                }
            )
        if not source.download_verified or source.download_error or not source.drive_md5:
            source_errors.append(
                {
                    "title": source.title,
                    "drive_url": source.drive_url,
                    "error": source.download_error or "Download o checksum non verificato.",
                }
            )
            continue

        candidates = [
            document
            for document in by_source.get(source.drive_file_id, [])
            if document.drive_md5 == source.drive_md5
        ]
        if not candidates:
            incomplete_documents.append(
                {"title": source.title, "drive_url": source.drive_url, "status": "non_importato"}
            )
            continue
        candidates.sort(key=lambda document: document.id, reverse=True)
        current = candidates[0]
        canonical = by_id.get(current.duplicate_of_id or -1) if current.status == "duplicate" else current
        if (
            canonical is None
            or canonical.status != "processed"
            or canonical.ingestion_version != CHUNKING_VERSION
            or canonical.processed_pages != canonical.total_pages
        ):
            incomplete_documents.append(
                {
                    "title": source.title,
                    "drive_url": source.drive_url,
                    "status": current.status,
                    "error": current.error_summary,
                    "indexed_pages": current.processed_pages,
                    "total_pages": current.total_pages,
                }
            )
            if canonical is not None:
                relevant_document_ids.add(canonical.id)
            continue
        relevant_document_ids.add(canonical.id)

    page_exceptions: list[dict[str, Any]] = []
    seen_pages: set[tuple[int, int]] = set()
    if relevant_document_ids:
        rows = (
            db.query(Page, Document)
            .join(Document, Document.id == Page.document_id)
            .filter(Document.id.in_(relevant_document_ids))
            .order_by(Document.title.asc(), Page.page_number.asc())
            .all()
        )
        for page, document in rows:
            trusted_processed = page.status == "processed" and page.ocr_status in {"not-needed", "success"}
            verified_empty = page.status == "empty" and page.ocr_status == "verified-empty"
            needs_review = not (trusted_processed or verified_empty)
            if not needs_review:
                continue
            key = (int(document.id), int(page.page_number))
            if key in seen_pages:
                continue
            seen_pages.add(key)
            page_exceptions.append(
                {
                    "document_id": document.id,
                    "title": document.title,
                    "drive_url": document.drive_url,
                    "page": page.page_number,
                    "status": page.status,
                    "ocr_status": page.ocr_status,
                    "ocr_used": page.ocr_used,
                    "characters_extracted": len((page.content or "").strip()),
                }
            )

    return {
        "chunking_version_required": CHUNKING_VERSION,
        "expected_sources": db.query(ArchiveSource).count(),
        "source_download_or_checksum_errors": source_errors,
        "documents_incomplete_or_not_imported": incomplete_documents,
        "pages_requiring_review": page_exceptions,
        "sources_with_unidentified_meeting_metadata": metadata_missing,
        "summary": {
            "source_errors": len(source_errors),
            "incomplete_documents": len(incomplete_documents),
            "pages_requiring_review": len(page_exceptions),
            "sources_with_unidentified_meeting_metadata": len(metadata_missing),
        },
    }


def verify_empty_page(db, document_id: int, page_number: int) -> None:
    page = (
        db.query(Page)
        .filter(Page.document_id == document_id, Page.page_number == page_number)
        .with_for_update()
        .first()
    )
    if page is None:
        raise ValueError("Pagina non trovata.")
    if page.status != "empty" or page.ocr_status != "empty" or (page.content or "").strip():
        raise ValueError("È possibile verificare come vuota solo una pagina senza testo con OCR status 'empty'.")
    page.ocr_status = "verified-empty"
    db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Elenca le eccezioni dell'archivio; la verifica di pagine vuote richiede controllo visivo umano."
    )
    parser.add_argument(
        "--verify-empty",
        nargs=2,
        type=int,
        metavar=("DOCUMENT_ID", "NUMERO_PAGINA"),
        help="Dopo aver aperto il PDF originale e controllato la pagina, marcarla come vuota verificata.",
    )
    parser.add_argument(
        "--confirm-visual-review",
        action="store_true",
        help="Obbligatorio: conferma che la pagina originale è stata ispezionata visivamente.",
    )
    args = parser.parse_args()
    if SessionLocal is None:
        print("DATABASE_URL non configurato.", file=sys.stderr)
        return 2

    with SessionLocal() as db:
        if args.verify_empty:
            if not args.confirm_visual_review:
                print("Per la modifica è obbligatorio --confirm-visual-review.", file=sys.stderr)
                return 2
            try:
                verify_empty_page(db, args.verify_empty[0], args.verify_empty[1])
            except ValueError as exc:
                db.rollback()
                print(str(exc), file=sys.stderr)
                return 2
            print(
                json.dumps(
                    {"verified_empty": True, "document_id": args.verify_empty[0], "page": args.verify_empty[1]},
                    ensure_ascii=False,
                )
            )
            return 0
        print(json.dumps(list_exceptions(db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
