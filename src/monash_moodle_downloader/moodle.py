"""Session-authenticated Moodle AJAX client and course discovery."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import urlencode

from playwright.async_api import Page

from monash_moodle_downloader.errors import CourseNotFoundError, MoodleApiError
from monash_moodle_downloader.models import Activity, ActivityType, Course, Section

COURSE_LIST_METHOD = "core_course_get_enrolled_courses_by_timeline_classification"
COURSE_STATE_METHOD = "core_courseformat_get_state"
COURSE_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z]{2,4}\d{4})(?!\d)", re.IGNORECASE)


class MoodleAjaxClient:
    """Call Moodle AJAX functions through an authenticated browser page."""

    def __init__(self, page: Page, *, base_url: str) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")

    async def call(self, method: str, args: Mapping[str, object]) -> object:
        """Call one AJAX method without exposing the Moodle session key."""
        sesskey_value = await self.page.evaluate(
            "() => window.M && window.M.cfg ? window.M.cfg.sesskey : null"
        )
        if not isinstance(sesskey_value, str) or not sesskey_value:
            raise MoodleApiError("The Moodle session is not authenticated. Run `mmd login`.")

        query = urlencode({"sesskey": sesskey_value, "info": method})
        endpoint = f"{self.base_url}/lib/ajax/service.php?{query}"
        payload = [{"index": 0, "methodname": method, "args": dict(args)}]
        raw_result = await self.page.evaluate(
            """
            async ({ endpoint, payload }) => {
                const response = await fetch(endpoint, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(payload),
                });
                return {
                    ok: response.ok,
                    status: response.status,
                    body: await response.text(),
                };
            }
            """,
            {"endpoint": endpoint, "payload": payload},
        )
        result = cast(dict[str, object], raw_result)
        status = _optional_int(result.get("status")) or 0
        if result.get("ok") is not True:
            raise MoodleApiError(f"Moodle AJAX request failed with HTTP {status}.")

        body = result.get("body")
        if not isinstance(body, str):
            raise MoodleApiError("Moodle returned an unreadable AJAX response.")
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise MoodleApiError("Moodle returned invalid JSON.") from error
        if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
            raise MoodleApiError("Moodle returned an unexpected AJAX response shape.")

        response = cast(dict[str, object], decoded[0])
        if response.get("error") is True:
            message = response.get("exception") or response.get("message") or "Unknown Moodle error"
            raise MoodleApiError(f"Moodle rejected {method}: {message}")
        if "data" not in response:
            raise MoodleApiError(f"Moodle returned no data for {method}.")
        return response["data"]

    async def list_courses(self) -> list[Course]:
        """Return courses visible to the authenticated Moodle account."""
        data = await self.call(
            COURSE_LIST_METHOD,
            {
                "offset": 0,
                "limit": 0,
                "classification": "all",
                "sort": "fullname",
                "customfieldname": "",
                "customfieldvalue": "",
                "requiredfields": [
                    "id",
                    "fullname",
                    "shortname",
                    "showcoursecategory",
                    "showshortname",
                    "visible",
                    "enddate",
                ],
            },
        )
        mapping = _as_mapping(data, "course list")
        records = _as_sequence(mapping.get("courses"), "course records")
        courses = [_course_from_record(_as_mapping(record, "course")) for record in records]
        return sorted(courses, key=lambda course: (course.code, course.name))

    async def resolve_course(self, selector: str) -> Course:
        """Resolve an exact course code or numeric Moodle course ID."""
        courses = await self.list_courses()
        normalised = selector.strip().casefold()
        matches = [
            course
            for course in courses
            if str(course.id) == selector.strip() or course.code.casefold() == normalised
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CourseNotFoundError(
                f"No visible Moodle course matches {selector!r}. Run `mmd courses` to list choices."
            )
        raise CourseNotFoundError(
            f"More than one visible course matches {selector!r}; use its numeric Moodle ID."
        )

    async def get_course_state(self, course: Course) -> Course:
        """Attach sections and course-module metadata returned by Moodle courseformat state."""
        data = await self.call(COURSE_STATE_METHOD, {"courseid": course.id})
        state = _decode_state(data)
        raw_sections = _as_sequence(
            state.get("section", state.get("sections", [])), "course sections"
        )
        raw_activities = _as_sequence(
            state.get("cm", state.get("cms", state.get("activities", []))),
            "course activities",
        )

        sections: list[Section] = []
        by_id: dict[int, Section] = {}
        by_number: dict[int, Section] = {}
        for item in raw_sections:
            record = _as_mapping(item, "section")
            section_id = _as_int(record.get("id"), "section id")
            number = _optional_int(record.get("section", record.get("number"))) or 0
            title = _first_text(record, "title", "name") or f"Section {number}"
            section = Section(
                id=section_id,
                number=number,
                title=title,
                visible=_visible(record),
            )
            sections.append(section)
            by_id[section_id] = section
            by_number[number] = section

        for item in raw_activities:
            record = _as_mapping(item, "activity")
            activity_section_id = _optional_int(record.get("sectionid"))
            section_number = _optional_int(record.get("sectionnumber", record.get("section")))
            target_section: Section | None = (
                by_id.get(activity_section_id) if activity_section_id is not None else None
            )
            if target_section is None and section_number is not None:
                target_section = by_number.get(section_number)
            if target_section is None:
                continue
            modname = _first_text(record, "modname", "plugin") or "unknown"
            activity = Activity(
                id=_as_int(record.get("id"), "activity id"),
                name=_first_text(record, "name") or "Untitled activity",
                activity_type=_activity_type(modname),
                section_id=target_section.id,
                visible=_visible(record),
            )
            target_section.activities.append(activity)

        sections.sort(key=lambda section: (section.number, section.id))
        course.sections = sections
        return course


def extract_course_code(shortname: str, fullname: str) -> str:
    """Extract a stable unit code without depending on Monash naming suffixes."""
    for candidate in (shortname, fullname):
        match = COURSE_CODE_PATTERN.search(candidate)
        if match:
            return match.group(1).upper()
    return shortname.strip() or "COURSE"


def _course_from_record(record: Mapping[str, object]) -> Course:
    course_id = _as_int(record.get("id"), "course id")
    fullname = _first_text(record, "fullname") or f"Course {course_id}"
    shortname = _first_text(record, "shortname") or ""
    return Course(
        id=course_id,
        code=extract_course_code(shortname, fullname),
        name=fullname,
        visible=_visible(record),
        end_date=_optional_int(record.get("enddate")),
    )


def _decode_state(data: object) -> Mapping[str, object]:
    current = data
    for _ in range(3):
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except json.JSONDecodeError as error:
                raise MoodleApiError("Moodle returned invalid course state JSON.") from error
            continue
        if isinstance(current, Mapping) and "state" in current:
            current = current["state"]
            continue
        break
    return _as_mapping(current, "course state")


def _activity_type(modname: str) -> ActivityType:
    normalised = modname.casefold().removeprefix("mod_")
    mapping = {
        "assign": ActivityType.ASSIGNMENT,
        "assignment": ActivityType.ASSIGNMENT,
        "resource": ActivityType.FILE,
        "file": ActivityType.FILE,
        "folder": ActivityType.FOLDER,
        "h5pactivity": ActivityType.H5P,
        "h5p": ActivityType.H5P,
        "label": ActivityType.TEXT,
        "text": ActivityType.TEXT,
        "page": ActivityType.PAGE,
        "quiz": ActivityType.QUIZ,
        "url": ActivityType.URL,
        "lti": ActivityType.EXTERNAL_TOOL,
    }
    return mapping.get(normalised, ActivityType.UNKNOWN)


def _visible(record: Mapping[str, object]) -> bool:
    return bool(record.get("visible", True)) and bool(record.get("uservisible", True))


def _first_text(record: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise MoodleApiError(f"Moodle returned an invalid numeric value: {value!r}.")


def _as_int(value: object, label: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise MoodleApiError(f"Moodle returned no {label}.")
    return parsed


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MoodleApiError(f"Moodle returned invalid {label} data.")
    return cast(Mapping[str, object], value)


def _as_sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MoodleApiError(f"Moodle returned invalid {label} data.")
    return value
