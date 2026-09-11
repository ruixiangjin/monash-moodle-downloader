"""Write human-readable Markdown and a machine-readable scan manifest."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from monash_moodle_downloader.content import week_number
from monash_moodle_downloader.models import Activity, ActivityType, Section, SyncManifest

UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def write_scan_output(manifest: SyncManifest, output_root: Path) -> Path:
    """Write a scan without creating any attachment bodies."""
    course = manifest.course
    course_directory = output_root / safe_component(course.name)
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
        _write_group(group_directory, root.title, grouped)
        index_lines.append(f"- [{root.title}]({directory_name}/README.md)")

    general = [section for section in course.sections if section.id not in assigned]
    if general:
        general_directory = course_directory / "General"
        general_directory.mkdir(parents=True, exist_ok=True)
        _write_group(general_directory, "General", general)
        index_lines.append("- [General](General/README.md)")

    (course_directory / "README.md").write_text(
        f"{'\n'.join(index_lines).rstrip()}\n",
        encoding="utf-8",
    )
    return course_directory


def _write_group(directory: Path, title: str, sections: Iterable[Section]) -> None:
    lines = [f"# {title}", ""]
    for section in sections:
        lines.extend([f"## {section.title}", ""])
        if section.text_markdown:
            lines.extend([section.text_markdown, ""])
        for activity in section.activities:
            if activity.activity_type is ActivityType.ASSIGNMENT:
                assignment_path = _write_assignment(directory, activity)
                lines.extend(
                    [
                        f"### [{activity.name}]({assignment_path.as_posix()})",
                        "",
                        "Type: assignment",
                        "",
                    ]
                )
            else:
                lines.extend(_activity_markdown(activity))
    (directory / "README.md").write_text(
        f"{'\n'.join(lines).rstrip()}\n",
        encoding="utf-8",
    )


def _write_assignment(group_directory: Path, activity: Activity) -> Path:
    relative = Path("Assignments") / safe_component(activity.name) / "README.md"
    destination = group_directory / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {activity.name}", "", *_activity_details(activity)]
    destination.write_text(f"{'\n'.join(lines).rstrip()}\n", encoding="utf-8")
    return relative


def _activity_markdown(activity: Activity) -> list[str]:
    return [
        f"### {activity.name}",
        "",
        f"Type: {activity.activity_type.value}",
        "",
        *_activity_details(activity),
    ]


def _activity_details(activity: Activity) -> list[str]:
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
