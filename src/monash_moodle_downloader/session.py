"""Persistent browser authentication for Monash Moodle."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from monash_moodle_downloader.errors import BrowserUnavailableError, LoginRequiredError
from monash_moodle_downloader.settings import Settings


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """A safe session summary that never contains cookies or a session key."""

    authenticated: bool
    current_url: str
    message: str


def is_moodle_page(url: str, base_url: str) -> bool:
    """Return whether a URL belongs to the configured Moodle origin."""
    current = urlparse(url)
    expected = urlparse(base_url)
    return current.scheme == expected.scheme and current.netloc == expected.netloc


class BrowserSession:
    """Own a persistent Chrome context backed by private application data."""

    def __init__(self, settings: Settings, *, headless: bool) -> None:
        self.settings = settings
        self.headless = headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser session has not been started.")
        return self._page

    async def start(self) -> None:
        """Start Chrome without reading credentials from the terminal."""
        self.settings.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.browser_profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.settings.browser_profile,
                channel="chrome",
                headless=self.headless,
                locale="en-AU",
                accept_downloads=False,
                viewport={"width": 1360, "height": 900} if self.headless else None,
            )
        except PlaywrightError as error:
            await self._playwright.stop()
            self._playwright = None
            raise BrowserUnavailableError(
                "Google Chrome could not be opened. Close any downloader Chrome window and "
                "check that Chrome is installed."
            ) from error

        saved_cookies = load_saved_cookies(self.settings.storage_state)
        if saved_cookies:
            await self._context.add_cookies(cast(Any, saved_cookies))
        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )

    async def close(self) -> None:
        """Close browser resources while preserving the persistent profile."""
        if self._context is not None:
            await self._save_storage_state()
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def open_moodle(self) -> None:
        """Open the Moodle course dashboard and follow normal SSO redirects."""
        try:
            await self.page.goto(
                f"{self.settings.moodle_base_url}/my/courses.php",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except PlaywrightError as error:
            raise LoginRequiredError(
                "Moodle could not be reached. Check the network connection and try again."
            ) from error

    async def status(self, *, navigate: bool = True) -> SessionStatus:
        """Inspect authentication without returning sensitive session values."""
        if navigate:
            await self.open_moodle()
        authenticated = await self._has_session_key()
        current_url = self.page.url
        if authenticated:
            message = "Authenticated with Monash Moodle."
        elif is_moodle_page(current_url, self.settings.moodle_base_url):
            message = "Moodle is open, but the saved session is not authenticated."
        else:
            message = "The saved session is missing or expired."
        return SessionStatus(authenticated, current_url, message)

    async def ensure_authenticated(self) -> None:
        """Raise a clear error when a non-interactive command needs login."""
        status = await self.status()
        if not status.authenticated:
            raise LoginRequiredError(
                "Monash login is required. Run `mmd login` and complete SSO/MFA."
            )

    async def wait_for_login(self, *, timeout_seconds: int) -> SessionStatus:
        """Wait for the user to finish SSO/MFA in the visible Chrome window."""
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if self.page.is_closed():
                raise LoginRequiredError(
                    "The login window was closed before Moodle login completed."
                )
            if await self._has_session_key():
                return await self.status(navigate=False)
            await asyncio.sleep(0.5)
        raise LoginRequiredError(
            "Login was not completed before the timeout. Run `mmd login` to try again."
        )

    async def _has_session_key(self) -> bool:
        if not is_moodle_page(self.page.url, self.settings.moodle_base_url):
            return False
        try:
            value = await self.page.evaluate(
                "() => Boolean(window.M && window.M.cfg && window.M.cfg.sesskey)"
            )
        except PlaywrightError:
            return False
        return value is True

    async def _save_storage_state(self) -> None:
        if self._context is None:
            return
        state = await self._context.storage_state()
        temporary = self.settings.storage_state.with_suffix(".json.tmp")
        temporary.write_text(f"{json.dumps(state)}\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.settings.storage_state)
        os.chmod(self.settings.storage_state, 0o600)


def load_saved_cookies(path: Path) -> list[dict[str, object]]:
    """Read saved Playwright cookies, treating missing or damaged state as logged out."""
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, dict) or not isinstance(decoded.get("cookies"), list):
        return []
    cookies = [cookie for cookie in decoded["cookies"] if isinstance(cookie, dict)]
    return cast(list[dict[str, object]], cookies)
