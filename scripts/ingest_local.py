from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import ArchiveSource, Chunk, Document, Page, SessionLocal, create_schema  # noqa: E402
from app.retrieval.embeddings import CHUNKING_VERSION, EMBEDDING_DIM, MODEL_NAME, embed_texts  # noqa: E402
from app.services.coverage import MINIMUM_EXPECTED_SOURCE_COUNT  # noqa: E402
from app.services.ingest import chunk_page, content_hash, extract_pdf_pages  # noqa: E402


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
    if (
        document
        and document.file_hash == digest
        and document.status in {"processed", "duplicate"}
        and document.ingestion_version == CHUNKING_VERSION
    ):
        return f"già presente: {path.name}"

    if document and document.file_hash and document.file_hash != digest:
        # Drive may retain the same file ID when a new revision is uploaded.
        # Keep the older indexed version and store this revision under a stable hash suffix.
        drive_id = f"{drive_id}#v-{digest[:12]}"
        document = db.query(Document).filter(Document.drive_file_id == str(drive_id)).first()

    needs_reindex = document is None or document.ingestion_version != CHUNKING_VERSION
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
        document.title = str(metadata.get("title") or path.name)
        document.drive_url = metadata.get("drive_url")
        document.meeting_number = metadata.get("meeting_number")
        document.meeting_date = parse_date(metadata.get("meeting_date"))
        document.file_hash = digest
        document.drive_size = int(metadata.get("drive_size") or 0) or None
        document.drive_md5 = metadata.get("drive_md5")
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
    document.title = str(metadata.get("title") or path.name)
    document.drive_url = metadata.get("drive_url")
    document.meeting_number = metadata.get("meeting_number")
    document.meeting_date = parse_date(metadata.get("meeting_date"))
    document.file_hash = digest
    document.drive_size = int(metadata.get("drive_size") or 0) or None
    document.drive_md5 = metadata.get("drive_md5")
    document.duplicate_of_id = None
    document.modified_time = parse_modified_time(metadata.get("modified_time")) or datetime.utcfromtimestamp(path.stat().st_mtime)
    document.status = "processing"
    document.processed_pages = 0
    if needs_reindex:
        document.ingestion_version = None
    document.error_summary = None
    db.commit()

    try:
        with fitz.open(str(path)) as pdf:
            document.total_pages = pdf.page_count
            db.commit()
        extracted_pages = extract_pdf_pages(path)
    except Exception as exc:
        document.status = "error"
        document.error_summary = f"Lettura PDF non riuscita ({type(exc).__name__})."
        db.commit()
        return f"errore: {path.name}"

    page_errors = 0
    for extracted in extracted_pages:
        page_row = (
            db.query(Page)
            .filter(Page.document_id == document.id, Page.page_number == extracted.page_number)
            .first()
        )
        if page_row and page_row.status in {"processed", "empty"}:
            existing_chunks = db.query(Chunk.id).filter(Chunk.page_id == page_row.id).count()
            if not needs_reindex and (not extracted.content or existing_chunks):
                continue
        if page_row:
            db.query(Chunk).filter(Chunk.page_id == page_row.id).delete(synchronize_session=False)
            page_row.content = extracted.content
            page_row.text_hash = content_hash(extracted.content)
            page_row.ocr_used = extracted.ocr_used
            page_row.ocr_status = extracted.ocr_status
            page_row.status = extracted.status
        else:
            page_row = Page(
                document_id=document.id,
                page_number=extracted.page_number,
                content=extracted.content,
                text_hash=content_hash(extracted.content),
                ocr_used=extracted.ocr_used,
                ocr_status=extracted.ocr_status,
                status=extracted.status,
            )
            db.add(page_row)
        db.flush()

        chunks = chunk_page(extracted.content)
        if chunks:
            vectors = embed_texts([chunk[2] for chunk in chunks], "passage")
            for ordinal, ((start, end, content), vector) in enumerate(zip(chunks, vectors)):
                db.add(
                    Chunk(
                        document_id=document.id,
                        page_id=page_row.id,
                        chunk_number=ordinal,
                        char_start=start,
                        char_end=end,
                        content=content,
                        embedding=vector.astype("float32").tobytes(),
                        embedding_model=MODEL_NAME,
                        embedding_dim=EMBEDDING_DIM,
                    )
                )
        if extracted.status == "error":
            page_errors += 1
        document.processed_pages = extracted.page_number
        db.commit()
        if extracted.page_number % 25 == 0 or extracted.page_number == document.total_pages:
            print(f"pagine: {path.name} {extracted.page_number}/{document.total_pages}", flush=True)

    document.status = "partial" if page_errors else "processed"
    document.ingestion_version = CHUNKING_VERSION if not page_errors else None
    if page_errors:
        document.error_summary = f"OCR/estrazione da verificare su {page_errors} pagine."
    db.commit()
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
    print(json.dumps({"pdf": len(files), "bytes": total_bytes, "dry_run": args.dry_run}, ensure_ascii=False))
    if args.dry_run:
        return 0
    if SessionLocal is None:
        print("DATABASE_URL non configurato.", file=sys.stderr)
        return 2
    create_schema()
    errors = 0
    with SessionLocal() as db:
        sync_archive_inventory(db, metadata)
        for path in files:
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
    print(json.dumps({"pdf_seen": len(files), "missing_or_unverified": missing, "processing_errors": errors}, ensure_ascii=False), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
