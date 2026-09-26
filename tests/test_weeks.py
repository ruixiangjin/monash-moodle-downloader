import pytest

from monash_moodle_downloader.models import Section
from monash_moodle_downloader.weeks import (
    available_weeks,
    format_week_ranges,
    parse_week_selection,
)


def test_available_weeks_uses_visible_root_titles_and_supports_zero() -> None:
    sections = [
        Section(1, 10, "Week 0 - Orientation"),
        Section(2, 11, "Own-time", parent_number=10),
        Section(3, 20, "Week 2 - Types"),
        Section(4, 30, "Week 3 - Hidden", visible=False),
    ]

    options = available_weeks(sections)

    assert [(option.number, option.title) for option in options] == [
        (0, "Week 0 - Orientation"),
        (2, "Week 2 - Types"),
    ]
    assert format_week_ranges(option.number for option in options) == "0, 2"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3", [3]),
        ("3-5", [3, 4, 5]),
        ("3~5", [3, 4, 5]),
        ("3～5", [3, 4, 5]),
        ("3, 4-5, 3", [3, 4, 5]),
        ("3，7-8", [3, 7, 8]),
        ("3、7－8", [3, 7, 8]),
        ("3；7—8", [3, 7, 8]),
        ("0-1", [0, 1]),
    ],
)
def test_parse_week_selection_supports_ranges_and_combinations(
    value: str,
    expected: list[int],
) -> None:
    assert parse_week_selection(value, range(14)) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "at least one"),
        ("week 3", "numbers and ranges"),
        ("3,,4", "numbers and ranges"),
        ("5-3", "backwards"),
        ("3-5", "Unavailable"),
    ],
)
def test_parse_week_selection_rejects_invalid_or_unavailable_values(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_week_selection(value, {0, 1, 2, 7, 8})


def test_format_week_ranges_preserves_gaps() -> None:
    assert format_week_ranges([0, 1, 2, 4, 7, 8]) == "0-2, 4, 7-8"


def test_parse_week_selection_rejects_huge_range_without_expanding_it() -> None:
    with pytest.raises(ValueError, match="Unavailable Week range"):
        parse_week_selection("0-999999999999", range(14))
