from __future__ import annotations

import json
import os
import ssl
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ArchiveSource(Base):
    __tablename__ = "cts_archive_sources"

    drive_file_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    drive_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    drive_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    drive_md5: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meeting_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    meeting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    download_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


def _create_engine_from_environment() -> Engine | None:
    raw = os.getenv("DATABASE_URL") or os.getenv("DRIZZLE_DATABASE_URL")
    if not raw:
        return None

    url = make_url(raw)
    if url.drivername == "mysql":
        url = url.set(drivername="mysql+pymysql")

    # The managed project DSN carries Node-style JSON: ssl={"rejectUnauthorized":true}.
    # PyMySQL expects an SSLContext or options mapping, not that JSON string.
    query = dict(url.query)
    ssl_option = query.pop("ssl", None)
    connect_args: dict[str, Any] = {}
    if ssl_option:
        if isinstance(ssl_option, str):
            try:
                tls_options = json.loads(ssl_option)
            except json.JSONDecodeError:
                tls_options = {"rejectUnauthorized": ssl_option.lower() not in {"0", "false", "no", "disable"}}
        else:
            tls_options = ssl_option
        if isinstance(tls_options, bool):
            tls_options = {"rejectUnauthorized": tls_options}
        elif not isinstance(tls_options, dict):
            tls_options = {"rejectUnauthorized": True}

        reject_unauthorized = bool(tls_options.get("rejectUnauthorized", True))
        paths = ssl.get_default_verify_paths()
        ca_option = tls_options.get("ca")
        capath_option = tls_options.get("capath")
        ca_file = ca_option if isinstance(ca_option, str) else paths.cafile
        ca_path = capath_option if isinstance(capath_option, str) else paths.capath
        context = ssl.create_default_context(cafile=ca_file, capath=ca_path)
        if reject_unauthorized:
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        else:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = context

    url = url.set(query=query)
    return create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=5,
        future=True,
    )


engine = _create_engine_from_environment()
SessionLocal: sessionmaker[Session] | None = (
    sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    if engine is not None
    else None
)


class Document(Base):
    __tablename__ = "cts_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    drive_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    drive_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    drive_md5: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meeting_number: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    meeting_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingestion_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Page(Base):
    __tablename__ = "cts_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_cts_page_document_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("cts_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(LONGTEXT, nullable=False, default="")
    text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ocr_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not-needed")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class Chunk(Base):
    __tablename__ = "cts_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("cts_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("cts_pages.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class AllowedEmail(Base):
    __tablename__ = "cts_allowed_emails"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    label: Mapped[str | None] = mapped_column(String(160), nullable=True)


class OneTimeCode(Base):
    __tablename__ = "cts_one_time_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserSession(Base):
    __tablename__ = "cts_user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def create_schema() -> None:
    """Create missing tables and add safe nullable columns; never drop user data."""
    if engine is None:
        return
    Base.metadata.create_all(bind=engine)
    # create_all does not add columns to already-existing managed tables.
    with engine.begin() as connection:
        existing = {row[0] for row in connection.execute(text("SHOW COLUMNS FROM cts_documents"))}
        additions = {
            "drive_size": "ALTER TABLE cts_documents ADD COLUMN drive_size BIGINT NULL",
            "drive_md5": "ALTER TABLE cts_documents ADD COLUMN drive_md5 VARCHAR(32) NULL",
            "duplicate_of_id": "ALTER TABLE cts_documents ADD COLUMN duplicate_of_id INTEGER NULL",
            "ingestion_version": "ALTER TABLE cts_documents ADD COLUMN ingestion_version VARCHAR(64) NULL",
        }
        for column_name, statement in additions.items():
            if column_name not in existing:
                connection.execute(text(statement))


def db_session() -> Session:
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL non è configurato")
    return SessionLocal()
