"""Credential-scrubbing tests (audit 2026-07-11).

Provider APIs can carry credentials in request URLs, and HTTP-client
exceptions stringify with the full URL — so raw error text reaching logs
would leak the key, and the scheduler pastes log tails into cleartext
alert emails. These tests pin the three layers of defense: the
redact_credentials helper, the downloader error paths, and the scheduler's
emailed log tail.
"""

import asyncio
import urllib.parse

import aiohttp
import pytest

from streetscape_metadata_tracker import download_mapillary as dm
from streetscape_metadata_tracker.download_common import DownloadError, redact_credentials


def test_redact_credentials_scrubs_query_params():
    assert (
        redact_credentials("GET https://maps.googleapis.com/x?location=1,2&key=AIzaSECRET failed")
        == "GET https://maps.googleapis.com/x?location=1,2&key=REDACTED failed"
    )
    assert (
        redact_credentials("https://tiles.mapillary.com/t?access_token=MLY%7Csecret%7C123")
        == "https://tiles.mapillary.com/t?access_token=REDACTED"
    )


def test_redact_credentials_scrubs_a_url_nested_in_another_urls_query():
    """A redirect's Location echoes the request URL percent-encoded inside its
    own query string (issue #199), so `access_token=` arrives as
    `access_token%3D`. Matching only the literal form let the token straight
    through into logs and the alert email."""
    tile = "https://tiles.mapillary.com/t/2/14/1/2?access_token=MLY%7CSECRET"
    for encoder in (urllib.parse.quote, urllib.parse.quote_plus):
        location = "https://www.mapillary.com/login/?next=" + encoder(tile, safe="")
        out = redact_credentials(location)
        assert "SECRET" not in out, out
        assert "REDACTED" in out


def test_redact_credentials_handles_multiple_and_mixed_case():
    text = "a?KEY=S1&other=ok&access_token=S2 b?key=S3"
    out = redact_credentials(text)
    assert "S1" not in out and "S2" not in out and "S3" not in out
    assert "other=ok" in out


def test_redact_credentials_leaves_clean_text_alone():
    msg = "HTTP 503 fetching tile (14, 2625, 5722); retrying"
    assert redact_credentials(msg) == msg


def test_redact_credentials_accepts_exceptions():
    # Callers pass exception objects directly.
    e = aiohttp.ClientError("boom url?key=SECRET")
    assert "SECRET" not in redact_credentials(e)


def test_mapillary_tile_url_has_no_token():
    # The token must never be part of the request URL (it travels as an
    # Authorization header); a regression here re-opens the log leak.
    assert "token" not in dm.TILE_URL_TEMPLATE
    assert "{token}" not in dm.TILE_URL_TEMPLATE


def test_mapillary_download_error_is_scrubbed(monkeypatch, tmp_path):
    # A failing tile fetch whose exception text contains a credential must
    # surface as a DownloadError WITHOUT the credential.
    # *args absorbs the rate limiter and request counter #198 passes through.
    async def exploding_fetch(session, url, timeout, *args):
        raise aiohttp.ClientError("HTTP 429 for https://tiles/x?access_token=SECRET123")

    monkeypatch.setattr(dm, "_fetch_tile", exploding_fetch)
    with pytest.raises(DownloadError) as excinfo:
        asyncio.run(
            dm.download_mapillary_metadata_async(
                "Test City",
                47.6,
                -122.3,
                100,
                100,
                20,
                "MLY|test|token",
                str(tmp_path / "t_2026-07-05.csv.gz"),
            )
        )
    msg = str(excinfo.value)
    assert "SECRET123" not in msg
    assert "REDACTED" in msg


def test_scheduler_log_tail_is_scrubbed(tmp_path):
    from streetscape_metadata_tracker import scheduler as sched

    cfg = sched.SchedulerConfig(log_dir=str(tmp_path))
    log_path = tmp_path / "streetscape_scheduler.log"
    log_path.write_text(
        "2026-07-11 INFO ok line\n"
        "2026-07-11 ERROR fetch failed: https://maps.googleapis.com/x?key=AIzaLEAKED\n"
    )
    tail = sched._recent_log_tail(cfg)
    assert "AIzaLEAKED" not in tail
    assert "ok line" in tail
