"""Application paths that keep private state and course material outside Git."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True, slots=True)
class Settings:
    """Filesystem locations used by the downloader."""

    output_root: Path
    state_root: Path

    @classmethod
    def default(cls, *, home: Path | None = None) -> Settings:
        """Build settings without creating or modifying any directories."""
        resolved_home = home if home is not None else Path.home()
        return cls(
            output_root=resolved_home / "Desktop" / "Monash Moodle Downloads",
            state_root=Path(user_data_path("Monash Moodle Downloader", "ruixiangjin")),
        )

    @property
    def browser_profile(self) -> Path:
        return self.state_root / "playwright-profile"

    @property
    def database(self) -> Path:
        return self.state_root / "state.sqlite3"
