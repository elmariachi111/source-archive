"""Browser engines used to render captures.

Stock headless Chromium announces itself (``HeadlessChrome`` in the UA,
``navigator.webdriver``) and is refused outright by Akamai, AWS WAF,
Cloudflare and the Federal Register. ``open_page`` hands out a page from an
engine that looks like a regular browser:

- ``chromium``: Playwright's Chromium in new-headless mode, with the
  automation flag off and ``HeadlessChrome`` removed from the UA.
- ``camoufox``: a fingerprint-hardened Firefox (optional ``stealth`` extra).
  It passes Cloudflare challenges stock Chromium can't, but is slower, so
  callers use it only as a fallback.

Both expose the Playwright page API, so response capture works the same.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

CHROMIUM = "chromium"
CAMOUFOX = "camoufox"

LOCALE = "en-US"
VIEWPORT = {"width": 1366, "height": 900}

_CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]


def available_engines() -> list[str]:
    """Engines to try, in order: Chromium first, Camoufox if installed."""
    engines = [CHROMIUM]
    if importlib.util.find_spec("camoufox") is not None:
        engines.append(CAMOUFOX)
    return engines


def _launch_chromium(p) -> Any:
    """Launch new-headless Chromium, or stock headless if it isn't installed."""
    try:
        return p.chromium.launch(channel="chromium", args=_CHROMIUM_ARGS)
    except Exception as exc:  # noqa: BLE001 — older installs lack the full build.
        log.warning("New-headless Chromium unavailable (%s); using headless shell.", exc)
        return p.chromium.launch(args=_CHROMIUM_ARGS)


def _browser_user_agent(browser) -> str:
    """The browser's own UA with the ``Headless`` marker removed.

    Keeps platform and version consistent with what the engine really is.
    """
    probe = browser.new_page()
    try:
        return probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
    finally:
        probe.close()


@contextmanager
def open_page(engine: str = CHROMIUM) -> Iterator[Any]:
    """Yield a fresh page from ``engine``; the browser closes on exit."""
    if engine == CAMOUFOX:
        from camoufox.sync_api import Camoufox

        # geoip: timezone and geolocation follow the exit IP. A browser that
        # claims Berlin time behind a US exit is an easy bot tell.
        with Camoufox(headless=True, humanize=True, locale=LOCALE, geoip=True) as browser:
            yield browser.new_page()
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        try:
            context = browser.new_context(
                user_agent=_browser_user_agent(browser),
                locale=LOCALE,
                viewport=VIEWPORT,
            )
            yield context.new_page()
        finally:
            browser.close()
