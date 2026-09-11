from pathlib import Path

import pytest
from typer.testing import CliRunner

from monash_moodle_downloader import cli
from monash_moodle_downloader.cli import app
from monash_moodle_downloader.models import Course

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


def test_menu_updates_all_current_courses(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict[str, object]] = []

    async def fake_load() -> list[Course]:
        return menu_courses()

    async def fake_sync(**options: object) -> None:
        received.append(options)

    monkeypatch.setattr(cli, "_load_menu_courses", fake_load)
    monkeypatch.setattr(cli, "_sync", fake_sync)

    result = runner.invoke(app, ["menu"], input="1\n")

    assert result.exit_code == 0
    assert "Current courses" in result.output
    assert "Removed from view" in result.output
    assert received == [
        {
            "course_selector": None,
            "all_courses": True,
            "week": None,
            "refresh": False,
            "output": None,
        }
    ]


def test_menu_selects_removed_course_and_week(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict[str, object]] = []

    async def fake_load() -> list[Course]:
        return menu_courses()

    async def fake_sync(**options: object) -> None:
        received.append(options)

    monkeypatch.setattr(cli, "_load_menu_courses", fake_load)
    monkeypatch.setattr(cli, "_sync", fake_sync)

    result = runner.invoke(app, ["menu"], input="3\n2\n4\n")

    assert result.exit_code == 0
    assert "FIT1001" in result.output
    assert received == [
        {
            "course_selector": "2",
            "all_courses": False,
            "week": 4,
            "refresh": False,
            "output": None,
        }
    ]
