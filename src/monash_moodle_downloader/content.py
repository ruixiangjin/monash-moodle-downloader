"""Extract readable course content and resource candidates without downloading files."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import PurePosixPath
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

from bs4 import BeautifulSoup
from bs4.element import Tag
from markdownify import markdownify
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from monash_moodle_downloader.errors import MoodleApiError
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

WEEK_PATTERN = re.compile(r"^\s*Week\s+(\d+)\b", re.IGNORECASE)
MEDIA_EXTENSIONS = {
    ".aac",
    ".avi",
    ".bmp",
    ".flac",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".ogg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
}
SENSITIVE_QUERY_KEYS = {
    "authorization",
    "credential",
    "expires",
    "key-pair-id",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-signature",
}
TRACKING_QUERY_PREFIXES = ("utm_",)
DROP_SELECTORS = (
    "script, style, noscript, svg, canvas, video, audio, picture, source, img, iframe, "
    "button, form, input, .accesshide, .activityiconcontainer, .completion-info, "
    ".activity-completion, .edit-menu, .dropdown, [aria-hidden='true'], "
    "a[href^='#collapse'], [data-bs-toggle='collapse'], [data-toggle='collapse']"
)


class CourseContentScanner:
    """Enrich API course metadata with readable HTML and candidate links."""

    def __init__(self, page: Page, *, base_url: str) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")

    async def scan(
        self,
        course: Course,
        *,
        week: int | None = None,
        weeks: Iterable[int] | None = None,
    ) -> SyncManifest:
        """Scan course pages and return a manifest without downloading attachment bodies."""
        selected = select_sections(course.sections, week=week, weeks=weeks)
        page_groups: dict[str, list[Section]] = defaultdict(list)
        for section in selected:
            page_groups[self._section_page_url(course, section)].append(section)

        for page_url, sections in page_groups.items():
            html = await self._get_html(page_url)
            soup = BeautifulSoup(html, "lxml")
            for section in sections:
                self._parse_section(soup, section)

        for section in selected:
            for activity in section.activities:
                await self._enrich_activity(activity)

        deduplicate_activity_text(selected)
        course.sections = selected
        return SyncManifest(course=course)

    async def _get_html(self, url: str) -> str:
        try:
            response = await self.page.request.get(
                url,
                fail_on_status_code=False,
                timeout=60_000,
            )
        except PlaywrightError as error:
            raise MoodleApiError(
                f"Moodle page could not be read: {safe_manifest_url(url)}"
            ) from error
        if response.status >= 400:
            raise MoodleApiError(
                f"Moodle page returned HTTP {response.status}: {safe_manifest_url(url)}"
            )
        return await response.text()

    def _section_page_url(self, course: Course, section: Section) -> str:
        if section.source_url:
            parsed = urlparse(section.source_url)
            return urlunparse(parsed._replace(fragment=""))
        return f"{self.base_url}/course/view.php?id={course.id}&section={section.number}"

    def _parse_section(self, soup: BeautifulSoup, section: Section) -> None:
        container = soup.select_one(f".course-content #section-{section.number}")
        if container is None:
            container = soup.select_one(f'.course-content [data-id="{section.id}"]')
        if not isinstance(container, Tag):
            return

        section_summary = container.select_one(":scope > .content > .summary, :scope > .summary")
        if isinstance(section_summary, Tag):
            section.text_markdown = clean_html(section_summary)
            section.text_hash = text_hash(section.text_markdown)

        for activity in section.activities:
            node = container.select_one(f"#module-{activity.id}")
            if node is None:
                node = container.select_one(f'[data-for="cmitem"][data-id="{activity.id}"]')
            if not isinstance(node, Tag):
                continue
            activity.text_markdown = extract_activity_text(node)
            self._collect_embedded_links(node, activity)

    async def _enrich_activity(self, activity: Activity) -> None:
        if not activity.source_url:
            return

        if activity.activity_type is ActivityType.FILE:
            add_resource(
                activity,
                name=activity.name,
                url=activity.source_url,
                source_kind="moodle_resource",
            )
            return

        if activity.activity_type in {
            ActivityType.ASSIGNMENT,
            ActivityType.FOLDER,
            ActivityType.PAGE,
        }:
            try:
                html = await self._get_html(activity.source_url)
            except MoodleApiError:
                add_external_link(
                    activity,
                    name=activity.name,
                    url=activity.source_url,
                    link_type="moodle_error",
                    status=ResourceStatus.FAILED,
                )
                return
            soup = BeautifulSoup(html, "lxml")
            self._parse_activity_page(soup, activity)
            return

        if activity.activity_type is ActivityType.URL:
            await self._resolve_url_activity(activity)
            return

        if activity.activity_type in {
            ActivityType.EXTERNAL_TOOL,
            ActivityType.FORUM,
            ActivityType.H5P,
            ActivityType.QUIZ,
            ActivityType.UNKNOWN,
        }:
            add_external_link(
                activity,
                name=activity.name,
                url=activity.source_url,
                link_type=activity.activity_type.value,
            )

    def _parse_activity_page(self, soup: BeautifulSoup, activity: Activity) -> None:
        selector = {
            ActivityType.ASSIGNMENT: "#intro",
            ActivityType.PAGE: "#region-main .box.generalbox, #region-main .generalbox",
            ActivityType.FOLDER: "#intro",
        }[activity.activity_type]
        detail = soup.select_one(selector)
        if isinstance(detail, Tag):
            page_text = clean_html(detail)
            activity.text_markdown = combine_markdown(activity.text_markdown, page_text)

        source_kind = {
            ActivityType.ASSIGNMENT: "assignment_attachment",
            ActivityType.FOLDER: "folder_file",
            ActivityType.PAGE: "page_attachment",
        }[activity.activity_type]
        for anchor in soup.select('a[href*="/pluginfile.php/"]'):
            if not isinstance(anchor, Tag):
                continue
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            url = urljoin(self.base_url, href)
            add_resource(
                activity,
                name=link_name(anchor, url),
                url=url,
                source_kind=source_kind,
                relative_path=folder_relative_path(url)
                if activity.activity_type is ActivityType.FOLDER
                else None,
            )

    async def _resolve_url_activity(self, activity: Activity) -> None:
        if activity.source_url is None:
            return
        target: str | None = None
        try:
            response = await self.page.request.get(
                activity.source_url,
                fail_on_status_code=False,
                max_redirects=0,
                timeout=30_000,
            )
            if 300 <= response.status < 400:
                location = response.headers.get("location")
                if location:
                    target = urljoin(activity.source_url, location)
            elif response.status < 400:
                content_type = response.headers.get("content-type", "").casefold()
                if "text/html" in content_type:
                    target = first_external_link(await response.text(), self.base_url)
        except PlaywrightError:
            target = None

        if target is None:
            add_external_link(
                activity,
                name=activity.name,
                url=activity.source_url,
                link_type="moodle_url",
                status=ResourceStatus.FAILED,
            )
            return
        add_external_link(
            activity,
            name=activity.name,
            url=target,
            link_type=classify_external_link(target),
        )

    def _collect_embedded_links(self, node: Tag, activity: Activity) -> None:
        for anchor in node.select("a[href]"):
            if not isinstance(anchor, Tag):
                continue
            href = anchor.get("href")
            if not isinstance(href, str) or href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = urljoin(self.base_url, href)
            parsed = urlparse(url)
            if "/pluginfile.php/" in parsed.path:
                add_resource(
                    activity,
                    name=link_name(anchor, url),
                    url=url,
                    source_kind="inline_attachment",
                )
            elif parsed.netloc and parsed.netloc != urlparse(self.base_url).netloc:
                add_external_link(
                    activity,
                    name=link_name(anchor, url),
                    url=url,
                    link_type=classify_external_link(url),
                )


def select_sections(
    sections: Iterable[Section],
    *,
    week: int | None = None,
    weeks: Iterable[int] | None = None,
) -> list[Section]:
    """Select requested Week roots and their children, or all visible sections."""
    if week is not None and weeks is not None:
        raise ValueError("Choose either week or weeks, not both.")
    visible = [section for section in sections if section.visible]
    requested = set(weeks) if weeks is not None else ({week} if week is not None else None)
    if requested is None:
        return sorted(visible, key=lambda section: (section.number, section.id))
    if not requested:
        raise MoodleApiError("At least one Week must be selected.")
    roots = [section for section in visible if week_number(section.title) in requested]
    found = {week_number(section.title) for section in roots}
    missing = sorted(requested - found)
    if missing:
        rendered = ", ".join(str(number) for number in missing)
        raise MoodleApiError(f"Week {rendered} was not found in this course.")
    root_numbers = {root.number for root in roots}
    selected = list(roots)
    selected.extend(section for section in visible if section.parent_number in root_numbers)
    return sorted(selected, key=lambda section: (section.number, section.id))


def week_number(title: str) -> int | None:
    match = WEEK_PATTERN.search(title)
    return int(match.group(1)) if match else None


def extract_activity_text(node: Tag) -> str | None:
    """Prefer the rich activity body, avoiding overlapping nested descriptions."""
    alt = node.select_one(".activity-altcontent")
    if isinstance(alt, Tag):
        return clean_html(alt)
    fragments = [
        clean_html(fragment)
        for fragment in node.select(".activity-description, .description")
        if isinstance(fragment, Tag)
    ]
    return combine_markdown(*fragments)


def clean_html(node: Tag) -> str | None:
    """Convert meaningful Moodle HTML to compact Markdown without media or controls."""
    fragment = BeautifulSoup(str(node), "lxml")
    for unwanted in fragment.select(DROP_SELECTORS):
        unwanted.decompose()
    for anchor in fragment.select("a[href]"):
        href = anchor.get("href")
        if isinstance(href, str) and href.startswith(("javascript:", "data:")):
            del anchor["href"]
    converted = markdownify(str(fragment), heading_style="ATX", bullets="-")
    lines = [line.rstrip() for line in converted.replace("\xa0", " ").splitlines()]
    compact: list[str] = []
    for line in lines:
        if not line.strip() and (not compact or not compact[-1].strip()):
            continue
        compact.append(line)
    result = "\n".join(compact).strip()
    return result or None


def combine_markdown(*parts: str | None) -> str | None:
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        normalised = part.strip()
        if normalised and normalised not in seen:
            unique.append(normalised)
            seen.add(normalised)
    return "\n\n".join(unique) or None


def text_hash(text: str | None) -> str | None:
    if not text:
        return None
    normalised = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def deduplicate_activity_text(sections: Iterable[Section]) -> None:
    seen: dict[str, int] = {}
    for section in sections:
        for activity in section.activities:
            activity.text_hash = text_hash(activity.text_markdown)
            if activity.text_hash is None:
                continue
            first = seen.get(activity.text_hash)
            if first is None:
                seen[activity.text_hash] = activity.id
            else:
                activity.text_markdown = None
                activity.text_duplicate_of = first


def add_resource(
    activity: Activity,
    *,
    name: str,
    url: str,
    source_kind: str,
    relative_path: str | None = None,
) -> None:
    normalised = normalise_url(url)
    if any(normalise_url(item.source_url) == normalised for item in activity.resources):
        return
    activity.resources.append(
        Resource(
            source_id=f"{activity.id}:{short_hash(normalised)}",
            name=name,
            source_url=normalised,
            source_kind=source_kind,
            status=ResourceStatus.SKIPPED_MEDIA
            if is_media_url(normalised)
            else ResourceStatus.PENDING,
            relative_path=relative_path,
        )
    )


def add_external_link(
    activity: Activity,
    *,
    name: str,
    url: str,
    link_type: str,
    status: ResourceStatus = ResourceStatus.LINK_ONLY,
) -> None:
    safe_url = safe_manifest_url(url)
    if any(item.source_url == safe_url for item in activity.external_links):
        return
    activity.external_links.append(
        ExternalLink(
            source_id=f"{activity.id}:{short_hash(safe_url)}",
            name=name,
            source_url=safe_url,
            link_type=link_type,
            final_host=urlparse(url).hostname,
            status=status,
        )
    )


def first_external_link(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base_url).netloc
    preferred = soup.select(".urlworkaround a[href], .resourceworkaround a[href]")
    candidates = preferred or soup.select("#region-main a[href]")
    for anchor in candidates:
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).netloc not in {"", base_host}:
            return url
    return None


def classify_external_link(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith("canva.com"):
        return "canva"
    if "panopto" in host:
        return "panopto"
    if host.endswith("youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith("vimeo.com"):
        return "vimeo"
    if is_media_url(url):
        return "media"
    if PurePosixPath(urlparse(url).path).suffix:
        return "file_candidate"
    return "webpage"


def folder_relative_path(url: str) -> str | None:
    marker = "/mod_folder/content/0/"
    path = urlparse(url).path
    if marker not in path:
        return None
    relative = unquote(path.split(marker, 1)[1]).strip("/")
    return relative or None


def link_name(anchor: Tag, url: str) -> str:
    text = " ".join(anchor.get_text(" ", strip=True).split())
    filename = unquote(PurePosixPath(urlparse(url).path).name)
    return text or filename or "Untitled resource"


def is_media_url(url: str) -> bool:
    return PurePosixPath(urlparse(url).path).suffix.casefold() in MEDIA_EXTENSIONS


def normalise_url(url: str) -> str:
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith(TRACKING_QUERY_PREFIXES)
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def safe_manifest_url(url: str) -> str:
    parsed = urlparse(normalise_url(url))
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in SENSITIVE_QUERY_KEYS and not key.casefold().startswith("x-amz-")
    ]
    return urlunparse(parsed._replace(query=urlencode(query)))


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
