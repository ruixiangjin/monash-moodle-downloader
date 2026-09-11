"""Command-line interface for Monash Moodle Downloader."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

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


@app.command()
def login() -> None:
    """Open the browser login flow for Monash SSO and MFA."""
    _planned("login")


@app.command()
def doctor() -> None:
    """Check local requirements, session state, and Moodle connectivity."""
    settings = Settings.default()
    console.print("Project structure: [green]ready[/green]")
    console.print(f"Default output: {settings.output_root}")
    console.print(f"Private state: {settings.state_root}")
    _planned("doctor")


@app.command()
def courses() -> None:
    """List courses visible to the authenticated Moodle account."""
    _planned("courses")


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
