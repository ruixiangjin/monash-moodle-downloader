"""Safe incremental file downloads for scanned Moodle resources."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import mimetypes
import re
import socket
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Page

from monash_moodle_downloader.cache import CacheEntry, ResourceCache, metadata_matches
from monash_moodle_downloader.content import add_resource, is_media_url, normalise_url
from monash_moodle_downloader.errors import LoginRequiredError, MoodleApiError
from monash_moodle_downloader.models import (
    Activity,
    ActivityType,
    Course,
    Resource,
    ResourceStatus,
    Section,
    SyncManifest,
)
from monash_moodle_downloader.output import course_directory_for, resource_relative_path

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MEDIA_MIME_PREFIXES = ("audio/", "font/", "image/", "video/")
HTML_MIME_TYPES = {"application/xhtml+xml", "text/html"}
CONTENT_DISPOSITION_FILENAME_STAR = re.compile(r"filename\*\s*=\s*([^;]+)", re.IGNORECASE)
CONTENT_DISPOSITION_FILENAME = re.compile(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', re.IGNORECASE)
MIME_EXTENSIONS = {
    "application/java-archive": ".jar",
    "application/pdf": ".pdf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/zip": ".zip",
    "text/csv": ".csv",
}

UrlValidator = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RemoteMetadata:
    request_url: str
    final_host: str | None
    filename: str
    mime_type: str
    content_length: int | None
    etag: str | None
    last_modified: str | None
    content_disposition: str | None
    content_encoding: str | None

    @property
    def is_attachment(self) -> bool:
        return bool(
            self.content_disposition and "attachment" in self.content_disposition.casefold()
        )


@dataclass(slots=True)
class SyncCounts:
    downloaded: int = 0
    unchanged: int = 0
    skipped_media: int = 0
    unsupported: int = 0
    missing_remote: int = 0
    failed: int = 0

    def add(self, status: ResourceStatus) -> None:
        field = {
            ResourceStatus.DOWNLOADED: "downloaded",
            ResourceStatus.UNCHANGED: "unchanged",
            ResourceStatus.SKIPPED_MEDIA: "skipped_media",
            ResourceStatus.UNSUPPORTED: "unsupported",
            ResourceStatus.MISSING_REMOTE: "missing_remote",
            ResourceStatus.FAILED: "failed",
        }.get(status)
        if field:
            setattr(self, field, getattr(self, field) + 1)


class ResourceDownloader:
    """Resolve, classify, and download resources with bounded concurrency."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: ResourceCache,
        output_root: Path,
        *,
        url_validator: UrlValidator,
        concurrency: int = 4,
        owns_client: bool = False,
    ) -> None:
        self.client = client
        self.cache = cache
        self.output_root = output_root
        self.url_validator = url_validator
        self.semaphore = asyncio.Semaphore(concurrency)
        self.owns_client = owns_client
        self.safe_hosts: set[str] = set()
        self._path_lock = asyncio.Lock()
        self._used_paths: dict[Path, str] = {}

    @classmethod
    async def from_page(
        cls,
        page: Page,
        cache: ResourceCache,
        output_root: Path,
        *,
        concurrency: int = 4,
    ) -> ResourceDownloader:
        cookie_jar = httpx.Cookies()
        for cookie in await page.context.cookies():
            cookie_jar.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
            )
        user_agent = await page.evaluate("() => navigator.userAgent")
        headers = {"User-Agent": user_agent} if isinstance(user_agent, str) else {}
        client = httpx.AsyncClient(
            cookies=cookie_jar,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(60.0, connect=20.0),
        )
        downloader = cls(
            client,
            cache,
            output_root,
            url_validator=validate_public_url,
            concurrency=concurrency,
            owns_client=True,
        )
        return downloader

    async def __aenter__(self) -> ResourceDownloader:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def sync(self, manifest: SyncManifest, *, refresh: bool = False) -> SyncCounts:
        """Download each unique non-media resource and copy results to duplicate references."""
        await self._promote_external_files(manifest.course)
        grouped: dict[str, list[tuple[Section, Activity, Resource]]] = defaultdict(list)
        for section, activity, resource in iter_resources(manifest.course):
            grouped[normalise_url(resource.source_url)].append((section, activity, resource))

        counts = SyncCounts()

        async def handle(items: list[tuple[Section, Activity, Resource]]) -> None:
            section, activity, primary = items[0]
            async with self.semaphore:
                await self._sync_one(
                    manifest.course,
                    section,
                    activity,
                    primary,
                    refresh=refresh,
                )
            for _, _, duplicate in items[1:]:
                copy_resource_result(primary, duplicate)
            counts.add(primary.status)

        await asyncio.gather(*(handle(items) for items in grouped.values()))
        return counts

    async def _promote_external_files(self, course: Course) -> None:
        for section in course.sections:
            for activity in section.activities:
                for link in activity.external_links:
                    should_probe = link.link_type == "file_candidate" or (
                        activity.activity_type is ActivityType.URL
                        and link.link_type not in {"canva", "media", "panopto", "vimeo", "youtube"}
                    )
                    if not should_probe:
                        continue
                    try:
                        response = await self._request_follow("HEAD", link.source_url)
                    except (httpx.HTTPError, MoodleApiError):
                        continue
                    try:
                        metadata = metadata_from_response(response, fallback_name=link.name)
                    finally:
                        await response.aclose()
                    if is_downloadable(metadata):
                        add_resource(
                            activity,
                            name=metadata.filename,
                            url=link.source_url,
                            source_kind="external_direct_file",
                        )

    async def _sync_one(
        self,
        course: Course,
        section: Section,
        activity: Activity,
        resource: Resource,
        *,
        refresh: bool,
    ) -> None:
        if resource.status is ResourceStatus.SKIPPED_MEDIA or is_media_url(resource.source_url):
            resource.status = ResourceStatus.SKIPPED_MEDIA
            return

        cache_key = normalise_url(resource.source_url)
        cached = self.cache.get(course.id, cache_key)
        try:
            resolved_url, metadata = await self._resolve_metadata(resource)
        except RemoteMissingError:
            resource.status = ResourceStatus.MISSING_REMOTE
            if cached is not None:
                resource.etag = cached.etag
                resource.last_modified = cached.last_modified
                resource.size = cached.content_length
                resource.sha256 = cached.sha256
                cached_path = Path(cached.local_path)
                course_directory = course_directory_for(course, self.output_root)
                try:
                    resource.local_path = cached_path.relative_to(course_directory).as_posix()
                except ValueError:
                    resource.local_path = None
                self._cache_result(course.id, resource, cached_path, None)
            else:
                self._cache_result(course.id, resource, None, None)
            return
        except (httpx.HTTPError, MoodleApiError):
            resource.status = ResourceStatus.FAILED
            return

        apply_metadata(resource, metadata)
        if is_media_metadata(metadata):
            resource.status = ResourceStatus.SKIPPED_MEDIA
            self._cache_result(course.id, resource, None, metadata)
            return
        if not is_downloadable(metadata):
            resource.status = ResourceStatus.UNSUPPORTED
            self._cache_result(course.id, resource, None, metadata)
            return

        destination = await self._destination(course, section, activity, resource)
        resource.local_path = destination.relative_to(
            course_directory_for(course, self.output_root)
        ).as_posix()
        if (
            not refresh
            and cached is not None
            and Path(cached.local_path) == destination
            and destination.is_file()
            and metadata_matches(
                cached,
                etag=metadata.etag,
                last_modified=metadata.last_modified,
                content_length=metadata.content_length,
            )
        ):
            resource.sha256 = cached.sha256
            resource.status = ResourceStatus.UNCHANGED
            return

        try:
            await self._download_with_retries(resolved_url, destination, resource, metadata)
        except RemoteMissingError:
            resource.status = ResourceStatus.MISSING_REMOTE
        except (httpx.HTTPError, OSError, MoodleApiError):
            resource.status = ResourceStatus.FAILED
        self._cache_result(course.id, resource, destination, metadata)

    async def _resolve_metadata(self, resource: Resource) -> tuple[str, RemoteMetadata]:
        response = await self._request_follow("HEAD", resource.source_url)
        try:
            if response.status_code in {404, 410}:
                raise RemoteMissingError
            if response.status_code >= 400 and response.status_code != 405:
                raise_for_resource_status(response)
            metadata = metadata_from_response(response, fallback_name=resource.name)
            resolved_url = str(response.url)
            head_status = response.status_code
        finally:
            await response.aclose()

        if head_status == 405 or (
            metadata.mime_type in HTML_MIME_TYPES and not metadata.is_attachment
        ):
            pluginfile = await self._resolve_html_wrapper(resource.source_url)
            if pluginfile:
                response = await self._request_follow("HEAD", pluginfile)
                try:
                    if response.status_code in {404, 410}:
                        raise RemoteMissingError
                    raise_for_resource_status(response)
                    metadata = metadata_from_response(response, fallback_name=resource.name)
                    resolved_url = pluginfile
                finally:
                    await response.aclose()
        return resolved_url, metadata

    async def _resolve_html_wrapper(self, url: str) -> str | None:
        response = await self._request_follow("GET", url, stream=True)
        try:
            if response.status_code >= 400:
                raise_for_resource_status(response)
                return None
            content_type = response.headers.get("content-type", "").casefold()
            if "text/html" not in content_type:
                return str(response.url)
            body = await response.aread()
        finally:
            await response.aclose()
        soup = BeautifulSoup(body, "lxml")
        anchor = soup.select_one('a[href*="/pluginfile.php/"]')
        href = anchor.get("href") if anchor is not None else None
        return urljoin(url, href) if isinstance(href, str) else None

    async def _download_with_retries(
        self,
        url: str,
        destination: Path,
        resource: Resource,
        metadata: RemoteMetadata,
    ) -> None:
        for attempt in range(3):
            try:
                await self._download(url, destination, resource, metadata)
                return
            except RemoteMissingError:
                raise
            except (httpx.HTTPError, OSError, MoodleApiError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))

    async def _download(
        self,
        url: str,
        destination: Path,
        resource: Resource,
        head_metadata: RemoteMetadata,
    ) -> None:
        response = await self._request_follow("GET", url, stream=True)
        temporary = destination.with_name(f"{destination.name}.part")
        try:
            if response.status_code in {404, 410}:
                raise RemoteMissingError
            raise_for_resource_status(response)
            metadata = metadata_from_response(response, fallback_name=head_metadata.filename)
            if is_media_metadata(metadata):
                resource.status = ResourceStatus.SKIPPED_MEDIA
                return
            if not is_downloadable(metadata):
                resource.status = ResourceStatus.UNSUPPORTED
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
            digest = hashlib.sha256()
            size = 0
            with temporary.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if (
                metadata.content_length is not None
                and metadata.content_encoding is None
                and size != metadata.content_length
            ):
                raise MoodleApiError(
                    f"Downloaded size mismatch for {resource.name}: "
                    f"expected {metadata.content_length}, received {size}."
                )
            temporary.replace(destination)
            resource.size = size
            resource.sha256 = digest.hexdigest()
            resource.mime_type = metadata.mime_type
            resource.etag = metadata.etag
            resource.last_modified = metadata.last_modified
            resource.final_host = metadata.final_host
            resource.status = ResourceStatus.DOWNLOADED
        finally:
            await response.aclose()
            if resource.status is not ResourceStatus.DOWNLOADED:
                temporary.unlink(missing_ok=True)

    async def _request_follow(
        self,
        method: str,
        url: str,
        *,
        stream: bool = False,
    ) -> httpx.Response:
        current = url
        for _ in range(6):
            await self._validate_once(current)
            request = self.client.build_request(method, current)
            response = await self.client.send(request, stream=stream)
            if response.status_code not in REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            next_url = urljoin(str(response.url), location)
            await response.aclose()
            current = next_url
            if response.status_code == 303 and method != "HEAD":
                method = "GET"
        raise MoodleApiError("Too many redirects while resolving a resource.")

    async def _validate_once(self, url: str) -> None:
        host = (urlparse(url).hostname or "").casefold()
        if host in self.safe_hosts:
            return
        await self.url_validator(url)
        self.safe_hosts.add(host)

    async def _destination(
        self,
        course: Course,
        section: Section,
        activity: Activity,
        resource: Resource,
    ) -> Path:
        relative = resource_relative_path(course, section, activity, resource)
        course_directory = course_directory_for(course, self.output_root)
        candidate = course_directory / relative
        source = normalise_url(resource.source_url)
        async with self._path_lock:
            existing = self._used_paths.get(candidate)
            if existing is not None and existing != source:
                suffix = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
                candidate = candidate.with_name(f"{candidate.stem}-{suffix}{candidate.suffix}")
            self._used_paths[candidate] = source
        return candidate

    def _cache_result(
        self,
        course_id: int,
        resource: Resource,
        destination: Path | None,
        metadata: RemoteMetadata | None,
    ) -> None:
        local_path = str(destination) if destination is not None else resource.local_path or ""
        self.cache.put(
            CacheEntry(
                course_id=course_id,
                source_url=normalise_url(resource.source_url),
                local_path=local_path,
                etag=resource.etag or (metadata.etag if metadata else None),
                last_modified=resource.last_modified
                or (metadata.last_modified if metadata else None),
                content_length=(
                    resource.size
                    if resource.size is not None
                    else metadata.content_length
                    if metadata
                    else None
                ),
                sha256=resource.sha256,
                status=resource.status.value,
            )
        )


class RemoteMissingError(Exception):
    """Internal signal for HTTP 404/410 without deleting local content."""


def raise_for_resource_status(response: httpx.Response) -> None:
    """Turn an expired Moodle session into an actionable CLI error."""
    if response.status_code in {401, 403} and response.url.host == "learning.monash.edu":
        raise LoginRequiredError("The saved Moodle session expired. Run `mmd login` again.")
    response.raise_for_status()


def iter_resources(course: Course) -> Iterable[tuple[Section, Activity, Resource]]:
    for section in course.sections:
        for activity in section.activities:
            for resource in activity.resources:
                yield section, activity, resource


def copy_resource_result(source: Resource, target: Resource) -> None:
    target.name = source.name
    target.status = source.status
    target.local_path = source.local_path
    target.mime_type = source.mime_type
    target.size = source.size
    target.etag = source.etag
    target.last_modified = source.last_modified
    target.sha256 = source.sha256
    target.final_host = source.final_host


def apply_metadata(resource: Resource, metadata: RemoteMetadata) -> None:
    resource.name = metadata.filename
    resource.mime_type = metadata.mime_type
    resource.size = metadata.content_length
    resource.etag = metadata.etag
    resource.last_modified = metadata.last_modified
    resource.final_host = metadata.final_host


def metadata_from_response(response: httpx.Response, *, fallback_name: str) -> RemoteMetadata:
    mime_type = response.headers.get("content-type", "application/octet-stream")
    mime_type = mime_type.split(";", 1)[0].strip().casefold()
    disposition = response.headers.get("content-disposition")
    filename = content_disposition_filename(disposition)
    if not filename:
        candidate = unquote(PurePosixPath(response.url.path).name)
        filename = candidate if PurePosixPath(candidate).suffix else fallback_name
    if not PurePosixPath(filename).suffix:
        extension = MIME_EXTENSIONS.get(mime_type) or mimetypes.guess_extension(mime_type)
        if extension:
            filename = f"{filename}{extension}"
    return RemoteMetadata(
        request_url=str(response.url),
        final_host=response.url.host,
        filename=filename,
        mime_type=mime_type,
        content_length=header_int(response.headers.get("content-length")),
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        content_disposition=disposition,
        content_encoding=response.headers.get("content-encoding"),
    )


def content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    star = CONTENT_DISPOSITION_FILENAME_STAR.search(value)
    if star:
        encoded = star.group(1).strip().strip('"')
        if "''" in encoded:
            encoded = encoded.split("''", 1)[1]
        return unquote(encoded).replace("\\", "/").rsplit("/", 1)[-1]
    plain = CONTENT_DISPOSITION_FILENAME.search(value)
    if plain:
        filename = (plain.group(1) or plain.group(2) or "").strip()
        return filename.replace("\\", "/").rsplit("/", 1)[-1] or None
    return None


def is_media_metadata(metadata: RemoteMetadata) -> bool:
    return metadata.mime_type.startswith(MEDIA_MIME_PREFIXES) or is_media_url(metadata.filename)


def is_downloadable(metadata: RemoteMetadata) -> bool:
    if is_media_metadata(metadata):
        return False
    return metadata.mime_type not in HTML_MIME_TYPES or metadata.is_attachment


def header_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


async def validate_public_url(url: str) -> None:
    """Reject local/private targets before following course-provided external links."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MoodleApiError("Only public HTTP/HTTPS resource URLs are allowed.")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".local"):
        raise MoodleApiError("Local resource URLs are not allowed.")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            records = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError as error:
            raise MoodleApiError(f"Resource host could not be resolved: {host}") from error
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    ):
        raise MoodleApiError("Private or local resource addresses are not allowed.")
