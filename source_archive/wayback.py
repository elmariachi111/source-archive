"""Internet Archive Wayback Machine SPN2 integration.

Submits URLs to the Save Page Now 2 API and polls for job completion.
If no S3 credentials are present in the environment, calls are skipped
gracefully so the capture pipeline still succeeds.
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

SPN2_SAVE_URL = "https://web.archive.org/save"
SPN2_STATUS_URL = "https://web.archive.org/save/status"
# Conservative polling: SPN2 jobs can take 30–90 seconds.
POLL_INTERVAL = 10  # seconds
POLL_TIMEOUT = 300  # seconds (5 minutes)
REQUEST_TIMEOUT = 30  # seconds


def get_credentials() -> tuple[str, str] | None:
    """Return ``(access_key, secret_key)`` from env, or None if unset."""
    access = os.environ.get("IA_S3_ACCESS_KEY", "").strip()
    secret = os.environ.get("IA_S3_SECRET_KEY", "").strip()
    if not access or not secret:
        return None
    return (access, secret)


def auth_header(creds: tuple[str, str]) -> str:
    """Build the ``Authorization: LOW <access>:<secret>`` header value."""
    return f"LOW {creds[0]}:{creds[1]}"


def submit_url(url: str, creds: tuple[str, str]) -> str | None:
    """Submit ``url`` to SPN2. Returns the job id, or None on failure."""
    headers = {"Authorization": auth_header(creds), "Accept": "application/json"}
    data = {"url": url, "skip_first_archive": "1"}
    try:
        resp = requests.post(
            SPN2_SAVE_URL, headers=headers, data=data, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code in (200, 201, 202):
            body = resp.json()
            job_id = body.get("job_id")
            if job_id:
                return str(job_id)
            # Some responses return the archive URL directly.
            wayback = body.get("wayback_url") or body.get("archive_url")
            if wayback:
                return f"direct:{wayback}"
        log.warning("SPN2 submit returned %s: %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        log.warning("SPN2 submit request failed: %s", exc)
    return None


def poll_job(job_id: str, creds: tuple[str, str]) -> str | None:
    """Poll the SPN2 status endpoint until the job completes.

    Returns the Wayback URL on success, or None on failure/timeout.
    A ``direct:<url>`` job id is returned as-is (the submit step already
    had the final URL).
    """
    if job_id.startswith("direct:"):
        return job_id[len("direct:") :]

    headers = {"Authorization": auth_header(creds), "Accept": "application/json"}
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            resp = requests.get(
                SPN2_STATUS_URL,
                params={"job_id": job_id},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("SPN2 poll request failed: %s", exc)
            time.sleep(POLL_INTERVAL)
            continue
        if resp.status_code != 200:
            log.warning("SPN2 status returned %s", resp.status_code)
            time.sleep(POLL_INTERVAL)
            continue
        body = resp.json()
        status = body.get("status")
        if status == "success":
            return body.get("wayback_url") or body.get("archive_url")
        if status == "error":
            log.warning("SPN2 job %s failed: %s", job_id, body.get("message", ""))
            return None
        # pending / in_progress
        time.sleep(POLL_INTERVAL)
    log.warning("SPN2 job %s timed out after %ds", job_id, POLL_TIMEOUT)
    return None


def save_to_wayback(url: str) -> str | None:
    """Submit ``url`` to the Wayback Machine and poll for the result.

    Returns the Wayback URL on success, or None if credentials are missing
    or the request failed. Never raises — callers can treat Wayback as
    best-effort.
    """
    creds = get_credentials()
    if creds is None:
        log.warning("IA_S3_ACCESS_KEY / IA_S3_SECRET_KEY not set — skipping Wayback.")
        return None
    job_id = submit_url(url, creds)
    if not job_id:
        return None
    return poll_job(job_id, creds)