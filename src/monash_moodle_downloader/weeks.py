"""Available-week discovery and terminal selection parsing."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from monash_moodle_downloader.content import week_number
from monash_moodle_downloader.models import Section

WEEK_ITEM_PATTERN = re.compile(r"^(\d+)(?:-(\d+))?$")
SELECTION_TRANSLATION = str.maketrans("，、;；～~－﹣–—−", ",,,,-------")


@dataclass(frozen=True, slots=True)
class WeekOption:
    """One visible Week root section offered by the terminal menu."""

    number: int
    title: str


def available_weeks(sections: Iterable[Section]) -> list[WeekOption]:
    """Return unique visible Week roots in numeric order."""
    found: dict[int, WeekOption] = {}
    for section in sections:
        number = week_number(section.title)
        if section.visible and number is not None:
            found.setdefault(number, WeekOption(number, section.title))
    return [found[number] for number in sorted(found)]


def format_week_ranges(numbers: Iterable[int]) -> str:
    """Compress Week numbers into a readable range list."""
    ordered = sorted(set(numbers))
    if not ordered:
        return "none"
    ranges: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(_format_range(start, previous))
        start = previous = number
    ranges.append(_format_range(start, previous))
    return ", ".join(ranges)


def parse_week_selection(value: str, available: Iterable[int]) -> list[int]:
    """Parse single Weeks, ranges, and comma-separated combinations."""
    normalised = re.sub(r"\s+", "", value.translate(SELECTION_TRANSLATION))
    if not normalised:
        raise ValueError("Enter at least one Week number, or `b` to go back.")

    allowed = set(available)
    if not allowed:
        raise ValueError("This course has no available Weeks.")
    selected: set[int] = set()
    for item in normalised.split(","):
        match = WEEK_ITEM_PATTERN.fullmatch(item)
        if match is None:
            raise ValueError("Use numbers and ranges such as `3,7-8`.")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if end < start:
            raise ValueError(f"Week range {start}-{end} runs backwards.")
        if start < min(allowed) or end > max(allowed):
            raise ValueError(f"Unavailable Week range: {start}-{end}.")
        selected.update(range(start, end + 1))

    unavailable = sorted(selected - allowed)
    if unavailable:
        rendered = ", ".join(str(number) for number in unavailable)
        raise ValueError(f"Unavailable Week number(s): {rendered}.")
    return sorted(selected)


def _format_range(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"
