from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

import fitz
from sqlalchemy import func, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import ArchiveSource, Chunk, Document, Page, SessionLocal, create_schema  # noqa: E402
from app.retrieval.embeddings import CHUNKING_VERSION, EMBEDDING_DIM, MODEL_NAME, embed_texts  # noqa: E402
from app.services.coverage import MINIMUM_EXPECTED_SOURCE_COUNT  # noqa: E402
from app.services.ingest import chunk_page, content_hash, iter_pdf_pages  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_modified_time(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def load_metadata(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("Il file metadata deve essere un oggetto JSON indicizzato per nome file.")
    if len(value) < MINIMUM_EXPECTED_SOURCE_COUNT:
        raise ValueError(
            f"Manifesto incompleto: attesi almeno {MINIMUM_EXPECTED_SOURCE_COUNT} PDF CTS, trovati {len(value)}."
        )
    drive_ids = [str(record.get("drive_file_id") or "") for record in value.values() if isinstance(record, dict)]
    if len(drive_ids) != len(value) or any(not drive_id for drive_id in drive_ids):
        raise ValueError("Ogni fonte del manifesto deve avere un drive_file_id valido.")
    if len(set(drive_ids)) != len(drive_ids):
        raise ValueError("Il manifesto contiene drive_file_id duplicati.")
    return value


CHUNK_PERSIST_BATCH_SIZE = 64
ARCHIVE_INGEST_LOCK_NAME = "cts_archive_ingestion_v1"


class IngestionLockUnavailable(RuntimeError):
    pass


@contextmanager
def _archive_ingestion_lock(bind) -> Iterator[None]:
    """Serialize archive runs across processes sharing the managed MySQL database."""
    if bind.dialect.name != "mysql":
        yield
        return

    connection = bind.connect()
    acquired = False
    try:
        value = connection.execute(
            text("SELECT GET_LOCK(:lock_name, 0)"),
            {"lock_name": ARCHIVE_INGEST_LOCK_NAME},
        ).scalar()
        connection.commit()
        if value != 1:
            raise IngestionLockUnavailable("Un'altra ingestione dell'archivio è già attiva.")
        acquired = True
        yield
    finally:
        if acquired:
            try:
                connection.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": ARCHIVE_INGEST_LOCK_NAME},
                )
                connection.commit()
            except Exception:
                connection.rollback()
        connection.close()


def _sync_document_metadata(document: Document, path: Path, metadata: dict[str, Any]) -> None:
    document.title = str(metadata.get("title") or path.name)
    document.drive_url = metadata.get("drive_url")
    document.meeting_number = metadata.get("meeting_number")
    document.meeting_date = parse_date(metadata.get("meeting_date"))
    document.drive_size = int(metadata.get("drive_size") or 0) or None
    document.drive_md5 = metadata.get("drive_md5")
    document.modified_time = parse_modified_time(metadata.get("modified_time")) or datetime.utcfromtimestamp(
        path.stat().st_mtime
    )


def _chunk_batches(
    chunks: Iterable[tuple[int, int, str]], batch_size: int = CHUNK_PERSIST_BATCH_SIZE
) -> Iterator[list[tuple[int, int, str]]]:
    if batch_size < 1:
        raise ValueError("batch_size deve essere positivo")
    batch: list[tuple[int, int, str]] = []
    for chunk in chunks:
        batch.append(chunk)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _completed_page_numbers(db, document_id: int) -> set[int]:
    pages = db.query(Page).filter(Page.document_id == document_id).all()
    counts = {
        int(page_id): int(count)
        for page_id, count in db.query(Chunk.page_id, func.count(Chunk.id))
        .filter(Chunk.document_id == document_id)
        .group_by(Chunk.page_id)
        .all()
    }
    completed: set[int] = set()
    for page in pages:
        actual_count = counts.get(page.id, 0)
        if page.indexed_chunk_count is None or actual_count != page.indexed_chunk_count:
            continue
        if page.status == "empty" and page.ocr_status == "verified-empty" and actual_count == 0:
            completed.add(page.page_number)
        elif (
            page.status == "processed"
            and page.ocr_status in {"not-needed", "success"}
            and page.content.strip()
            and actual_count > 0
        ):
            completed.add(page.page_number)
    return completed


def _consecutive_completed_pages(completed: set[int]) -> int:
    count = 0
    while count + 1 in completed:
        count += 1
    return count


def _select_pdf_batch(files: list[Path], start_index: int, max_files: int | None) -> list[Path]:
    if start_index < 0:
        raise ValueError("--start-index deve essere maggiore o uguale a zero")
    if max_files is not None and max_files < 1:
        raise ValueError("--max-files deve essere almeno 1")
    if start_index > len(files):
        raise ValueError(f"--start-index supera il numero di PDF verificati ({len(files)}).")
    end_index = None if max_files is None else start_index + max_files
    return files[start_index:end_index]


def sync_archive_inventory(db, metadata: dict[str, dict[str, Any]]) -> int:
    """Merge the complete expected Drive inventory; never remove previously expected sources."""
    for local_name, record in metadata.items():
        drive_id = str(record["drive_file_id"])
        source = db.query(ArchiveSource).filter(ArchiveSource.drive_file_id == drive_id).first()
        if source is None:
            source = ArchiveSource(drive_file_id=drive_id, title=str(record.get("title") or local_name))
            db.add(source)
        source.title = str(record.get("title") or local_name)
        source.drive_url = record.get("drive_url")
        source.drive_size = int(record.get("drive_size") or 0) or None
        source.drive_md5 = record.get("drive_md5")
        source.meeting_number = record.get("meeting_number")
        source.meeting_date = parse_date(record.get("meeting_date"))
        source.modified_time = parse_modified_time(record.get("modified_time"))
        source.download_verified = bool(record.get("download_verified")) and not record.get("download_error")
        source.download_error = str(record.get("download_error") or "")[:800] or None
    db.commit()
    count = db.query(ArchiveSource).count()
    print(f"fonti attese nel manifesto: {count}", flush=True)
    return count


def process_file(db, path: Path, metadata: dict) -> str:
    digest = sha256_file(path)
    drive_id = metadata.get("drive_file_id")
    document = None
    if drive_id:
        document = db.query(Document).filter(Document.drive_file_id == str(drive_id)).first()
    if document and document.file_hash and document.file_hash != digest:
        # Drive may retain the same file ID when a new revision is uploaded.
        # Keep the older indexed version and store this revision under a stable hash suffix.
        drive_id = f"{drive_id}#v-{digest[:12]}"
        document = db.query(Document).filter(Document.drive_file_id == str(drive_id)).first()

    if document is not None:
        _sync_document_metadata(document, path, metadata)
    document_pages_complete = bool(
        document
        and document.status == "processed"
        and document.total_pages > 0
        and len(_completed_page_numbers(db, int(document.id))) == document.total_pages
    )
    if (
        document
        and document.file_hash == digest
        and document.ingestion_version == CHUNKING_VERSION
        and (document.status == "duplicate" or document_pages_complete)
    ):
        db.commit()
        return f"già presente: {path.name}"

    needs_full_reindex = bool(
        document is None
        or document.file_hash != digest
        or document.ingestion_version != CHUNKING_VERSION
    )
    canonical = (
        db.query(Document)
        .filter(
            Document.file_hash == digest,
            Document.status == "processed",
            Document.ingestion_version == CHUNKING_VERSION,
        )
        .first()
    )
    if canonical and (not document or document.id != canonical.id):
        if document is None:
            document = Document(
                drive_file_id=str(drive_id) if drive_id else None,
                title=str(metadata.get("title") or path.name),
            )
            db.add(document)
        else:
            db.query(Chunk).filter(Chunk.document_id == document.id).delete(synchronize_session=False)
            db.query(Page).filter(Page.document_id == document.id).delete(synchronize_session=False)
        _sync_document_metadata(document, path, metadata)
        document.file_hash = digest
        document.duplicate_of_id = canonical.id
        document.status = "duplicate"
        document.ingestion_version = CHUNKING_VERSION
        document.error_summary = f"Contenuto identico al documento CTS #{canonical.id}; provenienza conservata."
        db.commit()
        return f"duplicato esatto del documento #{canonical.id}: {path.name}"

    if document is None:
        document = Document(
            drive_file_id=str(drive_id) if drive_id else None,
            title=str(metadata.get("title") or path.name),
        )
        db.add(document)
    previous_total_pages = int(document.total_pages or 0)
    _sync_document_metadata(document, path, metadata)
    document.file_hash = digest
    document.duplicate_of_id = None
    document.status = "processing"
    if needs_full_reindex:
        if document.id is not None:
            db.query(Chunk).filter(Chunk.document_id == document.id).delete(synchronize_session=False)
            db.query(Page).filter(Page.document_id == document.id).delete(synchronize_session=False)
        document.processed_pages = 0
    document.ingestion_version = CHUNKING_VERSION
    document.error_summary = None
    db.commit()

    try:
        with fitz.open(str(path)) as pdf:
            page_count = pdf.page_count
    except Exception as exc:
        document.status = "error"
        document.error_summary = f"Lettura PDF non riuscita ({type(exc).__name__})."
        db.commit()
        return f"errore: {path.name}"

    if not needs_full_reindex and previous_total_pages not in {0, page_count}:
        db.query(Chunk).filter(Chunk.document_id == document.id).delete(synchronize_session=False)
        db.query(Page).filter(Page.document_id == document.id).delete(synchronize_session=False)
        needs_full_reindex = True
    if not needs_full_reindex:
        extra_page_ids = [
            int(page_id)
            for (page_id,) in db.query(Page.id)
            .filter(Page.document_id == document.id)
            .filter((Page.page_number < 1) | (Page.page_number > page_count))
            .all()
        ]
        if extra_page_ids:
            db.query(Chunk).filter(Chunk.page_id.in_(extra_page_ids)).delete(synchronize_session=False)
            db.query(Page).filter(Page.id.in_(extra_page_ids)).delete(synchronize_session=False)
    document.total_pages = page_count
    if document.id is None:
        raise RuntimeError("ID documento non persistito prima della ripresa pagine.")
    document_id = int(document.id)
    completed_pages = set() if needs_full_reindex else _completed_page_numbers(db, document_id)
    document.processed_pages = _consecutive_completed_pages(completed_pages)
    db.commit()

    page_errors = 0
    for extracted in iter_pdf_pages(path, skip_page_numbers=completed_pages):
        if not needs_full_reindex and extracted.page_number in completed_pages:
            continue
        page_row = (
            db.query(Page)
            .filter(Page.document_id == document.id, Page.page_number == extracted.page_number)
            .first()
        )
        if page_row:
            db.query(Chunk).filter(Chunk.page_id == page_row.id).delete(synchronize_session=False)
            page_row.content = extracted.content
            page_row.text_hash = content_hash(extracted.content)
            page_row.ocr_used = extracted.ocr_used
            page_row.ocr_status = extracted.ocr_status
            page_row.status = "processing"
            page_row.indexed_chunk_count = 0
        else:
            page_row = Page(
                document_id=document.id,
                page_number=extracted.page_number,
                content=extracted.content,
                text_hash=content_hash(extracted.content),
                ocr_used=extracted.ocr_used,
                ocr_status=extracted.ocr_status,
                status="processing",
                indexed_chunk_count=0,
            )
            db.add(page_row)
        db.flush()

        ordinal = 0
        for chunk_batch in _chunk_batches(chunk_page(extracted.content)):
            vectors = embed_texts([chunk[2] for chunk in chunk_batch], "passage")
            if len(vectors) != len(chunk_batch):
                raise ValueError("Numero di embedding diverso dal numero di chunk.")
            db.add_all(
                [
                    Chunk(
                        document_id=document.id,
                        page_id=page_row.id,
                        chunk_number=ordinal + offset,
                        char_start=start,
                        char_end=end,
                        content=content,
                        embedding=vector.astype("float32").tobytes(),
                        embedding_model=MODEL_NAME,
                        embedding_dim=EMBEDDING_DIM,
                    )
                    for offset, ((start, end, content), vector) in enumerate(zip(chunk_batch, vectors))
                ]
            )
            ordinal += len(chunk_batch)
            # Commit each bounded batch so the page never accumulates all chunk text,
            # vectors and ORM objects in memory at once. The page remains processing
            # until every chunk batch has been persisted successfully.
            db.commit()
        final_status = extracted.status
        if final_status == "processed" and extracted.content.strip() and ordinal == 0:
            final_status = "error"
        page_row.status = final_status
        page_row.indexed_chunk_count = ordinal
        if final_status == "error":
            page_errors += 1
            completed_pages.discard(extracted.page_number)
        elif final_status == "empty" and extracted.ocr_status == "verified-empty" and ordinal == 0:
            completed_pages.add(extracted.page_number)
        elif (
            final_status == "processed"
            and extracted.ocr_status in {"not-needed", "success"}
            and extracted.content.strip()
            and ordinal > 0
        ):
            completed_pages.add(extracted.page_number)
        else:
            completed_pages.discard(extracted.page_number)
        document.processed_pages = _consecutive_completed_pages(completed_pages)
        db.commit()
        if extracted.page_number % 25 == 0 or extracted.page_number == document.total_pages:
            print(f"pagine: {path.name} {extracted.page_number}/{document.total_pages}", flush=True)

    expected_pages = set(range(1, document.total_pages + 1))
    recorded_pages = {
        int(page_number)
        for (page_number,) in db.query(Page.page_number).filter(Page.document_id == document.id).all()
    }
    is_complete = bool(expected_pages) and recorded_pages == expected_pages and expected_pages.issubset(completed_pages)
    document.status = "processed" if is_complete and page_errors == 0 else "partial"
    document.ingestion_version = CHUNKING_VERSION
    if document.status == "partial":
        missing_pages = len(expected_pages - completed_pages)
        document.error_summary = (
            f"Indicizzazione incompleta: {page_errors} pagine in errore, {missing_pages} pagine non complete."
        )
    else:
        document.error_summary = None
    db.commit()
    if document.status == "partial":
        return f"parziale: {path.name} ({document.processed_pages}/{document.total_pages} pagine verificate)"
    return f"indicizzato: {path.name} ({document.total_pages} pagine)"


def record_missing_sources(db, metadata: dict, available_names: set[str]) -> int:
    """Keep inaccessible/unverified Drive PDFs visible as incomplete coverage."""
    recorded = 0
    for local_name, record in metadata.items():
        drive_id = record.get("drive_file_id")
        if not drive_id or (local_name in available_names and record.get("download_verified") is True):
            continue
        source_id = str(drive_id)
        document = db.query(Document).filter(Document.drive_file_id == source_id).first()
        if document and document.status in {"processed", "duplicate", "partial"}:
            same_revision = not record.get("drive_md5") or document.drive_md5 == record.get("drive_md5")
            if same_revision:
                continue
            revision = str(record.get("drive_md5") or record.get("modified_time") or "unknown")
            source_id = f"{source_id}#missing-{revision[:24]}"
            document = db.query(Document).filter(Document.drive_file_id == source_id).first()
        if document is None:
            document = Document(
                drive_file_id=source_id,
                title=str(record.get("title") or local_name),
            )
            db.add(document)
        document.title = str(record.get("title") or local_name)
        document.drive_url = record.get("drive_url")
        document.drive_size = int(record.get("drive_size") or 0) or None
        document.drive_md5 = record.get("drive_md5")
        document.meeting_number = record.get("meeting_number")
        document.meeting_date = parse_date(record.get("meeting_date"))
        document.modified_time = parse_modified_time(record.get("modified_time"))
        document.status = "error"
        detail = record.get("download_error") or "PDF assente o checksum non verificato nella cartella di ingestione."
        document.error_summary = f"Download/lettura incompleta: {str(detail)[:800]}"
        db.commit()
        recorded += 1
        print(f"fonte non disponibile: {local_name}", flush=True)
    return recorded


def main() -> int:
    parser = argparse.ArgumentParser(description="Indicizza PDF CTS in MySQL con E5-small e OCR italiano.")
    parser.add_argument("--source", required=True, type=Path, help="Cartella locale con i PDF")
    parser.add_argument("--metadata-json", required=True, type=Path, help="Manifesto completo: nomefile locale -> metadati Drive/verbale")
    parser.add_argument("--dry-run", action="store_true", help="Conta file e dimensioni senza scrivere nel database")
    parser.add_argument("--start-index", type=int, default=0, help="Indice iniziale inclusivo nella lista dei PDF verificati e ordinati")
    parser.add_argument("--max-files", type=int, default=None, help="Numero massimo di PDF da elaborare in questa esecuzione")
    args = parser.parse_args()
    metadata = load_metadata(args.metadata_json)
    files = []
    for path in sorted(args.source.rglob("*.pdf")):
        if not path.is_file() or path.name.endswith(".download.pdf") or path.name not in metadata:
            continue
        record = metadata[path.name]
        if record.get("download_verified") is not True or record.get("download_error"):
            continue
        expected_size = int(record.get("drive_size") or 0)
        expected_md5 = str(record.get("drive_md5") or "").lower()
        try:
            matches = (
                expected_size > 0
                and expected_md5
                and path.stat().st_size == expected_size
                and md5_file(path).lower() == expected_md5
            )
        except OSError:
            matches = False
        if not matches:
            record["download_verified"] = False
            record["download_error"] = "Checksum MD5 o dimensione locale non corrispondente al manifesto Drive."
            print(f"file escluso: checksum/dimensione non corrispondente ({path.name})", file=sys.stderr, flush=True)
            continue
        files.append(path)
    total_bytes = sum(path.stat().st_size for path in files)
    try:
        selected_files = _select_pdf_batch(files, args.start_index, args.max_files)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "pdf_verified": len(files),
                "pdf_selected": len(selected_files),
                "start_index": args.start_index,
                "bytes_verified": total_bytes,
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
        )
    )
    if args.dry_run:
        return 0
    if SessionLocal is None:
        print("DATABASE_URL non configurato.", file=sys.stderr)
        return 2
    create_schema()
    errors = 0
    with SessionLocal() as db:
        try:
            with _archive_ingestion_lock(db.get_bind()):
                sync_archive_inventory(db, metadata)
                for path in selected_files:
                    record = metadata.get(path.name, {})
                    try:
                        result = process_file(db, path, record)
                        print(result, flush=True)
                        if result.startswith("errore:"):
                            errors += 1
                    except Exception as exc:
                        db.rollback()
                        print(f"errore: {path.name} ({type(exc).__name__})", file=sys.stderr, flush=True)
                        errors += 1
                missing = record_missing_sources(db, metadata, {path.name for path in files})
        except IngestionLockUnavailable as exc:
            print(f"ingestione non avviata: {exc}", file=sys.stderr, flush=True)
            return 2
    print(
        json.dumps(
            {
                "pdf_verified": len(files),
                "pdf_selected": len(selected_files),
                "start_index": args.start_index,
                "missing_or_unverified": missing,
                "processing_errors": errors,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
