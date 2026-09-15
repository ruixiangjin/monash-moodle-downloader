from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from monash_moodle_downloader import cli
from monash_moodle_downloader.cli import app
from monash_moodle_downloader.errors import LoginRequiredError, MoodleApiError
from monash_moodle_downloader.models import Course, Section
from monash_moodle_downloader.settings import Settings

runner = CliRunner()


def test_help_lists_public_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("login", "doctor", "courses", "menu", "scan", "sync"):
        assert command in result.stdout


def test_sync_requires_exactly_one_target() -> None:
    missing = runner.invoke(app, ["sync"])
    conflicting = runner.invoke(app, ["sync", "--course", "FIT2014", "--all"])

    assert missing.exit_code != 0
    assert "Choose exactly one" in missing.output
    assert conflicting.exit_code != 0
    assert "Choose exactly one" in conflicting.output


def test_sync_rejects_week_with_all_courses() -> None:
    result = runner.invoke(app, ["sync", "--all", "--week", "2"])

    assert result.exit_code != 0
    assert "--week cannot be combined with --all" in result.output


def test_sync_passes_options_to_async_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict[str, object]] = []

    async def fake_sync(**options: object) -> None:
        received.append(options)

    monkeypatch.setattr(cli, "_sync", fake_sync)
    result = runner.invoke(
        app,
        ["sync", "--course", "FIT2014", "--week", "2", "--refresh"],
    )

    assert result.exit_code == 0
    assert received == [
        {
            "course_selector": "FIT2014",
            "all_courses": False,
            "week": 2,
            "refresh": True,
            "output": None,
        }
    ]


def test_scan_accepts_course_and_week(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[str, int | None, Path | None]] = []

    async def fake_scan(course_selector: str, *, week: int | None, output: Path | None) -> None:
        received.append((course_selector, week, output))

    monkeypatch.setattr(cli, "_scan", fake_scan)
    result = runner.invoke(app, ["scan", "--course", "FIT2102", "--week", "3"])

    assert result.exit_code == 0
    assert received == [("FIT2102", 3, None)]


def menu_courses() -> list[Course]:
    return [
        Course(1, "FIT2001", "Current unit"),
        Course(2, "FIT1001", "Old unit", removed_from_view=True),
    ]


@pytest.mark.asyncio
async def test_menu_login_check_keeps_authenticated_session_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeSession:
        def __init__(self, _settings: object, *, headless: bool) -> None:
            events.append(f"start:{headless}")

        async def __aenter__(self) -> "FakeSession":
            events.append("enter")
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            events.append("exit")

        async def status(self) -> object:
            events.append("status")
            return type("Status", (), {"authenticated": True})()

    async def fake_login(_timeout: int) -> None:
        events.append("login")

    monkeypatch.setattr(cli, "BrowserSession", FakeSession)
    monkeypatch.setattr(cli, "_login", fake_login)

    await cli._ensure_menu_authenticated(Settings.default())

    assert events == ["start:True", "enter", "status", "exit"]


@pytest.mark.asyncio
async def test_menu_login_check_closes_headless_session_before_opening_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeSession:
        def __init__(self, _settings: object, *, headless: bool) -> None:
            events.append(f"start:{headless}")

        async def __aenter__(self) -> "FakeSession":
            events.append("enter")
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            events.append("exit")

        async def status(self) -> object:
            events.append("status")
            return type("Status", (), {"authenticated": False})()

    async def fake_login(timeout: int) -> None:
        events.append(f"login:{timeout}")

    monkeypatch.setattr(cli, "BrowserSession", FakeSession)
    monkeypatch.setattr(cli, "_login", fake_login)

    await cli._ensure_menu_authenticated(Settings.default())

    assert events == ["start:True", "enter", "status", "exit", "login:600"]


@pytest.mark.asyncio
async def test_menu_stops_when_automatic_login_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_login_check(_settings: object) -> None:
        raise LoginRequiredError("Login window closed")

    class UnexpectedMenuSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("The menu session must not start after a failed login")

    monkeypatch.setattr(cli, "_ensure_menu_authenticated", failed_login_check)
    monkeypatch.setattr(cli, "BrowserSession", UnexpectedMenuSession)

    with pytest.raises(LoginRequiredError, match="Login window closed"):
        await cli._menu()


@pytest.mark.asyncio
async def test_menu_updates_current_courses_then_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    courses = menu_courses()
    selections = iter(["all", None])
    loaded: list[int] = []
    synced: list[tuple[int, cli.MenuScope]] = []
    monkeypatch.setattr(cli, "_prompt_course_selection", lambda _courses: next(selections))

    async def load_course(course: Course) -> Course:
        loaded.append(course.id)
        return course

    async def sync_course(course: Course, scope: cli.MenuScope) -> None:
        synced.append((course.id, scope))

    await cli._menu_loop(courses, load_course=load_course, sync_course=sync_course)

    assert loaded == [1]
    assert synced == [(1, None)]


@pytest.mark.asyncio
async def test_menu_runs_two_week_operations_before_manual_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course = Course(
        1,
        "FIT2001",
        "Current unit",
        sections=[
            Section(10, 10, "Week 1 - Start"),
            Section(20, 20, "Week 2 - Continue"),
        ],
    )
    selections = iter([course, course, None])
    actions = iter(["week", "week"])
    week_choices = iter([[1], [2]])
    synced: list[cli.MenuScope] = []
    monkeypatch.setattr(cli, "_prompt_course_selection", lambda _courses: next(selections))
    monkeypatch.setattr(cli, "_prompt_course_action", lambda _course: next(actions))
    monkeypatch.setattr(cli, "_prompt_weeks", lambda _course: next(week_choices))

    async def load_course(selected: Course) -> Course:
        return selected

    async def sync_course(_course: Course, scope: cli.MenuScope) -> None:
        synced.append(scope)

    await cli._menu_loop([course], load_course=load_course, sync_course=sync_course)

    assert synced == [[1], [2]]


@pytest.mark.asyncio
async def test_menu_sends_multiple_weeks_as_one_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course = Course(1, "FIT2001", "Current unit")
    selections = iter([course, None])
    synced: list[cli.MenuScope] = []
    monkeypatch.setattr(cli, "_prompt_course_selection", lambda _courses: next(selections))
    monkeypatch.setattr(cli, "_prompt_course_action", lambda _course: "week")
    monkeypatch.setattr(cli, "_prompt_weeks", lambda _course: [3, 7, 8])

    async def load_course(selected: Course) -> Course:
        return selected

    async def sync_course(_course: Course, scope: cli.MenuScope) -> None:
        synced.append(scope)

    await cli._menu_loop([course], load_course=load_course, sync_course=sync_course)

    assert synced == [[3, 7, 8]]


@pytest.mark.asyncio
async def test_menu_can_sync_general_content(monkeypatch: pytest.MonkeyPatch) -> None:
    course = Course(1, "FIT2001", "Current unit")
    selections = iter([course, None])
    synced: list[cli.MenuScope] = []
    monkeypatch.setattr(cli, "_prompt_course_selection", lambda _courses: next(selections))
    monkeypatch.setattr(cli, "_prompt_course_action", lambda _course: "general")

    async def load_course(selected: Course) -> Course:
        return selected

    async def sync_course(_course: Course, scope: cli.MenuScope) -> None:
        synced.append(scope)

    await cli._menu_loop([course], load_course=load_course, sync_course=sync_course)

    assert synced == ["general"]


@pytest.mark.asyncio
async def test_menu_returns_after_a_recoverable_sync_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course = Course(1, "FIT2001", "Current unit")
    selections = iter([course, None])
    monkeypatch.setattr(cli, "_prompt_course_selection", lambda _courses: next(selections))
    monkeypatch.setattr(cli, "_prompt_course_action", lambda _course: "course")

    async def load_course(selected: Course) -> Course:
        return selected

    async def sync_course(_course: Course, _scope: cli.MenuScope) -> None:
        raise MoodleApiError("Temporary problem")

    await cli._menu_loop([course], load_course=load_course, sync_course=sync_course)


def test_prompt_weeks_retries_locally_and_supports_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course = Course(
        1,
        "FIT2001",
        "Current unit",
        sections=[
            Section(10, 10, "Week 0 - Orientation"),
            Section(20, 20, "Week 2 - Types"),
            Section(30, 30, "Week 3 - More types"),
        ],
    )
    responses = iter(["letters", "100", "0,2-3"])
    monkeypatch.setattr(typer, "prompt", lambda *_args, **_kwargs: next(responses))

    assert cli._prompt_weeks(course) == [0, 2, 3]

    monkeypatch.setattr(typer, "prompt", lambda *_args, **_kwargs: "b")
    assert cli._prompt_weeks(course) is None
