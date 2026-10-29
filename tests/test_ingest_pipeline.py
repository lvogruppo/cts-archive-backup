from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import fitz
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, Chunk, Document, Page
from app.retrieval.embeddings import EMBEDDING_DIM, MODEL_NAME
from app.services.ingest import ExtractedPage
import scripts.ingest_local as ingest_local


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_for_sqlite(element, compiler, **kw):
    return "TEXT"


class IngestPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pdf_path = self.root / "verbale.pdf"
        pdf: Any = fitz.open()
        pdf.new_page(width=595, height=842)
        pdf.save(self.pdf_path)
        pdf.close()

        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, class_=Session, expire_on_commit=False)
        self.db = self.Session()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _metadata(self, **overrides):
        record = {
            "drive_file_id": "drive-test",
            "title": "Verbale originale",
            "drive_url": "https://drive.google.com/file/d/original",
            "drive_size": self.pdf_path.stat().st_size,
            "drive_md5": "a" * 32,
            "meeting_number": "12",
            "meeting_date": "2020-04-01",
            "modified_time": "2020-04-02T12:00:00Z",
        }
        record.update(overrides)
        return record

    @staticmethod
    def _fake_embed(texts, kind):
        return np.ones((len(texts), EMBEDDING_DIM), dtype=np.float32)

    def test_pdf_batch_selection_is_stable_and_bounded(self) -> None:
        files = [Path(f"verbale-{index}.pdf") for index in range(7)]
        self.assertEqual(ingest_local._select_pdf_batch(files, 2, 3), files[2:5])
        self.assertEqual(ingest_local._select_pdf_batch(files, 5, None), files[5:])
        with self.assertRaises(ValueError):
            ingest_local._select_pdf_batch(files, -1, 2)

    def test_same_hash_fast_path_updates_manifest_metadata(self) -> None:
        extracted = ExtractedPage(1, "testo della pagina", False, "not-needed", "processed")
        with (
            patch.object(ingest_local, "iter_pdf_pages", return_value=iter([extracted])),
            patch.object(ingest_local, "chunk_page", return_value=iter([(0, 18, "testo della pagina")])),
            patch.object(ingest_local, "embed_texts", side_effect=self._fake_embed),
        ):
            ingest_local.process_file(self.db, self.pdf_path, self._metadata())

        with patch.object(ingest_local, "iter_pdf_pages", side_effect=AssertionError("il fast-path non deve re-estrarre")):
            result = ingest_local.process_file(
                self.db,
                self.pdf_path,
                self._metadata(
                    title="Verbale aggiornato",
                    drive_url="https://drive.google.com/file/d/updated",
                    meeting_number="13",
                    meeting_date="2020-04-02",
                    modified_time="2020-04-03T12:00:00Z",
                ),
            )

        document = self.db.query(Document).filter(Document.drive_file_id == "drive-test").one()
        self.assertTrue(result.startswith("già presente:"))
        self.assertEqual(document.title, "Verbale aggiornato")
        self.assertEqual(document.drive_url, "https://drive.google.com/file/d/updated")
        self.assertEqual(document.meeting_number, "13")
        self.assertEqual(document.meeting_date, date(2020, 4, 2))
        self.assertEqual(document.modified_time, datetime(2020, 4, 3, 12, 0, 0))

    def test_mysql_archive_lock_is_released_after_run(self) -> None:
        calls: list[str] = []

        class FakeConnection:
            closed = False

            def execute(self, statement, _parameters):
                calls.append(str(statement))
                return SimpleNamespace(scalar=lambda: 1)

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                self.closed = True

        connection = FakeConnection()
        bind = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), connect=lambda: connection)
        with ingest_local._archive_ingestion_lock(bind):
            calls.append("ingestion")

        self.assertEqual(len(calls), 3)
        self.assertIn("GET_LOCK", calls[0])
        self.assertEqual(calls[1], "ingestion")
        self.assertIn("RELEASE_LOCK", calls[2])
        self.assertTrue(connection.closed)

    def test_mysql_archive_lock_rejects_a_concurrent_run(self) -> None:
        calls: list[str] = []

        class FakeConnection:
            closed = False

            def execute(self, statement, _parameters):
                calls.append(str(statement))
                return SimpleNamespace(scalar=lambda: 0)

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                self.closed = True

        connection = FakeConnection()
        bind = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), connect=lambda: connection)
        with self.assertRaises(ingest_local.IngestionLockUnavailable):
            with ingest_local._archive_ingestion_lock(bind):
                self.fail("una seconda ingestione non deve entrare nella sezione critica")

        self.assertEqual(len(calls), 1)
        self.assertIn("GET_LOCK", calls[0])
        self.assertTrue(connection.closed)

    def test_large_page_is_embedded_and_persisted_in_bounded_batches(self) -> None:
        total_chunks = 257
        chunks = ((i, i + 1, f"chunk {i}") for i in range(total_chunks))
        batch_sizes: list[int] = []

        def recording_embed(texts, kind):
            batch_sizes.append(len(texts))
            return self._fake_embed(texts, kind)

        extracted = ExtractedPage(1, "pagina sintetica", False, "not-needed", "processed")
        with (
            patch.object(ingest_local, "iter_pdf_pages", return_value=iter([extracted])),
            patch.object(ingest_local, "chunk_page", return_value=chunks),
            patch.object(ingest_local, "embed_texts", side_effect=recording_embed),
        ):
            ingest_local.process_file(self.db, self.pdf_path, self._metadata(drive_file_id="drive-batch"))

        self.assertTrue(batch_sizes)
        self.assertLessEqual(max(batch_sizes), ingest_local.CHUNK_PERSIST_BATCH_SIZE)
        self.assertEqual(sum(batch_sizes), total_chunks)
        self.assertEqual(self.db.query(Chunk).count(), total_chunks)
        self.assertEqual(self.db.query(Page).one().indexed_chunk_count, total_chunks)

    def test_interrupted_current_version_document_rebuilds_partial_page(self) -> None:
        metadata = self._metadata(drive_file_id="drive-resume")
        document = Document(
            drive_file_id="drive-resume",
            title="Verbale parziale",
            drive_url=metadata["drive_url"],
            drive_size=self.pdf_path.stat().st_size,
            drive_md5=metadata["drive_md5"],
            file_hash=ingest_local.sha256_file(self.pdf_path),
            ingestion_version=ingest_local.CHUNKING_VERSION,
            total_pages=1,
            processed_pages=0,
            status="processing",
        )
        self.db.add(document)
        self.db.flush()
        page = Page(
            document_id=document.id,
            page_number=1,
            content="testo precedente parziale",
            text_hash=ingest_local.content_hash("testo precedente parziale"),
            ocr_used=False,
            ocr_status="not-needed",
            status="processing",
            indexed_chunk_count=0,
        )
        self.db.add(page)
        self.db.flush()
        self.db.add(
            Chunk(
                document_id=document.id,
                page_id=page.id,
                chunk_number=0,
                char_start=0,
                char_end=20,
                content="chunk parziale",
                embedding=np.ones(EMBEDDING_DIM, dtype=np.float32).tobytes(),
                embedding_model=MODEL_NAME,
                embedding_dim=EMBEDDING_DIM,
            )
        )
        self.db.commit()

        total_chunks = 257
        chunks = ((i, i + 1, f"chunk completo {i}") for i in range(total_chunks))
        extracted = ExtractedPage(1, "testo completo aggiornato", False, "not-needed", "processed")
        with (
            patch.object(ingest_local, "iter_pdf_pages", return_value=iter([extracted])),
            patch.object(ingest_local, "chunk_page", return_value=chunks),
            patch.object(ingest_local, "embed_texts", side_effect=self._fake_embed),
        ):
            ingest_local.process_file(self.db, self.pdf_path, metadata)

        self.db.refresh(document)
        self.assertEqual(self.db.query(Chunk).filter(Chunk.page_id == page.id).count(), total_chunks)
        self.assertEqual(document.status, "processed")
        self.assertEqual(document.ingestion_version, ingest_local.CHUNKING_VERSION)
        self.assertEqual(page.indexed_chunk_count, total_chunks)

    def test_resume_preserves_completed_page_and_rebuilds_only_partial_page(self) -> None:
        two_page_path = self.root / "two-pages.pdf"
        pdf: Any = fitz.open()
        pdf.new_page(width=595, height=842)
        pdf.new_page(width=595, height=842)
        pdf.save(two_page_path)
        pdf.close()
        digest = ingest_local.sha256_file(two_page_path)
        metadata = self._metadata(
            drive_file_id="drive-resume-pages",
            drive_size=two_page_path.stat().st_size,
            drive_md5="c" * 32,
        )
        document = Document(
            drive_file_id="drive-resume-pages",
            title="Verbale con ripresa per pagina",
            drive_url=metadata["drive_url"],
            drive_size=metadata["drive_size"],
            drive_md5=metadata["drive_md5"],
            file_hash=digest,
            ingestion_version=ingest_local.CHUNKING_VERSION,
            total_pages=2,
            processed_pages=1,
            status="processing",
        )
        self.db.add(document)
        self.db.flush()
        complete_page = Page(
            document_id=document.id,
            page_number=1,
            content="testo completo pagina uno",
            text_hash=ingest_local.content_hash("testo completo pagina uno"),
            ocr_used=False,
            ocr_status="not-needed",
            status="processed",
            indexed_chunk_count=1,
        )
        partial_page = Page(
            document_id=document.id,
            page_number=2,
            content="testo parziale pagina due",
            text_hash=ingest_local.content_hash("testo parziale pagina due"),
            ocr_used=False,
            ocr_status="not-needed",
            status="processing",
            indexed_chunk_count=0,
        )
        stale_page = Page(
            document_id=document.id,
            page_number=3,
            content="pagina fuori dal PDF",
            text_hash=ingest_local.content_hash("pagina fuori dal PDF"),
            ocr_used=False,
            ocr_status="not-needed",
            status="processed",
            indexed_chunk_count=1,
        )
        self.db.add_all([complete_page, partial_page, stale_page])
        self.db.flush()
        complete_chunk = Chunk(
            document_id=document.id,
            page_id=complete_page.id,
            chunk_number=0,
            char_start=0,
            char_end=26,
            content="chunk pagina uno",
            embedding=np.ones(EMBEDDING_DIM, dtype=np.float32).tobytes(),
            embedding_model=MODEL_NAME,
            embedding_dim=EMBEDDING_DIM,
        )
        self.db.add_all(
            [
                complete_chunk,
                Chunk(
                    document_id=document.id,
                    page_id=partial_page.id,
                    chunk_number=0,
                    char_start=0,
                    char_end=15,
                    content="chunk incompleto",
                    embedding=np.ones(EMBEDDING_DIM, dtype=np.float32).tobytes(),
                    embedding_model=MODEL_NAME,
                    embedding_dim=EMBEDDING_DIM,
                ),
                Chunk(
                    document_id=document.id,
                    page_id=stale_page.id,
                    chunk_number=0,
                    char_start=0,
                    char_end=20,
                    content="chunk fuori intervallo",
                    embedding=np.ones(EMBEDDING_DIM, dtype=np.float32).tobytes(),
                    embedding_model=MODEL_NAME,
                    embedding_dim=EMBEDDING_DIM,
                ),
            ]
        )
        self.db.commit()

        extracted_pages = [
            ExtractedPage(1, "testo completo pagina uno", False, "not-needed", "processed"),
            ExtractedPage(2, "testo aggiornato pagina due", False, "not-needed", "processed"),
        ]

        def chunk_only_page_two(content: str):
            self.assertEqual(content, "testo aggiornato pagina due")
            return iter([(0, len(content), "chunk pagina due aggiornato")])

        with (
            patch.object(ingest_local, "iter_pdf_pages", return_value=iter(extracted_pages)),
            patch.object(ingest_local, "chunk_page", side_effect=chunk_only_page_two),
            patch.object(ingest_local, "embed_texts", side_effect=self._fake_embed) as embed_call,
        ):
            ingest_local.process_file(self.db, two_page_path, metadata)

        self.db.refresh(document)
        self.assertEqual(embed_call.call_count, 1)
        self.assertEqual(document.processed_pages, 2)
        self.assertEqual(document.status, "processed")
        self.assertEqual(self.db.query(Chunk).filter(Chunk.page_id == complete_page.id).one().id, complete_chunk.id)
        self.assertEqual(self.db.query(Chunk).filter(Chunk.page_id == partial_page.id).count(), 1)
        self.assertEqual(partial_page.indexed_chunk_count, 1)
        self.assertEqual(self.db.query(Page).filter(Page.document_id == document.id, Page.page_number == 3).count(), 0)

    def test_same_hash_processed_document_retries_uncertain_ocr_page(self) -> None:
        metadata = self._metadata(drive_file_id="drive-ocr-retry")
        document = Document(
            drive_file_id="drive-ocr-retry",
            title=metadata["title"],
            drive_url=metadata["drive_url"],
            drive_size=metadata["drive_size"],
            drive_md5=metadata["drive_md5"],
            file_hash=ingest_local.sha256_file(self.pdf_path),
            ingestion_version=ingest_local.CHUNKING_VERSION,
            total_pages=1,
            processed_pages=1,
            status="processed",
        )
        self.db.add(document)
        self.db.flush()
        page = Page(
            document_id=document.id,
            page_number=1,
            content="testo OCR incerto precedente",
            text_hash=ingest_local.content_hash("testo OCR incerto precedente"),
            ocr_used=True,
            ocr_status="uncertain",
            status="processed",
            indexed_chunk_count=1,
        )
        self.db.add(page)
        self.db.flush()
        old_chunk = Chunk(
            document_id=document.id,
            page_id=page.id,
            chunk_number=0,
            char_start=0,
            char_end=29,
            content="chunk OCR incerto",
            embedding=np.ones(EMBEDDING_DIM, dtype=np.float32).tobytes(),
            embedding_model=MODEL_NAME,
            embedding_dim=EMBEDDING_DIM,
        )
        self.db.add(old_chunk)
        self.db.commit()
        self.db.expunge(old_chunk)

        extracted = ExtractedPage(1, "testo verificato dopo rotazione", True, "success", "processed")
        with (
            patch.object(ingest_local, "iter_pdf_pages", return_value=iter([extracted])) as page_iter,
            patch.object(ingest_local, "chunk_page", return_value=iter([(0, 31, "chunk OCR corretto")])),
            patch.object(ingest_local, "embed_texts", side_effect=self._fake_embed) as embed_call,
        ):
            result = ingest_local.process_file(self.db, self.pdf_path, metadata)

        self.db.refresh(document)
        self.db.refresh(page)
        self.assertTrue(result.startswith("indicizzato:"))
        self.assertTrue(page_iter.called)
        self.assertEqual(embed_call.call_count, 1)
        self.assertEqual(document.status, "processed")
        self.assertEqual(page.ocr_status, "success")
        self.assertEqual(page.content, "testo verificato dopo rotazione")
        self.assertEqual(
            self.db.query(Chunk.content).filter(Chunk.page_id == page.id).one()[0],
            "chunk OCR corretto",
        )

    def test_verified_empty_page_is_preserved_during_resume(self) -> None:
        metadata = self._metadata(drive_file_id="drive-verified-empty")
        document = Document(
            drive_file_id="drive-verified-empty",
            title=metadata["title"],
            drive_url=metadata["drive_url"],
            drive_size=metadata["drive_size"],
            drive_md5=metadata["drive_md5"],
            file_hash=ingest_local.sha256_file(self.pdf_path),
            ingestion_version=ingest_local.CHUNKING_VERSION,
            total_pages=1,
            processed_pages=0,
            status="processing",
        )
        self.db.add(document)
        self.db.flush()
        page = Page(
            document_id=document.id,
            page_number=1,
            content="",
            text_hash=ingest_local.content_hash(""),
            ocr_used=True,
            ocr_status="verified-empty",
            status="empty",
            indexed_chunk_count=0,
        )
        self.db.add(page)
        self.db.commit()

        def no_page_extraction(path, ocr_language="ita+eng", skip_page_numbers=None):
            self.assertEqual(skip_page_numbers, {1})
            return iter(())

        with (
            patch.object(ingest_local, "iter_pdf_pages", side_effect=no_page_extraction),
            patch.object(ingest_local, "chunk_page", side_effect=AssertionError("pagina vuota già verificata")),
            patch.object(ingest_local, "embed_texts", side_effect=AssertionError("nessun embedding atteso")),
        ):
            ingest_local.process_file(self.db, self.pdf_path, metadata)

        self.db.refresh(document)
        self.db.refresh(page)
        self.assertEqual(document.status, "processed")
        self.assertEqual(document.processed_pages, 1)
        self.assertEqual(page.ocr_status, "verified-empty")

    def test_repeated_current_drive_revision_fast_paths_without_resetting_progress(self) -> None:
        metadata = self._metadata(drive_file_id="drive-revision")
        original_page = ExtractedPage(1, "testo versione originale", False, "not-needed", "processed")
        with (
            patch.object(ingest_local, "iter_pdf_pages", return_value=iter([original_page])),
            patch.object(ingest_local, "chunk_page", return_value=iter([(0, 24, "testo originale")])),
            patch.object(ingest_local, "embed_texts", side_effect=self._fake_embed),
        ):
            ingest_local.process_file(self.db, self.pdf_path, metadata)

        revision_path = self.root / "verbale-revisionato.pdf"
        pdf: Any = fitz.open()
        revision_page = pdf.new_page(width=595, height=842)
        revision_page.insert_text((72, 72), "revisione aggiornata")
        pdf.save(revision_path)
        pdf.close()
        revision_metadata = self._metadata(
            drive_file_id="drive-revision",
            drive_size=revision_path.stat().st_size,
            drive_md5="b" * 32,
        )
        updated_page = ExtractedPage(1, "testo versione aggiornata", False, "not-needed", "processed")
        with (
            patch.object(ingest_local, "iter_pdf_pages", return_value=iter([updated_page])),
            patch.object(ingest_local, "chunk_page", return_value=iter([(0, 25, "testo aggiornato")])),
            patch.object(ingest_local, "embed_texts", side_effect=self._fake_embed),
        ):
            ingest_local.process_file(self.db, revision_path, revision_metadata)

        revision_id = f"drive-revision#v-{ingest_local.sha256_file(revision_path)[:12]}"
        revision = self.db.query(Document).filter(Document.drive_file_id == revision_id).one()
        self.assertEqual(revision.processed_pages, 1)
        self.assertEqual(revision.status, "processed")
        self.assertEqual(revision.ingestion_version, ingest_local.CHUNKING_VERSION)

        with patch.object(ingest_local, "iter_pdf_pages", side_effect=AssertionError("revisione già indicizzata")):
            result = ingest_local.process_file(self.db, revision_path, revision_metadata)
        self.db.refresh(revision)
        self.assertTrue(result.startswith("già presente:"))
        self.assertEqual(revision.processed_pages, 1)


if __name__ == "__main__":
    unittest.main()
