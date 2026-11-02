"""Tests for source_archive.browser engine setup, without launching browsers."""

from __future__ import annotations

import sys
import types
from unittest import mock

from source_archive import browser


def _fake_camoufox_module(calls: list) -> dict[str, types.ModuleType]:
    class FakeCamoufox:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            fake = mock.Mock()
            fake.new_page.return_value = "page"
            return fake

        def __exit__(self, *exc):
            return False

    package = types.ModuleType("camoufox")
    sync_api = types.ModuleType("camoufox.sync_api")
    sync_api.Camoufox = FakeCamoufox
    return {"camoufox": package, "camoufox.sync_api": sync_api}


def test_camoufox_matches_timezone_and_location_to_exit_ip() -> None:
    calls: list = []
    with mock.patch.dict(sys.modules, _fake_camoufox_module(calls)):
        with browser.open_page(browser.CAMOUFOX) as page:
            assert page == "page"

    assert calls == [{"headless": True, "humanize": True, "locale": "en-US", "geoip": True}]


def test_camoufox_is_offered_only_when_installed() -> None:
    with mock.patch.object(browser.importlib.util, "find_spec", return_value=None):
        assert browser.available_engines() == [browser.CHROMIUM]
    with mock.patch.object(browser.importlib.util, "find_spec", return_value=object()):
        assert browser.available_engines() == [browser.CHROMIUM, browser.CAMOUFOX]
