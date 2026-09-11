"""Write titled Markdown and a machine-readable record of the latest operation."""

from __future__ import annotations

import json
import os
import re
import warnings
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import quote

from monash_moodle_downloader.content import week_number
from monash_moodle_downloader.models import (
    Activity,
    ActivityType,
    Course,
    Resource,
    Section,
    SyncManifest,
)

UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WarningHandler = Callable[[str], None]


def write_scan_output(
    manifest: SyncManifest,
    output_root: Path,
    *,
    on_warning: WarningHandler | None = None,
) -> Path:
    """Write readable output and safely migrate recognised legacy index files."""
    course = manifest.course
    course_directory = course_directory_for(course, output_root)
    course_directory.mkdir(parents=True, exist_ok=True)
    existing_before = {path.resolve() for path in course_directory.rglob("*") if path.is_file()}
    last_sync = course_directory / last_sync_filename(course)
    _write_text(
        last_sync,
        f"{json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)}\n",
    )

    week_roots = [section for section in course.sections if week_number(section.title) is not None]
    assigned: set[int] = set()

    for root in week_roots:
        children = [section for section in course.sections if section.parent_number == root.number]
        grouped = [root, *children]
        assigned.update(section.id for section in grouped)
        directory_name = week_directory_name(root)
        group_directory = course_directory / directory_name
        group_directory.mkdir(parents=True, exist_ok=True)
        _write_group(course_directory, group_directory, root.title, grouped)

    general = [section for section in course.sections if section.id not in assigned]
    if general:
        general_directory = course_directory / "General"
        general_directory.mkdir(parents=True, exist_ok=True)
        _write_group(course_directory, general_directory, "General", general)

    _migrate_legacy_markdown(
        course_directory,
        existing_before=existing_before,
        on_warning=on_warning,
    )
    course_document = course_directory / markdown_filename(course.name)
    _write_course_index(course_directory, course_document, course)
    _migrate_legacy_manifest(
        course_directory / "manifest.json",
        last_sync,
        course,
        target_existed=last_sync.resolve() in existing_before,
        on_warning=on_warning,
    )
    _migrate_one_legacy_markdown(
        course_directory / "README.md",
        course_document,
        expected_title=course.name,
        target_existed=course_document.resolve() in existing_before,
        on_warning=on_warning,
    )
    return course_directory


def _write_group(
    course_directory: Path,
    directory: Path,
    title: str,
    sections: Iterable[Section],
) -> None:
    lines = [f"# {title}", ""]
    for section in sections:
        lines.extend([f"## {section.title}", ""])
        if section.text_markdown:
            lines.extend([section.text_markdown, ""])
        for activity in section.activities:
            if activity.activity_type is ActivityType.ASSIGNMENT:
                assignment_path = _write_assignment(course_directory, directory, activity)
                lines.extend(
                    [
                        f"### [{activity.name}]({quote(assignment_path.as_posix(), safe='/')})",
                        "",
                        "Type: assignment",
                        "",
                    ]
                )
            else:
                lines.extend(_activity_markdown(course_directory, directory, activity))
    _write_text(directory / markdown_filename(title), f"{'\n'.join(lines).rstrip()}\n")


def _write_assignment(
    course_directory: Path,
    group_directory: Path,
    activity: Activity,
) -> Path:
    filename = markdown_filename(activity.name)
    relative = Path("Assignments") / safe_component(activity.name) / filename
    destination = group_directory / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {activity.name}",
        "",
        *_activity_details(course_directory, destination.parent, activity),
    ]
    _write_text(destination, f"{'\n'.join(lines).rstrip()}\n")
    return relative


def _activity_markdown(
    course_directory: Path,
    current_directory: Path,
    activity: Activity,
) -> list[str]:
    return [
        f"### {activity.name}",
        "",
        f"Type: {activity.activity_type.value}",
        "",
        *_activity_details(course_directory, current_directory, activity),
    ]


def _activity_details(
    course_directory: Path,
    current_directory: Path,
    activity: Activity,
) -> list[str]:
    lines: list[str] = []
    if activity.text_markdown:
        lines.extend([activity.text_markdown, ""])
    elif activity.text_duplicate_of is not None:
        lines.extend([f"Repeated content from activity `{activity.text_duplicate_of}`.", ""])

    visible_resources = [
        item for item in activity.resources if item.status.value != "skipped_media"
    ]
    if visible_resources:
        lines.extend(["#### Files", ""])
        for resource in visible_resources:
            detail = resource.relative_path or resource.name
            if resource.local_path:
                target = course_directory / resource.local_path
                relative_link = os.path.relpath(target, current_directory)
                encoded = quote(Path(relative_link).as_posix(), safe="/")
                lines.append(f"- [{detail}]({encoded}) — `{resource.status.value}`")
            else:
                lines.append(f"- {detail} — `{resource.status.value}`")
        lines.append("")

    if activity.external_links:
        lines.extend(["#### Links", ""])
        for link in activity.external_links:
            lines.append(f"- [{link.name}]({link.source_url}) — `{link.link_type}`")
        lines.append("")
    return lines


def week_directory_name(section: Section) -> str:
    number = week_number(section.title)
    if number is None:
        return safe_component(section.title)
    detail = re.sub(r"^\s*Week\s+\d+\s*[-–—:]?\s*", "", section.title, flags=re.IGNORECASE)
    suffix = f" - {detail}" if detail else ""
    return safe_component(f"Week {number:02d}{suffix}")


def safe_component(value: str, *, max_length: int = 120) -> str:
    cleaned = UNSAFE_PATH_CHARS.sub("-", value)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned or "Untitled")[:max_length].rstrip(" .")


def markdown_filename(title: str) -> str:
    """Name a generated Markdown document after its first-level heading."""
    return f"{safe_component(title)}.md"


def last_sync_filename(course: Course) -> str:
    return f"{safe_component(course.code)} - Last Sync.json"


def _write_course_index(directory: Path, destination: Path, course: Course) -> None:
    entries: list[tuple[int, str, Path]] = []
    for child in directory.iterdir():
        if not child.is_dir():
            continue
        if child.name == "General":
            document = child / markdown_filename("General")
            if document.is_file():
                entries.append((-1, "General", document))
            continue
        match = re.match(r"^Week\s+0*(\d+)\b", child.name, flags=re.IGNORECASE)
        if match is None:
            continue
        number = int(match.group(1))
        week_document = _week_document_in(child, number)
        if week_document is not None:
            title = _first_heading(week_document) or child.name
            entries.append((number, title, week_document))

    lines = [f"# {course.name}", "", "## Local course content", ""]
    for _, title, document in sorted(entries, key=lambda entry: (entry[0], entry[1])):
        relative = os.path.relpath(document, directory)
        lines.append(f"- [{title}]({quote(Path(relative).as_posix(), safe='/')})")
    _write_text(destination, f"{'\n'.join(lines).rstrip()}\n")


def _week_document_in(directory: Path, number: int) -> Path | None:
    for candidate in sorted(directory.glob("*.md")):
        if candidate.name == "README.md":
            continue
        title = _first_heading(candidate)
        if title is not None and week_number(title) == number:
            return candidate
    return None


def _migrate_legacy_markdown(
    course_directory: Path,
    *,
    existing_before: set[Path],
    on_warning: WarningHandler | None,
) -> None:
    plans: list[tuple[Path, Path, str]] = []
    for legacy in course_directory.rglob("README.md"):
        if legacy.parent == course_directory:
            continue
        title = _first_heading(legacy)
        if title is None:
            _emit_warning(on_warning, f"Preserved unrecognised legacy file: {legacy}")
            continue
        if not _legacy_title_matches_location(legacy, title):
            _emit_warning(on_warning, f"Preserved unrecognised legacy file: {legacy}")
            continue
        plans.append((legacy, legacy.with_name(markdown_filename(title)), title))

    link_targets = {legacy.resolve(): target for legacy, target, _ in plans}
    for legacy, target, title in sorted(
        plans,
        key=lambda plan: len(plan[0].parts),
        reverse=True,
    ):
        _migrate_one_legacy_markdown(
            legacy,
            target,
            expected_title=title,
            target_existed=target.resolve() in existing_before,
            on_warning=on_warning,
            link_targets=link_targets,
        )


def _legacy_title_matches_location(path: Path, title: str) -> bool:
    if path.parent.name == "General":
        return title == "General"
    if path.parent.parent.name == "Assignments":
        return safe_component(title) == path.parent.name
    match = re.match(r"^Week\s+0*(\d+)\b", path.parent.name, flags=re.IGNORECASE)
    return match is not None and week_number(title) == int(match.group(1))


def _migrate_one_legacy_markdown(
    legacy: Path,
    target: Path,
    *,
    expected_title: str,
    target_existed: bool,
    on_warning: WarningHandler | None,
    link_targets: dict[Path, Path] | None = None,
) -> None:
    if not legacy.is_file():
        return
    content = legacy.read_text(encoding="utf-8")
    heading = _first_heading_from_text(content)
    if heading is None or safe_component(heading) != safe_component(expected_title):
        _emit_warning(on_warning, f"Preserved unrecognised legacy file: {legacy}")
        return
    migrated = _rewrite_legacy_links(content, legacy.parent, link_targets or {})
    if target.is_file():
        if target_existed and target.read_text(encoding="utf-8") != migrated:
            _emit_warning(
                on_warning,
                f"Preserved legacy file because {target.name} already has different content: "
                f"{legacy}",
            )
            return
    else:
        _write_text(target, migrated)
    legacy.unlink()


def _rewrite_legacy_links(
    content: str,
    base_directory: Path,
    link_targets: dict[Path, Path],
) -> str:
    migrated = content
    for old_target, new_target in link_targets.items():
        old_relative = Path(os.path.relpath(old_target, base_directory)).as_posix()
        new_relative = Path(os.path.relpath(new_target, base_directory)).as_posix()
        encoded_new = quote(new_relative, safe="/")
        for old_form in {old_relative, quote(old_relative, safe="/")}:
            migrated = migrated.replace(f"]({old_form})", f"]({encoded_new})")
    return migrated


def _migrate_legacy_manifest(
    legacy: Path,
    target: Path,
    course: Course,
    *,
    target_existed: bool,
    on_warning: WarningHandler | None,
) -> None:
    if not legacy.is_file():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _emit_warning(on_warning, f"Preserved unrecognised legacy file: {legacy}")
        return
    record = data.get("course") if isinstance(data, dict) else None
    valid = (
        isinstance(data, dict)
        and isinstance(data.get("schema_version"), int)
        and isinstance(record, dict)
        and record.get("id") == course.id
    )
    if not valid:
        _emit_warning(on_warning, f"Preserved unrecognised legacy file: {legacy}")
        return
    if target_existed and target.read_text(encoding="utf-8") != legacy.read_text(encoding="utf-8"):
        _emit_warning(
            on_warning,
            f"Preserved legacy manifest because {target.name} already has different content: "
            f"{legacy}",
        )
        return
    legacy.unlink()


def _first_heading(path: Path) -> str | None:
    try:
        return _first_heading_from_text(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _first_heading_from_text(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _emit_warning(handler: WarningHandler | None, message: str) -> None:
    if handler is not None:
        handler(message)
    else:
        warnings.warn(message, UserWarning, stacklevel=2)


def course_directory_for(course: Course, output_root: Path) -> Path:
    return output_root / safe_component(course.name)


def resource_relative_path(
    course: Course,
    section: Section,
    activity: Activity,
    resource: Resource,
) -> Path:
    """Return a traversal-safe location relative to the course directory."""
    group = _group_directory_for_section(course, section)
    if activity.activity_type is ActivityType.ASSIGNMENT:
        base = group / "Assignments" / safe_component(activity.name) / "Attachments"
    elif activity.activity_type is ActivityType.FOLDER:
        base = group / "Files" / safe_component(activity.name)
    else:
        base = group / "Files"

    if resource.relative_path:
        parts = safe_relative_parts(resource.relative_path)
        if parts:
            return base.joinpath(*parts)
    return base / safe_component(resource.name)


def safe_relative_parts(value: str) -> list[str]:
    parts: list[str] = []
    for raw in value.replace("\\", "/").split("/"):
        if raw in {"", ".", ".."}:
            continue
        parts.append(safe_component(raw))
    return parts


def _group_directory_for_section(course: Course, section: Section) -> Path:
    for root in course.sections:
        if week_number(root.title) is None:
            continue
        if section.id == root.id or section.parent_number == root.number:
            return Path(week_directory_name(root))
    return Path("General")
