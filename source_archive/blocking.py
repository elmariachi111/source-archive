"""Recognise block pages: geo restrictions, bot challenges and WAF denials.

A blocked source still returns *a* page, often with status 200, so it would
otherwise be bundled as if it were the cited content. ``detect_block`` looks
at the main document's status, title, headers and body and returns a short
reason (``"geo: CloudFront"``) or ``None``. The signatures were collected
from real failed captures (Oct 2026, issue #1).
"""

from __future__ import annotations

import re

# Titles of interstitials that resolve themselves in a real browser. The
# capture waits for these to go away before treating the page as loaded.
CHALLENGE_TITLES = (
    "just a moment...",  # Cloudflare managed challenge
    "please wait...",
    "ddos-guard",
)

# (reason, regex over the start of the body). First match wins, so the
# specific geo text comes before the generic CloudFront error.
_BODY_SIGNATURES = (
    ("geo: CloudFront", r"configured to block access from your country"),
    ("geo", r"not available in your (country|region)|unavailable in your (country|region)"),
    # Not "challenge-platform": Cloudflare injects that script into normal pages.
    ("bot: Cloudflare challenge", r"_cf_chl_opt|<title>\s*Just a moment\.\.\.\s*</title>"),
    ("bot: Cloudflare block", r"<title>\s*Attention Required! \| Cloudflare\s*</title>"),
    ("bot: AWS WAF challenge", r"awsWafCookieDomainList|window\.gokuProps"),
    ("bot: Akamai", r"You don't have permission to access [\s\S]{0,600}? on this server|errors\.edgesuite\.net"),
    ("bot: DataDome", r"captcha-delivery\.com|geo\.captcha-delivery"),
    ("bot: PerimeterX", r"px-captcha|_pxCaptcha"),
    ("bot: CloudFront", r"<title>\s*ERROR: The request could not be satisfied\s*</title>"),
)

# Hosts that only ever serve a "you look like a bot" page.
_BLOCK_HOSTS = {
    "unblock.federalregister.gov": "bot: Federal Register",
}

# Statuses that mean "not for you" rather than "not here".
_BLOCK_STATUSES = {401, 403, 407, 429, 451}

_SCAN_BYTES = 64_000

# Block pages are tiny (0.5-10 KB in practice); articles are 50 KB and up.
# A 2xx page larger than this is only checked by host and title, so an
# article that quotes block-page text (outage coverage) isn't flagged.
_MAX_2XX_BLOCK_PAGE_BYTES = 32_000


def detect_block(
    status: int | None,
    title: str | None = None,
    body: bytes | str | None = None,
    *,
    url: str | None = None,
    headers: dict[str, str] | None = None,
) -> str | None:
    """Return why the response is a block page, or ``None`` if it looks real."""
    if url:
        host = re.sub(r"^https?://([^/:]+).*$", r"\1", url).lower()
        if host in _BLOCK_HOSTS:
            return _BLOCK_HOSTS[host]

    successful = status is not None and 200 <= status < 300
    if not (successful and len(body or "") > _MAX_2XX_BLOCK_PAGE_BYTES):
        if isinstance(body, bytes):
            text = body[:_SCAN_BYTES].decode("utf-8", errors="replace")
        else:
            text = (body or "")[:_SCAN_BYTES]
        for reason, pattern in _BODY_SIGNATURES:
            if re.search(pattern, text, re.IGNORECASE):
                return reason

    if title and title.strip().lower() in CHALLENGE_TITLES:
        return "bot: challenge page"

    if status in _BLOCK_STATUSES:
        server = (headers or {}).get("server", "").lower()
        return f"http {status}" + (f": {server}" if server else "")

    return None


def is_challenge_title(title: str | None) -> bool:
    """True while a self-resolving bot challenge is showing."""
    return bool(title) and title.strip().lower() in CHALLENGE_TITLES
