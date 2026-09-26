"""Terminal progress for Moodle scans and synchronisation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    phase: str
    description: str
    completed: int | None = None
    total: int | None = None
    finished: bool = False


ProgressCallback = Callable[[ProgressUpdate], None]


def ignore_progress(_update: ProgressUpdate) -> None:
    pass


def scoped_progress(callback: ProgressCallback, scope: str) -> ProgressCallback:
    def report(update: ProgressUpdate) -> None:
        callback(replace(update, phase=f"{scope}:{update.phase}"))

    return report


class TerminalProgress:
    def __init__(self, console: Console) -> None:
        self.console = console
        self.interactive = console.is_terminal
        self._tasks: dict[str, TaskID] = {}
        self._last_plain: dict[str, str] = {}
        self._progress = Progress(
            SpinnerColumn(finished_text="[green]OK[/green]"),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=False,
        )

    def __enter__(self) -> TerminalProgress:
        if self.interactive:
            self._progress.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self.interactive:
            self._progress.stop()

    def update(self, event: ProgressUpdate) -> None:
        if not self.interactive:
            message = self._plain_message(event)
            if self._last_plain.get(event.phase) != message:
                self.console.print(message)
                self._last_plain[event.phase] = message
            return

        task_id = self._tasks.get(event.phase)
        if task_id is None:
            task_id = self._progress.add_task(
                event.description,
                total=event.total,
                completed=event.completed or 0,
            )
            self._tasks[event.phase] = task_id
        else:
            self._progress.update(task_id, description=event.description)

        if event.total is not None:
            self._progress.update(task_id, total=event.total)
        if event.completed is not None:
            self._progress.update(task_id, completed=event.completed)
        if event.finished:
            if event.total is None:
                completed = event.completed if event.completed is not None else 1
                total = max(completed, 1)
                self._progress.update(task_id, total=total, completed=total)
            elif event.total == 0:
                self._progress.update(task_id, total=1, completed=1)
            else:
                self._progress.update(task_id, completed=event.total)

    @staticmethod
    def _plain_message(event: ProgressUpdate) -> str:
        if event.total is not None:
            count = f" [{event.completed or 0}/{event.total}]"
        elif event.completed is not None:
            count = f" [{event.completed}]"
        else:
            count = ""
        suffix = " - done" if event.finished else ""
        return f"{event.description}{count}{suffix}"
