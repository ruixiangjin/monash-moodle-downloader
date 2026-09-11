"""Write human-readable Markdown and a machine-readable scan manifest."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
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


def write_scan_output(manifest: SyncManifest, output_root: Path) -> Path:
    """Write a scan without creating any attachment bodies."""
    course = manifest.course
    course_directory = course_directory_for(course, output_root)
    course_directory.mkdir(parents=True, exist_ok=True)
    (course_directory / "manifest.json").write_text(
        f"{json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )

    week_roots = [section for section in course.sections if week_number(section.title) is not None]
    assigned: set[int] = set()
    index_lines = [f"# {course.name}", "", "## Scanned content", ""]

    for root in week_roots:
        children = [section for section in course.sections if section.parent_number == root.number]
        grouped = [root, *children]
        assigned.update(section.id for section in grouped)
        directory_name = week_directory_name(root)
        group_directory = course_directory / directory_name
        group_directory.mkdir(parents=True, exist_ok=True)
        _write_group(course_directory, group_directory, root.title, grouped)
        index_lines.append(f"- [{root.title}]({directory_name}/README.md)")

    general = [section for section in course.sections if section.id not in assigned]
    if general:
        general_directory = course_directory / "General"
        general_directory.mkdir(parents=True, exist_ok=True)
        _write_group(course_directory, general_directory, "General", general)
        index_lines.append("- [General](General/README.md)")

    (course_directory / "README.md").write_text(
        f"{'\n'.join(index_lines).rstrip()}\n",
        encoding="utf-8",
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
                        f"### [{activity.name}]({assignment_path.as_posix()})",
                        "",
                        "Type: assignment",
                        "",
                    ]
                )
            else:
                lines.extend(_activity_markdown(course_directory, directory, activity))
    (directory / "README.md").write_text(
        f"{'\n'.join(lines).rstrip()}\n",
        encoding="utf-8",
    )


def _write_assignment(
    course_directory: Path,
    group_directory: Path,
    activity: Activity,
) -> Path:
    relative = Path("Assignments") / safe_component(activity.name) / "README.md"
    destination = group_directory / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {activity.name}",
        "",
        *_activity_details(course_directory, destination.parent, activity),
    ]
    destination.write_text(f"{'\n'.join(lines).rstrip()}\n", encoding="utf-8")
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
