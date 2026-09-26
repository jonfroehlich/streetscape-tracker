"""The one clock a snapshot is dated by (#347).

Every dated artifact in this project -- a grid run, a road walk, a census
cache marker, a checkpoint's ``created_at``, the ``api_usage`` ledger row --
is stamped in UTC, and ``checkpointing.load_census_cache_marker`` compares
the marker's UTC ``completed_at`` date against the consumer's ``run_date``.
A default ``run_date`` read from the LOCAL calendar (``date.today()``)
disagrees with that stamp for the last 7-8 hours of every Pacific day, so an
evening road walk was refused the census its grid run had just promoted and
re-paid for it (#347).

Nothing on the collection path reads a local calendar; this module is where
the UTC one lives, and ``_utc_clock`` is the seam a test freezes::

    monkeypatch.setattr(clock, "_utc_clock", lambda: datetime(2026, 9, 1, 0, 30, tzinfo=UTC))
    clock.snapshot_date_today()  # -> date(2026, 9, 1), whatever the host's zone
"""

from datetime import UTC, date, datetime


def _utc_clock() -> datetime:
    """Return the current aware UTC instant.

    The test seam: monkeypatch ``clock._utc_clock`` to freeze every stamp and
    date read through this module.  Never import it by name -- the public
    helpers re-read it at call time, which is what makes the patch reach them.
    """
    return datetime.now(UTC)


def utc_now() -> datetime:
    """Aware UTC now, read at call time (never bound at import)."""
    return _utc_clock()


def utc_now_iso() -> str:
    """``utc_now()`` as an ISO-8601 string with a ``+00:00`` offset."""
    return utc_now().isoformat()


def snapshot_date_today() -> date:
    """The default ``run_date`` for a grid run or a road walk: the UTC calendar date.

    The same date the scheduler passes both collection children as
    ``--run-date`` and keys ``api_usage`` by, and the calendar the census cache
    marker's ``completed_at`` is compared in.
    """
    return utc_now().date()
