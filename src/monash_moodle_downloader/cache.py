"""SQLite-backed incremental download state kept outside the repository."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CacheEntry:
    course_id: int
    source_url: str
    local_path: str
    etag: str | None
    last_modified: str | None
    content_length: int | None
    sha256: str | None
    status: str


class ResourceCache:
    """Store remote metadata without mixing state into course-material folders."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resources (
                course_id INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                local_path TEXT NOT NULL,
                etag TEXT,
                last_modified TEXT,
                content_length INTEGER,
                sha256 TEXT,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (course_id, source_url)
            )
            """
        )
        self.connection.commit()

    def __enter__(self) -> ResourceCache:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def get(self, course_id: int, source_url: str) -> CacheEntry | None:
        row = self.connection.execute(
            """
            SELECT course_id, source_url, local_path, etag, last_modified,
                   content_length, sha256, status
            FROM resources
            WHERE course_id = ? AND source_url = ?
            """,
            (course_id, source_url),
        ).fetchone()
        if row is None:
            return None
        return CacheEntry(
            course_id=int(row["course_id"]),
            source_url=str(row["source_url"]),
            local_path=str(row["local_path"]),
            etag=_optional_text(row["etag"]),
            last_modified=_optional_text(row["last_modified"]),
            content_length=_optional_int(row["content_length"]),
            sha256=_optional_text(row["sha256"]),
            status=str(row["status"]),
        )

    def put(self, entry: CacheEntry) -> None:
        self.connection.execute(
            """
            INSERT INTO resources (
                course_id, source_url, local_path, etag, last_modified,
                content_length, sha256, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(course_id, source_url) DO UPDATE SET
                local_path = excluded.local_path,
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                content_length = excluded.content_length,
                sha256 = excluded.sha256,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                entry.course_id,
                entry.source_url,
                entry.local_path,
                entry.etag,
                entry.last_modified,
                entry.content_length,
                entry.sha256,
                entry.status,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()


def metadata_matches(
    entry: CacheEntry,
    *,
    etag: str | None,
    last_modified: str | None,
    content_length: int | None,
) -> bool:
    """Compare strong metadata first and avoid size-only false positives."""
    if etag and entry.etag:
        return etag == entry.etag
    if last_modified and entry.last_modified and content_length is not None:
        return last_modified == entry.last_modified and content_length == entry.content_length
    return False


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError("Cache contained an invalid integer value.")
