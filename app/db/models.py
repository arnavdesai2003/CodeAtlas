from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    clone_url: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        unique=True,
    )

    default_branch: Mapped[str] = mapped_column(
        String(255),
        default="main",
        nullable=False,
    )

    last_indexed_commit: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    files: Mapped[list["CodeFile"]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
    )


class RepositorySyncJob(Base):
    """Durable work left between metadata commit and search-index publication."""

    __tablename__ = "repository_sync_jobs"

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True,
    )
    old_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    target_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    affected_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    file_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    stats: Mapped[dict] = mapped_column(JSON, nullable=False)


class RepositoryFullIndexJob(Base):
    """Committed symbol snapshot awaiting replayable full publication."""

    __tablename__ = "repository_full_index_jobs"
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True,
    )
    stats: Mapped[dict] = mapped_column(JSON, nullable=False)


class IndexPublicationJob(Base):
    """Singleton journal: a whole-index copy must exclude all cooperating writers."""

    __tablename__ = "index_publication_jobs"
    __table_args__ = (CheckConstraint("id = 1", name="ck_single_index_publication_job"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    source_index: Mapped[str] = mapped_column(String(255), nullable=False)
    staging_index: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    stats: Mapped[dict] = mapped_column(JSON, nullable=False)


class CodeFile(Base):
    __tablename__ = "code_files"

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "path",
            name="uq_code_file_repository_path",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
    )

    language: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    last_indexed_commit: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    repository: Mapped["Repository"] = relationship(
        back_populates="files",
    )
class CodeSymbol(Base):
    __tablename__ = "code_symbols"

    __table_args__ = (
        UniqueConstraint(
            "file_id",
            "name",
            "kind",
            "start_line",
            name="uq_code_symbol_location",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    file_id: Mapped[int] = mapped_column(
        ForeignKey("code_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    qualified_name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )

    start_line: Mapped[int] = mapped_column(nullable=False)

    end_line: Mapped[int] = mapped_column(nullable=False)

    code: Mapped[str] = mapped_column(
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
