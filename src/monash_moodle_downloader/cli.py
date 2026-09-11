"""Command-line interface for Monash Moodle Downloader."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from monash_moodle_downloader.errors import MmdError
from monash_moodle_downloader.moodle import MoodleAjaxClient
from monash_moodle_downloader.session import BrowserSession
from monash_moodle_downloader.settings import Settings

app = typer.Typer(
    name="mmd",
    help="Download authorised Monash Moodle resources for offline study.",
    no_args_is_help=True,
)
console = Console()


def _planned(command: str) -> None:
    console.print(
        f"[yellow]{command}[/yellow] is part of the staged implementation and is not connected "
        "to Moodle yet."
    )


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
    target = f"{course}, week {week}" if week is not None else course
    console.print(f"Scan target: {target}")
    if output is not None:
        console.print(f"Output override: {output}")
    _planned("scan")


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

    target = "all visible courses" if all_courses else course
    console.print(f"Sync target: {target}")
    if week is not None:
        console.print(f"Week: {week}")
    if refresh:
        console.print("Remote metadata cache: bypassed")
    if output is not None:
        console.print(f"Output override: {output}")
    _planned("sync")
