"""Tests for block-page detection.

Block fixtures are trimmed copies of what real captures received (Oct 2026,
issue #1); the clean fixtures are look-alikes that must not trip detection.
"""

from __future__ import annotations

import pytest

from source_archive.blocking import detect_block, is_challenge_title

CLOUDFRONT_GEO = b"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE></HEAD><BODY>
<H1>403 ERROR</H1><H2>The request could not be satisfied.</H2><HR noshade size="1px">
The Amazon CloudFront distribution is configured to block access from your country.
</BODY></HTML>"""

CLOUDFRONT_OTHER = b"""<HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE></HEAD>
<BODY><H1>403 ERROR</H1>Request blocked.</BODY></HTML>"""

AKAMAI = b"""<HTML><HEAD>
<TITLE>Access Denied</TITLE>
</HEAD><BODY>
<H1>Access Denied</H1>

You don't have permission to access "http&#58;&#47;&#47;www&#46;sanders&#46;senate&#46;gov&#47;press&#45;releases&#47;" on this server.<P>
Reference&#32;&#35;18&#46;5d2f3e17&#46;1792571308&#46;2b2f0a4
<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;5d2f3e17
</BODY>
</HTML>"""

CLOUDFLARE_CHALLENGE = b"""<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>
<meta http-equiv="refresh" content="360"></head><body><div class="main-wrapper">
<script>(function(){window._cf_chl_opt = {cvId: '3', cZone: "www.axios.com"};}());</script>
</div></body></html>"""

CLOUDFLARE_BLOCK = b"""<!DOCTYPE html><html class="no-js" lang="en-US">
<head><title>Attention Required! | Cloudflare</title></head>
<body><h1>Sorry, you have been blocked</h1></body></html>"""

AWS_WAF = b"""<!DOCTYPE html><html lang="en"><head><title>IEEE Xplore - Unable to Load Page</title>
<script>window.awsWafCookieDomainList = [];window.gokuProps = {"key":"AQID"};</script>
</head><body></body></html>"""

# A normal article on a Cloudflare-fronted site: the bot-detection script and
# a reCAPTCHA-protected newsletter form are everywhere and mean nothing.
NORMAL_CLOUDFLARE_PAGE = b"""<html><head><title>The Cloud Is Not Carbon Neutral</title>
<script src="/cdn-cgi/challenge-platform/h/b/scripts/jsd/a1b2/main.js"></script>
<script src="https://www.google.com/recaptcha/api.js"></script></head>
<body><article><p>Data centres are the fastest-growing load on the grid.</p>
<form class="captcha-protected">Subscribe</form></article></body></html>"""


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (403, CLOUDFRONT_GEO, "geo: CloudFront"),
        (403, CLOUDFRONT_OTHER, "bot: CloudFront"),
        (403, AKAMAI, "bot: Akamai"),
        (403, CLOUDFLARE_CHALLENGE, "bot: Cloudflare challenge"),
        (403, CLOUDFLARE_BLOCK, "bot: Cloudflare block"),
        (202, AWS_WAF, "bot: AWS WAF challenge"),
    ],
)
def test_detects_real_block_pages(status: int, body: bytes, reason: str) -> None:
    assert detect_block(status, None, body) == reason


def test_body_may_be_str() -> None:
    assert detect_block(403, None, CLOUDFRONT_GEO.decode()) == "geo: CloudFront"


def test_block_host_is_detected_regardless_of_status() -> None:
    reason = detect_block(
        200,
        "Federal Register :: Request Access",
        b"<html><body>Request Access</body></html>",
        url="https://unblock.federalregister.gov/",
    )
    assert reason == "bot: Federal Register"


def test_challenge_title_alone_is_a_block() -> None:
    assert detect_block(200, "Just a moment...", b"<html></html>") == "bot: challenge page"


def test_forbidden_status_without_signature_reports_server() -> None:
    assert detect_block(403, "Forbidden", b"nope", headers={"server": "nginx"}) == "http 403: nginx"
    assert detect_block(429, None, None) == "http 429"


@pytest.mark.parametrize("status", [200, 404, 500])
def test_normal_pages_are_not_blocks(status: int) -> None:
    assert detect_block(status, "The Cloud Is Not Carbon Neutral", NORMAL_CLOUDFLARE_PAGE) is None


def test_binary_bodies_are_ignored() -> None:
    assert detect_block(200, None, None, url="https://example.com/report.pdf") is None


def test_is_challenge_title() -> None:
    assert is_challenge_title("Just a moment...")
    assert is_challenge_title("  just a moment... ")
    # A hard block never resolves itself, so it isn't worth waiting for.
    assert not is_challenge_title("Attention Required! | Cloudflare")
    assert not is_challenge_title("Just a moment of reflection on data centres")
    assert not is_challenge_title(None)
