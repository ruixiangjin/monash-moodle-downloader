from io import StringIO

from rich.console import Console

from monash_moodle_downloader.progress import ProgressUpdate, TerminalProgress


def test_redirected_progress_uses_plain_english_counts() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    with TerminalProgress(console) as display:
        display.update(ProgressUpdate("resources", "Checking resources", completed=0, total=2))
        display.update(ProgressUpdate("resources", "Checking resources", completed=1, total=2))
        display.update(
            ProgressUpdate("resources", "Checking resources", completed=2, total=2, finished=True)
        )

    assert output.getvalue().splitlines() == [
        "Checking resources [0/2]",
        "Checking resources [1/2]",
        "Checking resources [2/2] - done",
    ]
