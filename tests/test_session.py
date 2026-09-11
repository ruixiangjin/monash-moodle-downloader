import json
from pathlib import Path

from monash_moodle_downloader.session import is_moodle_page, load_saved_cookies


def test_is_moodle_page_requires_the_exact_origin() -> None:
    base = "https://learning.monash.edu"

    assert is_moodle_page("https://learning.monash.edu/my/courses.php", base)
    assert not is_moodle_page("https://learning.monash.edu.evil.example/", base)
    assert not is_moodle_page("http://learning.monash.edu/", base)


def test_load_saved_cookies_handles_valid_and_damaged_state(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    damaged = tmp_path / "damaged.json"
    valid.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "MoodleSession",
                        "value": "test-only-value",
                        "domain": "learning.monash.edu",
                        "path": "/",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    damaged.write_text("not-json", encoding="utf-8")

    assert load_saved_cookies(valid)[0]["name"] == "MoodleSession"
    assert load_saved_cookies(damaged) == []
    assert load_saved_cookies(tmp_path / "missing.json") == []
