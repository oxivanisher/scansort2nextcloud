"""SQLite store: ORM models, versioned migrations (``PRAGMA user_version``) and session handling."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores naive UTC in SQLite, returns aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class Status:
    PENDING = "pending"
    FILED = "filed"
    HANDLED_EXTERNALLY = "handled_externally"
    DELETED_EXTERNALLY = "deleted_externally"
    DELETED = "deleted"
    ERROR = "error"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String, index=True)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default=Status.PENDING, index=True)
    nc_fileid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    nc_path: Mapped[str | None] = mapped_column(String, nullable=True)
    nc_etag: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    text_source: Mapped[str] = mapped_column(String, default="none")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    doc_date_guessed: Mapped[bool] = mapped_column(Boolean, default=False)
    sender: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_name: Mapped[str] = mapped_column(String, default="")
    final_label: Mapped[str | None] = mapped_column(String, nullable=True)
    final_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    filed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    proposals: Mapped[list[Proposal]] = relationship(
        back_populates="document", order_by="Proposal.rank", cascade="all, delete-orphan"
    )

    @property
    def top_proposal(self) -> Proposal | None:
        return self.proposals[0] if self.proposals else None


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String)
    probability: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String)  # model | rule | sender | new
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)

    document: Mapped[Document] = relationship(back_populates="proposals")


class IngestAttempt(Base):
    __tablename__ = "ingest_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_path: Mapped[str] = mapped_column(String, unique=True)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_try_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ArchiveFile(Base):
    __tablename__ = "archive_files"

    nc_fileid: Mapped[str] = mapped_column(String, primary_key=True)
    nc_path: Mapped[str] = mapped_column(String)
    etag: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Folder(Base):
    __tablename__ = "folders"

    path: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, index=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SettingKV(Base):
    __tablename__ = "settings_kv"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# --------------------------------------------------------------------------------------------------
# Migrations. Append new entries; never edit an applied one. Index = resulting user_version.
# --------------------------------------------------------------------------------------------------
MIGRATIONS: list[list[str]] = [
    # v1: initial schema
    [
        """CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            sha256 VARCHAR NOT NULL,
            duplicate_of INTEGER REFERENCES documents(id),
            status VARCHAR NOT NULL DEFAULT 'pending',
            nc_fileid VARCHAR,
            nc_path VARCHAR,
            nc_etag VARCHAR,
            text TEXT NOT NULL DEFAULT '',
            text_source VARCHAR NOT NULL DEFAULT 'none',
            page_count INTEGER NOT NULL DEFAULT 0,
            doc_date DATE,
            doc_date_guessed BOOLEAN NOT NULL DEFAULT 0,
            sender VARCHAR,
            amount VARCHAR,
            proposed_name VARCHAR NOT NULL DEFAULT '',
            final_label VARCHAR,
            final_path VARCHAR,
            created_at DATETIME NOT NULL,
            filed_at DATETIME
        )""",
        "CREATE INDEX ix_documents_sha256 ON documents (sha256)",
        "CREATE INDEX ix_documents_status ON documents (status)",
        "CREATE INDEX ix_documents_nc_fileid ON documents (nc_fileid)",
        """CREATE TABLE proposals (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            label VARCHAR NOT NULL,
            probability FLOAT NOT NULL,
            source VARCHAR NOT NULL,
            model_version VARCHAR
        )""",
        "CREATE INDEX ix_proposals_document_id ON proposals (document_id)",
        """CREATE TABLE ingest_attempts (
            id INTEGER PRIMARY KEY,
            local_path VARCHAR NOT NULL UNIQUE,
            sha256 VARCHAR,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            next_try_at DATETIME NOT NULL
        )""",
        """CREATE TABLE archive_files (
            nc_fileid VARCHAR PRIMARY KEY,
            nc_path VARCHAR NOT NULL,
            etag VARCHAR NOT NULL,
            label VARCHAR NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            synced_at DATETIME NOT NULL
        )""",
        "CREATE INDEX ix_archive_files_label ON archive_files (label)",
        """CREATE TABLE folders (
            path VARCHAR PRIMARY KEY,
            label VARCHAR NOT NULL,
            doc_count INTEGER NOT NULL DEFAULT 0,
            synced_at DATETIME NOT NULL
        )""",
        "CREATE INDEX ix_folders_label ON folders (label)",
        "CREATE TABLE settings_kv (key VARCHAR PRIMARY KEY, value TEXT NOT NULL)",
    ],
]

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    """Thin wrapper around a SQLite engine. All sessions are serialized (single writer)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(self.engine, "connect", self._on_connect)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)
        self._lock = threading.RLock()

    @staticmethod
    def _on_connect(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    def migrate(self) -> int:
        with self._lock, self.engine.begin() as conn:
            current = conn.execute(text("PRAGMA user_version")).scalar_one()
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{current} is newer than this scansort (v{SCHEMA_VERSION})"
                )
            for version in range(current, SCHEMA_VERSION):
                log.info("applying database migration", extra={"version": version + 1})
                for stmt in MIGRATIONS[version]:
                    conn.execute(text(stmt))
                conn.execute(text(f"PRAGMA user_version = {version + 1}"))
            return SCHEMA_VERSION

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._lock:
            session = self._sessionmaker()
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    # key/value helpers -----------------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self.session() as s:
            row = s.get(SettingKV, key)
            return row.value if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self.session() as s:
            row = s.get(SettingKV, key)
            if row:
                row.value = value
            else:
                s.add(SettingKV(key=key, value=value))

    def all_kv(self) -> dict[str, str]:
        with self.session() as s:
            return {r.key: r.value for r in s.scalars(select(SettingKV))}
