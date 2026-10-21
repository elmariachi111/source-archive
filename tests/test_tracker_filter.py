"""Test the tracker/ad/consent host blocklist in capture.py.

Every known-junk host must be caught, every known-content host must be kept.
Junk list mined from three real production bundles + the Oct 2026 hang log.
The marker tuple is extracted from the SHIPPED source (single source of
truth — never duplicate the list here, it went stale once already)."""
import re
from urllib.parse import urlparse

import pytest

import source_archive.capture as cap

_src = cap.__doc__ and ""  # placeholder no-op; real source below
import inspect

_src = inspect.getsource(cap)
_m = re.search(
    r"_TRACKER_HOST_MARKERS = (.*?)\)\s*\n\s*\n\s*def _handle_response",
    _src,
    re.S,
)
assert _m, "marker tuple not found in capture.py"
MARKERS = tuple(re.findall(r'"([^"]+)"', _m.group(1)))
assert MARKERS, "no markers parsed"
assert len(MARKERS) >= 55, f"unexpectedly small marker list: {len(MARKERS)}"


def _caught(netloc: str) -> bool:
    return any(mk in netloc for mk in MARKERS)


KNOWN_JUNK = [
    # ad exchanges / RTB / header bidding
    "pagead2.googlesyndication.com", "tpc.googlesyndication.com",
    "securepubads.g.doubleclick.net", "stats.g.doubleclick.net",
    "googleads.g.doubleclick.net", "cm.g.doubleclick.net",
    "static.doubleclick.net", "ad.doubleclick.net",
    "29f2a5bdd9e8547281d428941aaf1565.safeframe.googlesyndication.com",
    "rtb-csync.smartadserver.com", "apps.smartadserver.com",
    "ssbsync-global.smartadserver.com", "ssb-global.smartadserver.com",
    "csync-global.smartadserver.com", "csync-eu.smartadserver.com",
    "ads.pubmatic.com", "ads.adthrive.com", "a.teads.tv", "s.ad.smaato.net",
    "ad.360yield.com", "ad.turn.com", "match.adsrvr.org",
    "c.amazon-adsystem.com", "config.aps.amazon-adsystem.com",
    "client.aps.amazon-adsystem.com", "aax.amazon-adsystem.com",
    "ads.projectagoraservices.com",
    # taboola / outbrain widgets
    "trc.taboola.com", "images.taboola.com", "am-vid-events.taboola.com",
    "am-wf.taboola.com", "vidstat.taboola.com", "am-trc-events.taboola.com",
    "cdn.taboola.com", "wf.taboola.com", "beacon.taboola.com",
    "am-match.taboola.com", "imprammp.taboola.com", "pm-widget.taboola.com",
    "display.bidder.taboola.com",
    # tag containers / GA / publisher analytics
    "www.googletagmanager.com", "www.google-analytics.com",
    "region1.google-analytics.com", "region1.analytics.google.com",
    "static.chartbeat.com", "ping.chartbeat.net", "mab.chartbeat.com",
    "sb.scorecardresearch.com", "secure.quantserve.com", "pixel.quantserve.com",
    "cms.quantserve.com", "www.clarity.ms", "scripts.clarity.ms",
    "u.clarity.ms", "z.clarity.ms", "y.clarity.ms", "e.clarity.ms",
    "static.hotjar.com", "api-js.mixpanel.com", "cdn.amplitude.com",
    "api.lab.amplitude.com", "api2.amplitude.com",
    "techniquesingenieur.matomo.cloud", "cdn.matomo.cloud", "iea.matomo.cloud",
    "matomojs.trackify.info", "js.hs-analytics.net", "js-agent.newrelic.com",
    "bam.nr-data.net", "siteimproveanalytics.com",
    "6294131.global.r2.siteimproveanalytics.io", "ophan.theguardian.com",
    "analytics.twitter.com", "analytics.ahrefs.com",
    "marketingplatform.google.com", "telemetry.us.transcend.io",
    "mybbc-analytics.files.bbci.co.uk", "metrics.brightcove.com",
    "gametrics.manufacturingdive.com",
    # pixels / social trackers
    "px.ads.linkedin.com", "bat.bing.com", "bat.bing.net",
    "pixel-config.reddit.com", "alb.reddit.com", "redditmedia.com",
    "ads.aeroxplorer.com", "cloudflareinsights", "static.cloudflareinsights.com",
    # consent / cookie management
    "cdn.cookielaw.org", "cdn.privacy-mgmt.com", "eu-us.consentmanager.net",
    "eu-us-cdn.consentmanager.net", "cdn.consentmanager.net",
    "a.delivery.consentmanager.net", "b.delivery.consentmanager.net",
    "cdn-au.onetrust.com", "geolocation.onetrust.com",
    "consent.cookiebot.com", "consentcdn.cookiebot.com", "cdn-cookieyes.com",
    "choices.consentframework.com", "api.consentframework.com",
    "cache.consentframework.com", "api.consentpro.com",
    # misc adtech
    "p.flipp.com", "flux.broadstreet.ai", "pvhxscx.pa-cd.com",
    "media.ffycdn.net", "btloader.com", "api.btloader.com",
    "cdn.btloader.com", "cdn.api.btloader.com", "plausible.io",
]

# MUST be kept — real content, several deliberately scary-looking
KNOWN_CONTENT = [
    "insights.carbon-direct.com",  # hosts the cited Carbon Direct Navigator PDF
    "uploads.thirdway.org",        # '...ads.' substring trap — content CDN
    "fonts.gstatic.com", "www.gstatic.com", "t1.gstatic.com",
    "static.files.bbci.co.uk", "ichef.bbci.co.uk",
    "www.pv-magazine.com", "substackcdn.com", "challenges.cloudflare.com",
    "www.reuters.com", "www.theguardian.com", "cdn.visaverge.com",
    "www.energyintel.com", "sdfestaticassets-eu-west-1.sciencedirectassets.com",
    "uploads.disquscdn.com",     # disqus embed assets — page content, kept
    "static.addtoany.com",       # share buttons — part of rendered page
    "www.straitstimes.com", "static.politico.com", "www.kompas.id",
]


@pytest.mark.parametrize("host", KNOWN_JUNK)
def test_junk_caught(host):
    assert _caught(host), f"tracker NOT caught: {host}"


@pytest.mark.parametrize("host", KNOWN_CONTENT)
def test_content_kept(host):
    assert not _caught(host), f"content host WRONGLY dropped: {host}"


def test_marker_list_extracted_not_duplicated():
    assert len(MARKERS) >= 55