import json
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Page

from monash_moodle_downloader.content import (
    CourseContentScanner,
    classify_external_link,
    folder_relative_path,
    normalise_url,
    safe_manifest_url,
    select_sections,
)
from monash_moodle_downloader.errors import MoodleApiError
from monash_moodle_downloader.models import (
    Activity,
    ActivityType,
    Course,
    Resource,
    ResourceStatus,
    Section,
    SyncManifest,
)
from monash_moodle_downloader.output import safe_component, write_scan_output

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, body: str, *, status: int = 200, headers: dict[str, str] | None = None):
        self.body = body
        self.status = status
        self.headers = headers or {"content-type": "text/html"}

    async def text(self) -> str:
        return self.body


class FakeRequest:
    def __init__(self, responses: dict[str, FakeResponse]):
        self.responses = responses

    async def get(self, url: str, **_kwargs: object) -> FakeResponse:
        return self.responses[url]


class FakePage:
    def __init__(self, responses: dict[str, FakeResponse]):
        self.request = FakeRequest(responses)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def sample_course() -> Course:
    base = "https://learning.monash.edu"
    week = Section(
        id=700,
        number=7,
        title="Week 1 - Introduction",
        parent_number=3,
        source_url=f"{base}/course/view.php?id=77&section=7",
        activities=[Activity(100, "Week banner", ActivityType.TEXT, 700)],
    )
    own_time = Section(
        id=701,
        number=8,
        title="Own-time",
        parent_number=7,
        source_url=f"{base}/course/view.php?id=77&section=7#section-8",
        activities=[
            Activity(101, "Repeated banner", ActivityType.TEXT, 701),
            Activity(
                102,
                "Lecture notes",
                ActivityType.FILE,
                701,
                source_url=f"{base}/mod/resource/view.php?id=102",
            ),
            Activity(
                103,
                "Exercise submission",
                ActivityType.ASSIGNMENT,
                701,
                source_url=f"{base}/mod/assign/view.php?id=103",
            ),
        ],
    )
    folder = Section(
        id=702,
        number=56,
        title="Assessments",
        parent_number=0,
        source_url=f"{base}/course/view.php?id=77&section=56",
        activities=[
            Activity(
                104,
                "Data examples",
                ActivityType.FOLDER,
                702,
                source_url=f"{base}/mod/folder/view.php?id=104",
            ),
            Activity(
                105,
                "Design link",
                ActivityType.URL,
                702,
                source_url=f"{base}/mod/url/view.php?id=105",
            ),
        ],
    )
    return Course(
        id=77, code="FIT0000", name="FIT0000 Example Unit", sections=[week, own_time, folder]
    )


@pytest.mark.asyncio
async def test_scan_extracts_text_files_links_and_deduplicates() -> None:
    base = "https://learning.monash.edu"
    responses = {
        f"{base}/course/view.php?id=77&section=7": FakeResponse(fixture("week_page.html")),
        f"{base}/course/view.php?id=77&section=56": FakeResponse(fixture("folder_page.html")),
        f"{base}/mod/assign/view.php?id=103": FakeResponse(fixture("assignment_page.html")),
        f"{base}/mod/folder/view.php?id=104": FakeResponse(fixture("folder_page.html")),
        f"{base}/mod/url/view.php?id=105": FakeResponse(fixture("url_page.html")),
    }
    page = cast(Page, cast(Any, FakePage(responses)))

    manifest = await CourseContentScanner(page, base_url=base).scan(sample_course())

    week, own_time, assessments = manifest.course.sections
    assert "Decorative banner" not in (week.activities[0].text_markdown or "")
    assert "collapseOverviewSection" not in (week.activities[0].text_markdown or "")
    assert own_time.activities[0].text_markdown is None
    assert own_time.activities[0].text_duplicate_of == 100
    assert own_time.activities[1].resources[0].source_kind == "moodle_resource"
    assignment = own_time.activities[2]
    assert "Submit one archive" in (assignment.text_markdown or "")
    assert {item.name for item in assignment.resources} == {
        "Specification PDF",
        "Starter ZIP",
        "Banner",
    }
    assert next(item for item in assignment.resources if item.name == "Banner").status.value == (
        "skipped_media"
    )
    folder = assessments.activities[0]
    assert folder.resources[0].relative_path == "examples/data.csv"
    assert folder.resources[1].status.value == "skipped_media"
    assert assessments.activities[1].external_links[0].link_type == "canva"


@pytest.mark.asyncio
async def test_scan_can_select_one_week_and_children() -> None:
    course = sample_course()
    base = "https://learning.monash.edu"
    page = cast(
        Page,
        cast(
            Any,
            FakePage(
                {
                    f"{base}/course/view.php?id=77&section=7": FakeResponse(
                        fixture("week_page.html")
                    ),
                    f"{base}/mod/assign/view.php?id=103": FakeResponse(
                        fixture("assignment_page.html")
                    ),
                }
            ),
        ),
    )

    manifest = await CourseContentScanner(page, base_url=base).scan(course, week=1)

    assert [section.number for section in manifest.course.sections] == [7, 8]


def test_select_sections_can_combine_multiple_weeks_and_children() -> None:
    sections = [
        Section(1, 10, "Week 0 - Orientation"),
        Section(2, 11, "Own-time", parent_number=10),
        Section(3, 20, "Week 2 - Types"),
        Section(4, 21, "Workshop", parent_number=20),
        Section(5, 30, "Week 3 - Later"),
    ]

    selected = select_sections(sections, weeks=[0, 2])

    assert [section.id for section in selected] == [1, 2, 3, 4]


def test_output_writes_week_assignment_and_manifest(tmp_path: Path) -> None:
    course = sample_course()
    course.sections = select_sections(course.sections, week=1)
    course.sections[1].activities[2].text_markdown = "Assignment details."
    manifest = SyncManifest(course=course)

    destination = write_scan_output(manifest, tmp_path)

    assert (destination / "FIT0000 - Last Sync.json").is_file()
    assert (destination / "FIT0000 Example Unit.md").is_file()
    week_document = next(destination.glob("Week 01*/Week 1 - Introduction.md"))
    assignment_document = next(destination.glob("Week 01*/Assignments/*/Exercise submission.md"))
    assert "Exercise submission" in week_document.read_text(encoding="utf-8")
    assert "Assignment details" in assignment_document.read_text(encoding="utf-8")
    assert "Assignment details" not in week_document.read_text(encoding="utf-8")


def test_output_links_downloaded_files_relative_to_each_readme(tmp_path: Path) -> None:
    course = sample_course()
    course.sections = select_sections(course.sections, week=1)
    resource = course.sections[1].activities[1].resources
    if not resource:
        course.sections[1].activities[1].resources.append(
            Resource(
                "102:file",
                "Lecture notes.pdf",
                "https://learning.monash.edu/file.pdf",
                "moodle_resource",
                status=ResourceStatus.DOWNLOADED,
                local_path="Week 01 - Introduction/Files/Lecture notes.pdf",
            )
        )
    destination = write_scan_output(SyncManifest(course=course), tmp_path)
    readme = destination / "Week 01 - Introduction" / "Week 1 - Introduction.md"

    assert "[Lecture notes.pdf](Files/Lecture%20notes.pdf)" in readme.read_text(encoding="utf-8")


def test_partial_writes_keep_all_existing_weeks_in_course_index(tmp_path: Path) -> None:
    first = Course(
        77,
        "FIT0000",
        "FIT0000 Example Unit",
        sections=[Section(1, 10, "Week 1 - Start")],
    )
    second = Course(
        77,
        "FIT0000",
        "FIT0000 Example Unit",
        sections=[Section(2, 20, "Week 2 - Continue")],
    )

    destination = write_scan_output(SyncManifest(course=first), tmp_path)
    write_scan_output(SyncManifest(course=second), tmp_path)
    index = (destination / "FIT0000 Example Unit.md").read_text(encoding="utf-8")

    assert "Week 1 - Start" in index
    assert "Week 2 - Continue" in index


def test_output_safely_migrates_recognised_legacy_files(tmp_path: Path) -> None:
    course = Course(
        77,
        "FIT0000",
        "FIT0000 Example Unit",
        sections=[Section(2, 20, "Week 2 - Continue")],
    )
    destination = tmp_path / course.name
    assignment = destination / "Week 01 - Introduction" / "Assignments" / "Old task (10%)"
    assignment.mkdir(parents=True)
    (destination / "README.md").write_text(
        f"# {course.name}\n\n## Scanned content\n",
        encoding="utf-8",
    )
    week_legacy = destination / "Week 01 - Introduction" / "README.md"
    week_legacy.write_text(
        "# Week 1 - Introduction\n\n[Old task](Assignments/Old task (10%)/README.md)\n",
        encoding="utf-8",
    )
    (assignment / "README.md").write_text(
        "# Old task (10%)\n\nDetails.\n",
        encoding="utf-8",
    )
    (destination / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "course": {"id": course.id}}),
        encoding="utf-8",
    )

    write_scan_output(SyncManifest(course=course), tmp_path)

    assert not list(destination.rglob("README.md"))
    assert not (destination / "manifest.json").exists()
    migrated_week = destination / "Week 01 - Introduction" / "Week 1 - Introduction.md"
    assert "Assignments/Old%20task%20%2810%25%29/Old%20task%20%2810%25%29.md" in (
        migrated_week.read_text(encoding="utf-8")
    )
    assert (assignment / "Old task (10%).md").is_file()


def test_output_preserves_unrecognised_or_conflicting_legacy_files(tmp_path: Path) -> None:
    course = Course(
        77,
        "FIT0000",
        "FIT0000 Example Unit",
        sections=[Section(1, 10, "Week 1 - Start")],
    )
    destination = tmp_path / course.name
    week_directory = destination / "Week 01 - Start"
    week_directory.mkdir(parents=True)
    custom = week_directory / "README.md"
    custom.write_text("# My personal notes\n", encoding="utf-8")
    warnings: list[str] = []

    write_scan_output(SyncManifest(course=course), tmp_path, on_warning=warnings.append)

    assert custom.is_file()
    assert any("unrecognised" in warning for warning in warnings)


def test_output_preserves_recognised_legacy_when_new_name_already_differs(
    tmp_path: Path,
) -> None:
    course = Course(
        77,
        "FIT0000",
        "FIT0000 Example Unit",
        sections=[Section(2, 20, "Week 2 - Continue")],
    )
    week_directory = tmp_path / course.name / "Week 01 - Start"
    week_directory.mkdir(parents=True)
    legacy = week_directory / "README.md"
    target = week_directory / "Week 1 - Start.md"
    legacy.write_text("# Week 1 - Start\n\nLegacy details.\n", encoding="utf-8")
    target.write_text("# Week 1 - Start\n\nDifferent details.\n", encoding="utf-8")
    warnings: list[str] = []

    write_scan_output(SyncManifest(course=course), tmp_path, on_warning=warnings.append)

    assert legacy.is_file()
    assert target.is_file()
    assert any("different content" in warning for warning in warnings)


def test_url_and_path_helpers_remove_noise_and_secrets() -> None:
    assert normalise_url("https://example.com/a?utm_source=x&id=2#part") == (
        "https://example.com/a?id=2"
    )
    assert "Signature" not in safe_manifest_url("https://cdn.example/a?Signature=secret&id=2")
    assert classify_external_link("https://www.canva.com/design/abc") == "canva"
    assert classify_external_link("https://example.com/notes.pdf") == "file_candidate"
    assert (
        folder_relative_path(
            "https://learning.monash.edu/pluginfile.php/2/mod_folder/content/0/a/b.csv"
        )
        == "a/b.csv"
    )
    assert safe_component("Week 1: A/B?") == "Week 1- A-B-"


def test_manifest_removes_temporary_signatures_without_changing_runtime_url() -> None:
    course = sample_course()
    signed = "https://cdn.example/notes?X-Amz-Signature=secret&id=2&token=private"
    resource = Resource("signed", "Notes", signed, "external_direct_file")
    course.sections[0].activities[0].resources.append(resource)

    exported = SyncManifest(course=course).to_dict()
    exported_url = exported["course"]["sections"][0]["activities"][0]["resources"][0]["source_url"]

    assert exported_url == "https://cdn.example/notes?id=2"
    assert resource.source_url == signed


def test_select_sections_rejects_a_missing_week() -> None:
    with pytest.raises(MoodleApiError, match="Week 9 was not found"):
        select_sections(sample_course().sections, week=9)
