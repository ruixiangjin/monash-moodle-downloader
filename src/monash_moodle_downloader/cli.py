"""Command-line interface for Monash Moodle Downloader."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from monash_moodle_downloader.cache import ResourceCache
from monash_moodle_downloader.content import CourseContentScanner
from monash_moodle_downloader.downloader import ResourceDownloader, SyncCounts
from monash_moodle_downloader.errors import MmdError
from monash_moodle_downloader.moodle import MoodleAjaxClient
from monash_moodle_downloader.output import write_scan_output
from monash_moodle_downloader.session import BrowserSession
from monash_moodle_downloader.settings import Settings

app = typer.Typer(
    name="mmd",
    help="Download authorised Monash Moodle resources for offline study.",
    no_args_is_help=True,
)
console = Console()


def _run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(awaitable)
    except MmdError as error:
        console.print(f"[red]Error:[/red] {error}")
        raise typer.Exit(code=1) from error


@app.command()
def login(
    timeout: Annotated[
        int,
        typer.Option("--timeout", min=30, help="Seconds to wait for browser login."),
    ] = 600,
) -> None:
    """Open the browser login flow for Monash SSO and MFA."""
    _run(_login(timeout))


async def _login(timeout: int) -> None:
    settings = Settings.default()
    console.print("Opening Chrome. Complete Monash SSO/MFA only in the browser window.")
    async with BrowserSession(settings, headless=False) as session:
        await session.open_moodle()
        status = await session.wait_for_login(timeout_seconds=timeout)
    console.print(f"[green]{status.message}[/green]")


@app.command()
def doctor() -> None:
    """Check local requirements, session state, and Moodle connectivity."""
    _run(_doctor())


async def _doctor() -> None:
    settings = Settings.default()
    console.print("Python project: [green]ready[/green]")
    console.print(f"Default output: {settings.output_root}")
    console.print(f"Private state: {settings.state_root}")
    async with BrowserSession(settings, headless=True) as session:
        status = await session.status()
    console.print("Google Chrome: [green]available[/green]")
    colour = "green" if status.authenticated else "yellow"
    console.print(f"Moodle session: [{colour}]{status.message}[/{colour}]")
    if not status.authenticated:
        console.print("Run `mmd login` to create or renew the saved session.")


@app.command()
def courses() -> None:
    """List courses visible to the authenticated Moodle account."""
    _run(_courses())


async def _courses() -> None:
    settings = Settings.default()
    async with BrowserSession(settings, headless=True) as session:
        await session.ensure_authenticated()
        client = MoodleAjaxClient(session.page, base_url=settings.moodle_base_url)
        visible_courses = await client.list_courses()

    table = Table(title="Visible Moodle courses")
    table.add_column("Code", style="cyan", no_wrap=True)
    table.add_column("Moodle ID", justify="right")
    table.add_column("Course name")
    table.add_column("Visible", justify="center")
    for course in visible_courses:
        table.add_row(course.code, str(course.id), course.name, "yes" if course.visible else "no")
    console.print(table)


@app.command()
def scan(
    course: Annotated[str, typer.Option("--course", "-c", help="Course code or Moodle ID.")],
    week: Annotated[int | None, typer.Option("--week", min=1)] = None,
    output: Annotated[Path | None, typer.Option("--output", file_okay=False)] = None,
) -> None:
    """Inspect one course without downloading its resources."""
    _run(_scan(course, week=week, output=output))


async def _scan(course_selector: str, *, week: int | None, output: Path | None) -> None:
    settings = Settings.default()
    output_root = output if output is not None else settings.output_root
    async with BrowserSession(settings, headless=True) as session:
        await session.ensure_authenticated()
        client = MoodleAjaxClient(session.page, base_url=settings.moodle_base_url)
        course = await client.resolve_course(course_selector)
        course = await client.get_course_state(course)
        scanner = CourseContentScanner(session.page, base_url=settings.moodle_base_url)
        manifest = await scanner.scan(course, week=week)

    destination = write_scan_output(manifest, output_root)
    activities = [
        activity for section in manifest.course.sections for activity in section.activities
    ]
    resources = sum(len(activity.resources) for activity in activities)
    links = sum(len(activity.external_links) for activity in activities)
    console.print(f"[green]Scan complete:[/green] {manifest.course.code}")
    console.print(f"Sections: {len(manifest.course.sections)}")
    console.print(f"Activities: {len(activities)}")
    console.print(f"File candidates: {resources}")
    console.print(f"Recorded links: {links}")
    console.print(f"Output: {destination}")
    console.print("No attachment bodies were downloaded.")


@app.command()
def sync(
    course: Annotated[
        str | None,
        typer.Option("--course", "-c", help="Course code or Moodle ID."),
    ] = None,
    all_courses: Annotated[
        bool,
        typer.Option("--all", help="Synchronise every currently visible course."),
    ] = False,
    week: Annotated[int | None, typer.Option("--week", min=1)] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Ignore cached remote metadata."),
    ] = False,
    output: Annotated[Path | None, typer.Option("--output", file_okay=False)] = None,
) -> None:
    """Synchronise one course, or every course when --all is explicit."""
    if (course is None) == (not all_courses):
        raise typer.BadParameter("Choose exactly one of --course or --all.")
    if all_courses and week is not None:
        raise typer.BadParameter("--week cannot be combined with --all.")

    _run(
        _sync(
            course_selector=course,
            all_courses=all_courses,
            week=week,
            refresh=refresh,
            output=output,
        )
    )


async def _sync(
    *,
    course_selector: str | None,
    all_courses: bool,
    week: int | None,
    refresh: bool,
    output: Path | None,
) -> None:
    settings = Settings.default()
    output_root = output if output is not None else settings.output_root
    async with BrowserSession(settings, headless=True) as session:
        await session.ensure_authenticated()
        client = MoodleAjaxClient(session.page, base_url=settings.moodle_base_url)
        if all_courses:
            courses_to_sync = await client.list_courses()
        else:
            assert course_selector is not None
            courses_to_sync = [await client.resolve_course(course_selector)]

        with ResourceCache(settings.database) as cache:
            downloader = await ResourceDownloader.from_page(session.page, cache, output_root)
            async with downloader:
                for course in courses_to_sync:
                    populated = await client.get_course_state(course)
                    scanner = CourseContentScanner(session.page, base_url=settings.moodle_base_url)
                    manifest = await scanner.scan(populated, week=week)
                    counts = await downloader.sync(manifest, refresh=refresh)
                    destination = write_scan_output(manifest, output_root)
                    _print_sync_result(course.code, destination, counts)


def _print_sync_result(code: str, destination: Path, counts: SyncCounts) -> None:
    console.print(f"[green]Sync complete:[/green] {code}")
    console.print(f"Downloaded: {counts.downloaded}")
    console.print(f"Unchanged: {counts.unchanged}")
    console.print(f"Skipped media: {counts.skipped_media}")
    console.print(f"Unsupported pages: {counts.unsupported}")
    console.print(f"Missing remotely: {counts.missing_remote}")
    console.print(f"Failed: {counts.failed}")
    console.print(f"Output: {destination}")
