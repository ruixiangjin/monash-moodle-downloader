import json
from typing import Any, cast

import pytest
from playwright.async_api import Page

from monash_moodle_downloader.errors import CourseNotFoundError, MoodleApiError
from monash_moodle_downloader.models import ActivityType
from monash_moodle_downloader.moodle import MoodleAjaxClient, extract_course_code


class FakePage:
    def __init__(self, responses: dict[str, object], *, session_key: str | None = "private-key"):
        self.responses = responses
        self.session_key = session_key

    async def evaluate(self, expression: str, argument: object | None = None) -> object:
        if "window.M" in expression:
            return self.session_key
        request = cast(dict[str, Any], argument)
        payload = request["payload"][0]
        method = payload["methodname"]
        response = self.responses[method]
        if (
            method == "core_course_get_enrolled_courses_by_timeline_classification"
            and isinstance(response, dict)
            and "all" in response
        ):
            response = response[payload["args"]["classification"]]
        body = [{"error": False, "data": response}]
        return {"ok": True, "status": 200, "body": json.dumps(body)}


def client(responses: dict[str, object]) -> MoodleAjaxClient:
    page = cast(Page, cast(object, FakePage(responses)))
    return MoodleAjaxClient(page, base_url="https://learning.monash.edu")


COURSES = {
    "courses": [
        {
            "id": 44375,
            "fullname": "ETW2001 Foundations of Data Analysis",
            "shortname": "ETW2001_S2_2026",
            "visible": True,
            "enddate": 1790000000,
        },
        {
            "id": 44553,
            "fullname": "FIT2014 Theory of Computation",
            "shortname": "FIT2014_S2_2026",
            "visible": True,
            "enddate": 1790000000,
        },
        {
            "id": 44834,
            "fullname": "FIT2102 Programming Paradigms",
            "shortname": "FIT2102_S2_2026",
            "visible": True,
            "enddate": 1790000000,
        },
        {
            "id": 42804,
            "fullname": "FIT2109 Cybersecurity",
            "shortname": "FIT2109_S2_2026",
            "visible": True,
            "enddate": 1790000000,
        },
    ]
}

REMOVED_COURSES = {
    "courses": [
        {
            "id": 34987,
            "fullname": "FIT1045 Introduction to programming - S2 2025",
            "shortname": "FIT1045_S2_2025",
            "visible": True,
            "enddate": 1750000000,
        }
    ]
}


@pytest.mark.asyncio
async def test_list_courses_extracts_codes_and_sorts() -> None:
    moodle = client({"core_course_get_enrolled_courses_by_timeline_classification": COURSES})

    courses = await moodle.list_courses()

    assert [course.code for course in courses] == ["ETW2001", "FIT2014", "FIT2102", "FIT2109"]
    assert courses[1].id == 44553


@pytest.mark.asyncio
async def test_list_courses_includes_and_marks_removed_courses() -> None:
    responses: dict[str, object] = {
        "core_course_get_enrolled_courses_by_timeline_classification": {
            "all": COURSES,
            "hidden": REMOVED_COURSES,
        }
    }
    moodle = client(responses)

    all_courses = await moodle.list_courses(include_removed=True)
    current_courses = await moodle.list_courses(include_removed=False)

    assert [course.code for course in current_courses] == [
        "ETW2001",
        "FIT2014",
        "FIT2102",
        "FIT2109",
    ]
    removed = next(course for course in all_courses if course.code == "FIT1045")
    assert removed.removed_from_view is True
    assert all(not course.removed_from_view for course in current_courses)


@pytest.mark.asyncio
async def test_resolve_course_can_select_removed_course_by_id() -> None:
    moodle = client(
        {
            "core_course_get_enrolled_courses_by_timeline_classification": {
                "all": COURSES,
                "hidden": REMOVED_COURSES,
            }
        }
    )

    course = await moodle.resolve_course("34987")

    assert course.code == "FIT1045"
    assert course.removed_from_view is True


@pytest.mark.asyncio
async def test_resolve_course_accepts_code_or_id() -> None:
    moodle = client({"core_course_get_enrolled_courses_by_timeline_classification": COURSES})

    assert (await moodle.resolve_course("fit2102")).id == 44834
    assert (await moodle.resolve_course("42804")).code == "FIT2109"
    with pytest.raises(CourseNotFoundError, match="mmd courses"):
        await moodle.resolve_course("FIT9999")


@pytest.mark.asyncio
async def test_course_state_maps_sections_and_activity_types() -> None:
    state = {
        "section": [
            {
                "id": 10,
                "section": 0,
                "title": "General",
                "visible": 1,
                "parent": 0,
                "sectionurl": "https://learning.monash.edu/course/view.php?id=7&section=0",
            },
            {"id": 11, "section": 1, "title": "Week 1", "visible": 1, "parent": 0},
        ],
        "cm": [
            {"id": 100, "name": "Welcome", "modname": "label", "sectionid": 10},
            {
                "id": 101,
                "name": "Lecture notes",
                "modname": "File",
                "module": "resource",
                "sectionid": 11,
                "url": "https://learning.monash.edu/mod/resource/view.php?id=101",
            },
            {"id": 102, "name": "Quiz", "modname": "quiz", "sectionid": 11},
        ],
    }
    moodle = client(
        {
            "core_course_get_enrolled_courses_by_timeline_classification": COURSES,
            "core_courseformat_get_state": json.dumps(state),
        }
    )
    course = await moodle.resolve_course("FIT2014")

    populated = await moodle.get_course_state(course)

    assert [section.title for section in populated.sections] == ["General", "Week 1"]
    assert populated.sections[0].parent_number == 0
    assert populated.sections[1].activities[0].source_url is not None
    assert [item.activity_type for item in populated.sections[1].activities] == [
        ActivityType.FILE,
        ActivityType.QUIZ,
    ]


@pytest.mark.asyncio
async def test_api_errors_do_not_include_the_session_key() -> None:
    page = cast(Page, cast(object, FakePage({}, session_key=None)))
    moodle = MoodleAjaxClient(page, base_url="https://learning.monash.edu")

    with pytest.raises(MoodleApiError) as captured:
        await moodle.list_courses()

    assert "private-key" not in str(captured.value)
    assert "sesskey" not in str(captured.value).casefold()


def test_extract_course_code_uses_shortname_then_fullname() -> None:
    assert extract_course_code("FIT2014_S2_2026", "Different title") == "FIT2014"
    assert extract_course_code("2026-semester-two", "ETW2001 Foundations") == "ETW2001"
