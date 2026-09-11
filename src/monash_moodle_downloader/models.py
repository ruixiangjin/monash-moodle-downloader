"""Shared domain models and the public manifest shape."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ActivityType(StrEnum):
    """Normalised Moodle activity categories used by parsers and manifests."""

    ASSIGNMENT = "assignment"
    CONTENT = "content"
    EXTERNAL_TOOL = "external_tool"
    FILE = "file"
    FOLDER = "folder"
    FORUM = "forum"
    H5P = "h5p"
    PAGE = "page"
    QUIZ = "quiz"
    TEXT = "text"
    URL = "url"
    UNKNOWN = "unknown"


class ResourceStatus(StrEnum):
    """Result of handling a resource during a scan or synchronisation."""

    DOWNLOADED = "downloaded"
    FAILED = "failed"
    LINK_ONLY = "link_only"
    MISSING_REMOTE = "missing_remote"
    PENDING = "pending"
    SKIPPED_MEDIA = "skipped_media"
    UNCHANGED = "unchanged"
    UNSUPPORTED = "unsupported"


@dataclass(slots=True)
class Resource:
    """A downloadable file or an item deliberately left as a reference."""

    source_id: str
    name: str
    source_url: str
    source_kind: str
    status: ResourceStatus = ResourceStatus.PENDING
    local_path: str | None = None
    mime_type: str | None = None
    size: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    sha256: str | None = None
    final_host: str | None = None
    relative_path: str | None = None


@dataclass(slots=True)
class ExternalLink:
    """A classified external target without authentication secrets or signed query data."""

    source_id: str
    name: str
    source_url: str
    link_type: str = "unknown"
    final_host: str | None = None
    status: ResourceStatus = ResourceStatus.LINK_ONLY


@dataclass(slots=True)
class Activity:
    """One Moodle course-module entry."""

    id: int
    name: str
    activity_type: ActivityType
    section_id: int
    source_url: str | None = None
    visible: bool = True
    text_markdown: str | None = None
    text_hash: str | None = None
    text_duplicate_of: int | None = None
    resources: list[Resource] = field(default_factory=list)
    external_links: list[ExternalLink] = field(default_factory=list)


@dataclass(slots=True)
class Section:
    """A Moodle section, usually a teaching week or general information area."""

    id: int
    number: int
    title: str
    parent_number: int | None = None
    source_url: str | None = None
    visible: bool = True
    text_markdown: str | None = None
    text_hash: str | None = None
    activities: list[Activity] = field(default_factory=list)


@dataclass(slots=True)
class Course:
    """A course visible to the authenticated user."""

    id: int
    code: str
    name: str
    visible: bool = True
    end_date: int | None = None
    sections: list[Section] = field(default_factory=list)


@dataclass(slots=True)
class SyncManifest:
    """Versioned, JSON-serialisable record of a course scan or synchronisation."""

    course: Course
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    schema_version: int = field(default=1, init=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of the manifest."""
        return asdict(self)
