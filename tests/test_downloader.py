from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from monash_moodle_downloader.cache import ResourceCache
from monash_moodle_downloader.downloader import (
    ResourceDownloader,
    content_disposition_filename,
    validate_public_url,
)
from monash_moodle_downloader.errors import LoginRequiredError, MoodleApiError
from monash_moodle_downloader.models import (
    Activity,
    ActivityType,
    Course,
    ExternalLink,
    Resource,
    ResourceStatus,
    Section,
    SyncManifest,
)
from monash_moodle_downloader.progress import ProgressUpdate


async def allow_test_url(_url: str) -> None:
    return None


def manifest_with_resource(url: str, *, name: str = "Lecture notes") -> SyncManifest:
    resource = Resource("100:one", name, url, "moodle_resource")
    activity = Activity(100, name, ActivityType.FILE, 700, source_url=url, resources=[resource])
    section = Section(
        700,
        7,
        "Week 1 - Introduction",
        parent_number=3,
        activities=[activity],
    )
    return SyncManifest(course=Course(77, "FIT0000", "FIT0000 Example Unit", sections=[section]))


@pytest.mark.asyncio
async def test_download_cache_and_missing_local_file_recovery(tmp_path: Path) -> None:
    source = "https://learning.monash.edu/mod/resource/view.php?id=100"
    target = "https://cdn.example/opaque"
    body = b"PDF!"
    calls = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == source:
            return httpx.Response(302, headers={"location": target}, request=request)
        headers = {
            "content-type": "application/pdf",
            "content-disposition": 'attachment; filename="notes.pdf"',
            "content-length": str(len(body)),
            "etag": '"v1"',
        }
        if request.method == "GET":
            calls["get"] += 1
        return httpx.Response(200, headers=headers, content=body, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        with ResourceCache(tmp_path / "state.sqlite3") as cache:
            downloader = ResourceDownloader(
                client,
                cache,
                tmp_path / "downloads",
                url_validator=allow_test_url,
            )
            manifest = manifest_with_resource(source)

            updates: list[ProgressUpdate] = []
            first = await downloader.sync(manifest, progress=updates.append)
            resource = manifest.course.sections[0].activities[0].resources[0]
            assert resource.local_path is not None
            destination = tmp_path / "downloads" / "FIT0000 Example Unit" / resource.local_path
            second = await downloader.sync(manifest)
            destination.unlink()
            third = await downloader.sync(manifest)

    assert any(
        update.phase == "resources"
        and update.completed == 1
        and update.total == 1
        and update.finished
        for update in updates
    )
    assert first.downloaded == 1
    assert second.unchanged == 1
    assert third.downloaded == 1
    assert calls["get"] == 2
    assert destination.read_bytes() == body
    assert resource.sha256 is not None


@pytest.mark.asyncio
async def test_media_is_skipped_without_get(tmp_path: Path) -> None:
    source = "https://learning.monash.edu/mod/resource/view.php?id=100"
    calls = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls["get"] += 1
        return httpx.Response(
            200,
            headers={
                "content-type": "image/png",
                "content-disposition": 'attachment; filename="diagram.png"',
                "content-length": "3",
            },
            content=b"png",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with ResourceCache(tmp_path / "state.sqlite3") as cache:
            downloader = ResourceDownloader(
                client,
                cache,
                tmp_path / "downloads",
                url_validator=allow_test_url,
            )
            manifest = manifest_with_resource(source)
            counts = await downloader.sync(manifest)

    resource = manifest.course.sections[0].activities[0].resources[0]
    assert counts.skipped_media == 1
    assert resource.status is ResourceStatus.SKIPPED_MEDIA
    assert calls["get"] == 0
    assert not (tmp_path / "downloads").exists()


@pytest.mark.asyncio
async def test_partial_download_is_removed_after_retries(tmp_path: Path) -> None:
    source = "https://learning.monash.edu/file.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="broken.pdf"',
                "content-length": "5",
                "etag": '"broken"',
            },
            content=b"bad",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with ResourceCache(tmp_path / "state.sqlite3") as cache:
            downloader = ResourceDownloader(
                client,
                cache,
                tmp_path / "downloads",
                url_validator=allow_test_url,
            )
            manifest = manifest_with_resource(source)
            counts = await downloader.sync(manifest)

    assert counts.failed == 1
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("broken.pdf"))


@pytest.mark.asyncio
async def test_external_file_candidate_is_promoted_and_downloaded(tmp_path: Path) -> None:
    target = "https://files.example/worksheet.csv"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/csv",
                "content-disposition": 'attachment; filename="worksheet.csv"',
                "content-length": "4",
                "etag": '"csv1"',
            },
            content=b"a,b\n",
            request=request,
        )

    link = ExternalLink("105:link", "Worksheet", target, link_type="file_candidate")
    activity = Activity(105, "Worksheet", ActivityType.URL, 700, external_links=[link])
    section = Section(700, 7, "Week 1 - Introduction", activities=[activity])
    manifest = SyncManifest(
        course=Course(77, "FIT0000", "FIT0000 Example Unit", sections=[section])
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with ResourceCache(tmp_path / "state.sqlite3") as cache:
            downloader = ResourceDownloader(
                client,
                cache,
                tmp_path / "downloads",
                url_validator=allow_test_url,
            )
            counts = await downloader.sync(manifest)

    assert counts.downloaded == 1
    assert activity.resources[0].source_kind == "external_direct_file"
    assert list((tmp_path / "downloads").rglob("worksheet.csv"))


@pytest.mark.asyncio
async def test_private_external_addresses_are_rejected() -> None:
    with pytest.raises(MoodleApiError, match="Private or local"):
        await validate_public_url("http://127.0.0.1/private.pdf")
    with pytest.raises(MoodleApiError, match="Local resource"):
        await validate_public_url("http://localhost/private.pdf")


def test_content_disposition_supports_utf8_and_plain_names() -> None:
    assert content_disposition_filename("attachment; filename=notes.pdf") == "notes.pdf"
    assert content_disposition_filename("attachment; filename*=UTF-8''Week%201.zip") == "Week 1.zip"


@pytest.mark.asyncio
async def test_missing_remote_keeps_local_file_and_cached_path(tmp_path: Path) -> None:
    source = "https://learning.monash.edu/file.pdf"
    available = {"value": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if not available["value"]:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="kept.pdf"',
                "content-length": "4",
                "etag": '"kept"',
            },
            content=b"kept",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with ResourceCache(tmp_path / "state.sqlite3") as cache:
            downloader = ResourceDownloader(
                client,
                cache,
                tmp_path / "downloads",
                url_validator=allow_test_url,
            )
            first_manifest = manifest_with_resource(source)
            await downloader.sync(first_manifest)
            first_resource = first_manifest.course.sections[0].activities[0].resources[0]
            assert first_resource.local_path is not None
            destination = (
                tmp_path / "downloads" / "FIT0000 Example Unit" / first_resource.local_path
            )
            available["value"] = False
            second_manifest = manifest_with_resource(source)
            counts = await downloader.sync(second_manifest)
            entry = cache.get(77, source)

    assert counts.missing_remote == 1
    assert destination.read_bytes() == b"kept"
    assert entry is not None
    assert Path(entry.local_path) == destination


@pytest.mark.asyncio
async def test_moodle_authorisation_failure_requests_login(tmp_path: Path) -> None:
    source = "https://learning.monash.edu/file.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with ResourceCache(tmp_path / "state.sqlite3") as cache:
            downloader = ResourceDownloader(
                client,
                cache,
                tmp_path / "downloads",
                url_validator=allow_test_url,
            )
            with pytest.raises(LoginRequiredError, match="mmd login"):
                await downloader.sync(manifest_with_resource(source))
