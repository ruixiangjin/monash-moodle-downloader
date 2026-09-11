import stat
from pathlib import Path

from monash_moodle_downloader.cache import CacheEntry, ResourceCache, metadata_matches


def test_cache_round_trip_and_metadata_priority(tmp_path: Path) -> None:
    database = tmp_path / "private" / "state.sqlite3"
    entry = CacheEntry(
        course_id=77,
        source_url="https://learning.monash.edu/file/1",
        local_path="/tmp/notes.pdf",
        etag='"v1"',
        last_modified="Mon, 01 Sep 2026 00:00:00 GMT",
        content_length=4,
        sha256="abcd",
        status="downloaded",
    )

    with ResourceCache(database) as cache:
        cache.put(entry)
        restored = cache.get(77, entry.source_url)

    assert restored == entry
    assert restored is not None
    assert metadata_matches(restored, etag='"v1"', last_modified=None, content_length=None)
    assert not metadata_matches(
        restored,
        etag='"v2"',
        last_modified=entry.last_modified,
        content_length=entry.content_length,
    )
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_cache_requires_date_and_size_without_etag(tmp_path: Path) -> None:
    entry = CacheEntry(1, "https://example.com/a", "/tmp/a", None, "date", 10, None, "ok")

    assert metadata_matches(entry, etag=None, last_modified="date", content_length=10)
    assert not metadata_matches(entry, etag=None, last_modified="date", content_length=11)
    assert not metadata_matches(entry, etag=None, last_modified=None, content_length=10)
