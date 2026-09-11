import stat
import subprocess
from pathlib import Path


def test_macos_launcher_is_executable_and_valid_zsh() -> None:
    launcher = Path(__file__).parents[1] / "Monash Moodle Downloader.command"

    assert launcher.is_file()
    assert stat.S_IMODE(launcher.stat().st_mode) & stat.S_IXUSR
    result = subprocess.run(
        ["/bin/zsh", "-n", str(launcher)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "/Users/" not in launcher.read_text(encoding="utf-8")
