"""Command-line interface for Monash Moodle Downloader."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from rich.console import Console
from rich.table import Table

from monash_moodle_downloader.cache import ResourceCache
from monash_moodle_downloader.content import CourseContentScanner
from monash_moodle_downloader.downloader import ResourceDownloader, SyncCounts
from monash_moodle_downloader.errors import MmdError
from monash_moodle_downloader.models import Course
from monash_moodle_downloader.moodle import MoodleAjaxClient
from monash_moodle_downloader.output import write_scan_output
from monash_moodle_downloader.session import BrowserSession
from monash_moodle_downloader.settings import Settings
from monash_moodle_downloader.weeks import (
    available_weeks,
    format_week_ranges,
    parse_week_selection,
)

LoadCourse = Callable[[Course], Awaitable[Course]]
MenuScope = list[int] | Literal["general"] | None
SyncCourse = Callable[[Course, MenuScope], Awaitable[None]]

app = typer.Typer(
    name="mmd",
    help="Download authorised Monash Moodle resources for offline study.",
    no_args_is_help=True,
)
console = Console()
MENU_LOGIN_TIMEOUT_SECONDS = 600


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
    """List current and removed-from-view Moodle courses."""
    _run(_courses())


async def _courses() -> None:
    settings = Settings.default()
    async with BrowserSession(settings, headless=True) as session:
        await session.ensure_authenticated()
        client = MoodleAjaxClient(session.page, base_url=settings.moodle_base_url)
        visible_courses = await client.list_courses()

    current = [course for course in visible_courses if not course.removed_from_view]
    removed = [course for course in visible_courses if course.removed_from_view]
    _print_course_table("Current courses", current)
    _print_course_table("Removed from view", removed)


def _print_course_table(title: str, courses_to_show: list[Course]) -> None:
    table = Table(title=title)
    table.add_column("Code", style="cyan", no_wrap=True)
    table.add_column("Moodle ID", justify="right")
    table.add_column("Course name")
    for course in courses_to_show:
        table.add_row(course.code, str(course.id), course.name)
    if not courses_to_show:
        table.add_row("—", "—", "No courses")
    console.print(table)


@app.command()
def menu() -> None:
    """Open an interactive terminal menu for course synchronisation."""
    _run(_menu())


async def _menu() -> None:
    settings = Settings.default()
    await _ensure_menu_authenticated(settings)
    async with BrowserSession(settings, headless=True) as session:
        await session.ensure_authenticated()
        client = MoodleAjaxClient(session.page, base_url=settings.moodle_base_url)
        courses_to_show = await client.list_courses(include_removed=True)
        scanner = CourseContentScanner(session.page, base_url=settings.moodle_base_url)

        with ResourceCache(settings.database) as cache:
            downloader = await ResourceDownloader.from_page(
                session.page,
                cache,
                settings.output_root,
            )
            async with downloader:

                async def sync_course(course: Course, scope: MenuScope) -> None:
                    await _sync_populated_course(
                        course,
                        weeks=scope if isinstance(scope, list) else None,
                        general=scope == "general",
                        scanner=scanner,
                        downloader=downloader,
                        output_root=settings.output_root,
                        refresh=False,
                    )

                await _menu_loop(
                    courses_to_show,
                    load_course=client.get_course_state,
                    sync_course=sync_course,
                )


async def _ensure_menu_authenticated(settings: Settings) -> None:
    """Open the interactive login flow when the menu has no saved session."""
    async with BrowserSession(settings, headless=True) as session:
        status = await session.status()
    if status.authenticated:
        return

    console.print(
        "[yellow]The saved Moodle session is missing or expired. Opening Chrome to log in.[/yellow]"
    )
    await _login(MENU_LOGIN_TIMEOUT_SECONDS)


async def _menu_loop(
    courses_to_show: list[Course],
    *,
    load_course: LoadCourse,
    sync_course: SyncCourse,
) -> None:
    while True:
        selection = _prompt_course_selection(courses_to_show)
        if selection is None:
            console.print("Exiting Moodle Downloader.")
            return
        try:
            if isinstance(selection, str):
                current = [course for course in courses_to_show if not course.removed_from_view]
                for course in current:
                    await sync_course(await load_course(course), None)
                continue

            action = _prompt_course_action(selection)
            if action is None:
                continue
            populated = await load_course(selection)
            selected_weeks = None
            if action == "week":
                selected_weeks = _prompt_weeks(populated)
                if selected_weeks is None:
                    continue
            scope: MenuScope = "general" if action == "general" else selected_weeks
            await sync_course(populated, scope)
        except MmdError as error:
            console.print(f"[red]Error:[/red] {error}")
            console.print("Returning to course selection.")


def _prompt_course_selection(courses_to_show: list[Course]) -> Course | str | None:
    current = [course for course in courses_to_show if not course.removed_from_view]
    removed = [course for course in courses_to_show if course.removed_from_view]
    console.print("\n[bold]What would you like to update?[/bold]")
    console.print("  [cyan]1[/cyan]  All current courses")
    numbered: dict[int, Course] = {}
    next_number = 2
    console.print("\n[bold]Current courses[/bold]")
    for course in current:
        numbered[next_number] = course
        console.print(f"  [cyan]{next_number}[/cyan]  {course.code} — {course.name}")
        next_number += 1
    console.print("\n[bold]Removed from view[/bold]")
    for course in removed:
        numbered[next_number] = course
        console.print(f"  [cyan]{next_number}[/cyan]  {course.code} — {course.name}")
        next_number += 1
    console.print("\n  [cyan]0[/cyan]  Exit")

    while True:
        choice = typer.prompt("Enter a number", type=int)
        if choice == 0:
            return None
        if choice == 1:
            return "all"
        if choice in numbered:
            return numbered[choice]
        console.print("[yellow]Choose one of the numbers shown above.[/yellow]")


def _prompt_course_action(course: Course) -> str | None:
    console.print(f"\n[bold]{course.code} — {course.name}[/bold]")
    console.print("  [cyan]1[/cyan]  Update the entire course")
    console.print("  [cyan]2[/cyan]  Update one or more specific Weeks")
    console.print("  [cyan]3[/cyan]  Update General (non-Week content)")
    console.print("  [cyan]0[/cyan]  Back to course selection")
    while True:
        choice = typer.prompt("Enter a number", type=int)
        if choice == 0:
            return None
        if choice == 1:
            return "course"
        if choice == 2:
            return "week"
        if choice == 3:
            return "general"
        console.print("[yellow]Choose 0, 1, 2, or 3.[/yellow]")


def _prompt_weeks(course: Course) -> list[int] | None:
    options = available_weeks(course.sections)
    if not options:
        console.print("[yellow]This course has no visible Week sections.[/yellow]")
        return None
    console.print("\n[bold]Available Weeks[/bold]")
    for option in options:
        console.print(f"  [cyan]{option.number}[/cyan]  {option.title}")
    available_numbers = [option.number for option in options]
    console.print(f"Available: {format_week_ranges(available_numbers)}")
    console.print("Examples: 3, 3-5, 3,7-8. Enter b to go back.")
    while True:
        value = typer.prompt("Enter Week selection", type=str)
        if value.strip().casefold() == "b":
            return None
        try:
            return parse_week_selection(value, available_numbers)
        except ValueError as error:
            console.print(f"[yellow]{error}[/yellow]")
            console.print(f"Available: {format_week_ranges(available_numbers)}")


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

    destination = write_scan_output(manifest, output_root, on_warning=_print_output_warning)
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
        typer.Option(
            "--all",
            help="Synchronise every current course, excluding Remove from view.",
        ),
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
            courses_to_sync = await client.list_courses(include_removed=False)
        else:
            assert course_selector is not None
            courses_to_sync = [await client.resolve_course(course_selector)]

        with ResourceCache(settings.database) as cache:
            downloader = await ResourceDownloader.from_page(session.page, cache, output_root)
            async with downloader:
                for course in courses_to_sync:
                    populated = await client.get_course_state(course)
                    scanner = CourseContentScanner(session.page, base_url=settings.moodle_base_url)
                    await _sync_populated_course(
                        populated,
                        weeks=[week] if week is not None else None,
                        general=False,
                        scanner=scanner,
                        downloader=downloader,
                        output_root=output_root,
                        refresh=refresh,
                    )


async def _sync_populated_course(
    course: Course,
    *,
    weeks: list[int] | None,
    general: bool = False,
    scanner: CourseContentScanner,
    downloader: ResourceDownloader,
    output_root: Path,
    refresh: bool,
) -> None:
    manifest = await scanner.scan(course, weeks=weeks, general=general)
    counts = await downloader.sync(manifest, refresh=refresh)
    destination = write_scan_output(manifest, output_root, on_warning=_print_output_warning)
    _print_sync_result(course.code, destination, counts)


def _print_output_warning(message: str) -> None:
    console.print(f"[yellow]Warning:[/yellow] {message}")


def _print_sync_result(code: str, destination: Path, counts: SyncCounts) -> None:
    console.print(f"[green]Sync complete:[/green] {code}")
    console.print(f"Downloaded: {counts.downloaded}")
    console.print(f"Unchanged: {counts.unchanged}")
    console.print(f"Skipped media: {counts.skipped_media}")
    console.print(f"Unsupported pages: {counts.unsupported}")
    console.print(f"Missing remotely: {counts.missing_remote}")
    console.print(f"Failed: {counts.failed}")
    console.print(f"Output: {destination}")
