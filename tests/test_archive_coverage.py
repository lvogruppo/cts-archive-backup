from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.database import ArchiveSource, Base, Chunk, Document, Page
from app.retrieval.embeddings import CHUNKING_VERSION, EMBEDDING_DIM, MODEL_NAME
from app.services.coverage import MINIMUM_EXPECTED_SOURCE_COUNT, archive_coverage
import app.retrieval.search as search_module


# The test database is always SQLite memory-only; the project runtime remains MySQL.
@compiles(LONGTEXT, "sqlite")
def _compile_longtext_for_sqlite(element, compiler, **kw):
    return "TEXT"


class _FakeIndex:
    def __init__(self, rows: list[tuple[int, float]]) -> None:
        self.rows = rows
        self.allowed_ids: set[int] | None = None

    def search(self, db, query, limit, allowed_ids=None):
        self.allowed_ids = allowed_ids
        return self.rows


class ArchiveCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, class_=Session, expire_on_commit=False)
        self.db = self.Session()
        self.current_chunk_id, self.stale_chunk_id, self.current_document_id = self._seed_complete_archive()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _add_processed_document(self, drive_id: str, md5: str, file_hash: str, title: str) -> tuple[Document, Chunk]:
        document = Document(
            drive_file_id=drive_id,
            title=title,
            drive_md5=md5,
            file_hash=file_hash,
            ingestion_version=CHUNKING_VERSION,
            total_pages=1,
            processed_pages=1,
            status="processed",
        )
        self.db.add(document)
        self.db.flush()
        page = Page(
            document_id=document.id,
            page_number=1,
            content="estratto di prova verificato",
            ocr_status="not-needed",
            status="processed",
        )
        self.db.add(page)
        self.db.flush()
        chunk = Chunk(
            document_id=document.id,
            page_id=page.id,
            chunk_number=0,
            content="estratto di prova verificato",
            embedding=np.ones(EMBEDDING_DIM, dtype=np.float32).tobytes(),
            embedding_model=MODEL_NAME,
            embedding_dim=EMBEDDING_DIM,
        )
        self.db.add(chunk)
        self.db.flush()
        return document, chunk

    def _seed_complete_archive(self) -> tuple[int, int, int]:
        current_md5 = "a" * 32
        current_hash = "c" * 64
        sources: list[ArchiveSource] = []
        for index in range(MINIMUM_EXPECTED_SOURCE_COUNT):
            sources.append(
                ArchiveSource(
                    drive_file_id=f"drive-{index:03d}",
                    title=f"Verbale {index:03d}",
                    drive_md5=current_md5,
                    download_verified=True,
                )
            )
        self.db.add_all(sources)
        current, current_chunk = self._add_processed_document("drive-000", current_md5, current_hash, "Verbale corrente")

        for index in range(1, MINIMUM_EXPECTED_SOURCE_COUNT):
            duplicate = Document(
                drive_file_id=f"drive-{index:03d}",
                title=f"Copia {index:03d}",
                drive_md5=current_md5,
                file_hash=current_hash,
                duplicate_of_id=current.id,
                ingestion_version=CHUNKING_VERSION,
                status="duplicate",
            )
            self.db.add(duplicate)

        # A processed prior revision with the same Drive ID must never enter the current corpus.
        stale, stale_chunk = self._add_processed_document(
            "drive-000#v-obsolete",
            "b" * 32,
            "d" * 64,
            "Versione obsoleta",
        )
        self.db.commit()
        return int(current_chunk.id), int(stale_chunk.id), int(current.id)

    def test_complete_inventory_resolves_duplicates_and_excludes_old_revision(self) -> None:
        report = archive_coverage(self.db)
        self.assertTrue(report.complete)
        self.assertEqual(report.expected_sources, MINIMUM_EXPECTED_SOURCE_COUNT)
        self.assertEqual(report.complete_sources, MINIMUM_EXPECTED_SOURCE_COUNT)
        self.assertEqual(report.eligible_document_ids, frozenset({self.current_document_id}))

    def test_unverified_expected_source_keeps_coverage_closed(self) -> None:
        self.db.add(
            ArchiveSource(
                drive_file_id="drive-unverified",
                title="PDF non verificato",
                drive_md5="e" * 32,
                download_verified=False,
                download_error="checksum non verificato",
            )
        )
        self.db.commit()
        report = archive_coverage(self.db)
        self.assertFalse(report.complete)
        self.assertEqual(report.expected_sources, MINIMUM_EXPECTED_SOURCE_COUNT + 1)

    def test_search_applies_manifest_filter_and_rejects_stale_rows_defensively(self) -> None:
        report = archive_coverage(self.db)
        lexical = _FakeIndex([(self.current_chunk_id, 1.0), (self.stale_chunk_id, 0.99)])
        semantic = _FakeIndex([(self.current_chunk_id, 0.99), (self.stale_chunk_id, 1.0)])
        query_vector = np.ones((1, EMBEDDING_DIM), dtype=np.float32)
        with (
            patch.object(search_module, "lexical_index", lexical),
            patch.object(search_module, "vector_index", semantic),
            patch.object(search_module, "embed_texts", return_value=query_vector),
        ):
            results = search_module.search_chunks(
                self.db,
                "test",
                eligible_document_ids=set(report.eligible_document_ids),
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["chunk_id"], self.current_chunk_id)
        self.assertEqual(lexical.allowed_ids, {self.current_chunk_id})
        self.assertEqual(semantic.allowed_ids, {self.current_chunk_id})


if __name__ == "__main__":
    unittest.main()
