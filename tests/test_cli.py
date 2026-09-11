from typer.testing import CliRunner

from monash_moodle_downloader.cli import app

runner = CliRunner()


def test_help_lists_public_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("login", "doctor", "courses", "scan", "sync"):
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


def test_scan_accepts_course_and_week() -> None:
    result = runner.invoke(app, ["scan", "--course", "FIT2102", "--week", "3"])

    assert result.exit_code == 0
    assert "FIT2102, week 3" in result.stdout
