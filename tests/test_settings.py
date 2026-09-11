from pathlib import Path

from monash_moodle_downloader.settings import Settings


def test_default_output_is_outside_the_repository() -> None:
    settings = Settings.default(home=Path("/Users/example"))

    assert settings.output_root == Path("/Users/example/Desktop/Monash Moodle Downloads")
    assert settings.database.name == "state.sqlite3"
    assert settings.browser_profile.name == "playwright-profile"
