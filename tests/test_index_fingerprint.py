from __future__ import annotations

import hashlib
import unittest
import zlib

import numpy as np
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, Chunk, Document, Page
from app.retrieval.embeddings import CHUNKING_VERSION, EMBEDDING_DIM, MODEL_NAME
from app.retrieval.index import VectorIndex


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_for_sqlite(element, compiler, **kw):
    return "TEXT"


def _as_bytes(value):
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


class _BitXor:
    def __init__(self):
        self.value = 0

    def step(self, value):
        if value is not None:
            self.value ^= int(value)

    def finalize(self):
        return self.value


class VectorIndexFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def _register(connection, _record):
            connection.create_function("CONCAT", -1, lambda *values: "".join(str(value) for value in values))
            connection.create_function(
                "SHA2", 2,
                lambda value, bits: hashlib.sha256(_as_bytes(value)).hexdigest() if int(bits) == 256 else None,
            )
            connection.create_function("CRC32", 1, lambda value: zlib.crc32(_as_bytes(value)) & 0xFFFFFFFF)
            connection.create_aggregate("BIT_XOR", 1, _BitXor)

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, class_=Session, expire_on_commit=False)
        self.db = self.Session()
        document = Document(
            drive_file_id="drive-1",
            title="Test",
            file_hash="a" * 64,
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
            content="testo",
            status="processed",
            ocr_status="not-needed",
            indexed_chunk_count=1,
        )
        self.db.add(page)
        self.db.flush()
        self.chunk = Chunk(
            document_id=document.id,
            page_id=page.id,
            chunk_number=0,
            content="testo",
            embedding=np.ones(EMBEDDING_DIM, dtype=np.float32).tobytes(),
            embedding_model=MODEL_NAME,
            embedding_dim=EMBEDDING_DIM,
        )
        self.db.add(self.chunk)
        self.db.commit()
        self.index = VectorIndex()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_in_place_embedding_mutation_invalidates_cache_and_marks_document(self) -> None:
        self.assertEqual(self.index.size(self.db), 1)
        vector = np.ones(EMBEDDING_DIM, dtype=np.float32)
        vector[0] = np.nan
        self.chunk.embedding = vector.tobytes()
        self.db.commit()
        self.assertEqual(self.index.size(self.db), 0)
        self.assertEqual(self.index.invalid_document_ids(self.db), {self.chunk.document_id})


if __name__ == "__main__":
    unittest.main()
