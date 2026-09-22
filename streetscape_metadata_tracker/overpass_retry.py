"""
How long a road walk's Overpass fetch keeps asking before it gives up (issue #357).

osmnx-free on purpose, like ``download_common``'s Overpass identity: the
scheduler loads ``[overpass]`` from TOML and hands the values to every walk
child on its argv, and the scheduler must never import the osmnx stack. The
fetch itself (``streetscape_street_analyzer.download_street_network``) is the
only caller of :func:`call_with_retry`.

**What this changes, and what it deliberately does not.** Before #357 the
fetch retried a transport fault with tenacity's ``stop_after_attempt(3)`` and
``wait_exponential(min=4, max=10)``: ~12 s of backoff in all. How long the
whole fetch then took depended on whether ``/status`` still answered. When it
did not, each attempt also sat through osmnx's 60 s ``default_pause``
(``_get_overpass_pause`` falls back to it on a ConnectionError), for ~3 min in
all -- which matches the ~3.2 min a refused walk took to die on 2026-09-15.
When it did (overpass-api.de is multi-backend, and on 2026-09-21 a clean
``/status`` was followed by a refused ``/interpreter`` within minutes), the
three POSTs were ~12 s apart and the window was that 12 s. Refusals that night
flapped on a minutes scale (refused 06:07, serving 06:53, refused again
06:57), so either window escalated each one into a latched night-level
breaker. The default window here is ~7.5 minutes in both shapes; see
``OverpassRetryPolicy``.

What stays exactly as it was:

* **The retry predicate.** Only transport faults (``ConnectionError`` /
  ``Timeout``) are retried; a settled answer (a ban page, a 406, a bbox with no
  drivable ways) still costs one round trip. That predicate lives with the
  fetch, not here.
* **No classification by errno.** A bare ECONNREFUSED is the signature of both
  a transient flap and the 2026-08-14 abuse ban (issue #341), so every
  retryable fault waits on the SAME schedule; duration is the discriminator,
  and the window is how long we are willing to measure it.
* **HTTP 429/504 are not ours to retry.** osmnx answers both by sleeping 55 s
  and recursing (``_overpass.py``), which already clears the usage policy's
  30 s pause; the fetch's SIGALRM deadline bounds that recursion.

Sources read before choosing the numbers (2026-09-22):

* Overpass API wiki, usage policy: "If you receive an HTTP error code such as
  429 or 406, pause for 30 seconds before making a new request", and "No
  parallel running of multiple scripts".
* overpass-doc, "Commons" chapter: a client refused by the rate limit "shall
  resubmit the requests after the 15 seconds".
* OSM community forum, "Blocked IP on overpass api?" (thread 146883): an
  Overpass operator, 2026-08-27, "the IP had been blocked for ignoring the 429
  return ... If you get a 429, wait at least 30s before sending the next
  query", and 2026-09-01, "The IP bans are completely automatic and will
  disappear after a while"; the reporter's symptom was ``Connection refused``
  and the fix they shipped was a 35 s retry delay.

Hence the 30 s floor on every wait (``OVERPASS_MIN_RETRY_WAIT_S``), which is
enforced rather than defaulted: a config that asks for less is refused.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The usage policy's pause after a 429/406, and the operator's "wait at least
# 30s" (see the module docstring). A FLOOR, not a default: no configuration can
# put two of our attempts closer together than this.
OVERPASS_MIN_RETRY_WAIT_S = 30.0

# The largest retry window the fetch's SIGALRM deadline leaves room for.
#
# ``download_street_network.OVERPASS_DEADLINE_S`` is derived from this: the
# window, plus one full final attempt (the 180 s request timeout and 120 s of
# osmnx slot-pause slack). A retry is only started if its wait ENDS inside the
# window, so the last attempt begins no later than this many seconds in and the
# deadline still covers it. Raising the window past this would have the deadline
# cut the final attempt and report it as a 429/504 hang, so it is refused here
# rather than silently truncated there.
OVERPASS_RETRY_WINDOW_CEILING_S = 600.0

# What one final attempt costs after the window closes, and what a walk child
# needs before its fetch starts. Both are osmnx-free copies of numbers the fetch
# owns, because the SCHEDULER has to do this arithmetic (see
# :func:`policy_for_child_timeout`) and must never import the osmnx stack --
# the same reason ``download_common`` carries its own copy of the Overpass
# endpoint and identity. ``test_the_request_timeout_here_is_the_one_osmnx_uses``
# pins the mirror, so a change to ``OVERPASS_TIMEOUT_S`` cannot drift from it.
OVERPASS_REQUEST_TIMEOUT_S = 180.0
OVERPASS_ATTEMPT_SLACK_S = 120.0
OVERPASS_FINAL_ATTEMPT_RESERVE_S = OVERPASS_REQUEST_TIMEOUT_S + OVERPASS_ATTEMPT_SLACK_S  # 300 s

# Everything a walk child does before the retry window can start: interpreter
# startup and the osmnx/geopandas import chain, the catalog open, the host lock,
# and the /status pre-flight's own 15 s timeout. Deliberately generous, because
# what it buys is the difference between exiting 76 (the breaker learns) and
# being SIGKILLed with no exit code at all (it does not).
OVERPASS_CHILD_STARTUP_RESERVE_S = 60.0


@dataclass(frozen=True)
class OverpassRetryPolicy:
    """
    The retry schedule for one Overpass graph fetch.

    Failure ``k`` (1-based) is followed by a wait of::

        nominal(k) = min(max_wait_s, initial_wait_s * 2 ** (k - 1))
        wait(k)    = nominal(k) * (1 + jitter * u),   u ~ Uniform[0, 1)

    Jitter is UPWARD only, so the policy floor holds at every step and the
    ceiling of any single wait is ``max_wait_s * (1 + jitter)``. The fetch gives
    up at whichever comes first: ``max_attempts`` attempts, or a wait that would
    end more than ``window_s`` after the first attempt started. The window is
    measured on the wall clock and so includes the attempts themselves --
    osmnx's 60 s pre-request pause on a refusing host included.

    Defaults, and what they buy on the two refusal shapes we have seen:

    * ``/status`` refusing too (the pause is osmnx's 60 s fallback): attempts
      start at ~0, 90, 210 and 390 s; the 240 s wait after the fourth would
      end past 600 s, so the fetch gives up at ~450 s -- 4 attempts,
      ~7.5-9 min with jitter.
    * ``/status`` serving while ``/interpreter`` refuses (the multi-backend
      shape of 2026-09-21, so no pause): attempts at ~0, 30, 90, 210 and
      450 s -- 5 attempts, ~7.5-9.5 min with jitter.

    Against the ~12 s / ~3 min windows it replaces, that is a flap of up to
    ~7 min absorbed rather than escalated. What it costs: the Overpass host lock is
    held for the window, and the breaker can trip at most
    ``1 + scheduler.HOST_RECHECKS_PER_NIGHT`` times a night, so a night that
    refuses throughout pays at most five windows (~40 min, bounded by five
    900 s deadlines) of wall clock in the lane running the walk. With
    ``[schedule].max_concurrent_channels`` above 1, channels on other hosts
    keep running in another lane meanwhile; at 1 the whole night waits.

    Raises ``ValueError`` on a value outside the documented range, so an
    invalid policy can never be constructed; the scheduler's config loader
    catches that and falls back to the defaults with a warning.
    """

    max_attempts: int = 5
    initial_wait_s: float = 30.0
    max_wait_s: float = 240.0
    jitter: float = 0.25
    window_s: float = 600.0

    def __post_init__(self) -> None:
        def number(name: str) -> float:
            value = getattr(self, name)
            # TOML booleans are Python ints; `retry_jitter = true` must not
            # load as 1 and read as if it had been honoured.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name}={value!r} is not a number")
            if not math.isfinite(value):
                raise ValueError(f"{name}={value!r} is not finite")
            return float(value)

        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError(f"max_attempts={self.max_attempts!r} is not an integer")
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts={self.max_attempts} must be at least 1")
        initial = number("initial_wait_s")
        if initial < OVERPASS_MIN_RETRY_WAIT_S:
            raise ValueError(
                f"initial_wait_s={initial:g} is below the {OVERPASS_MIN_RETRY_WAIT_S:g} s "
                f"the Overpass usage policy asks for between a refusal and the next request"
            )
        if number("max_wait_s") < initial:
            raise ValueError(f"max_wait_s={self.max_wait_s:g} is below initial_wait_s={initial:g}")
        if not 0.0 <= number("jitter") <= 1.0:
            raise ValueError(f"jitter={self.jitter!r} is not a fraction in [0, 1]")
        window = number("window_s")
        if not 0.0 < window <= OVERPASS_RETRY_WINDOW_CEILING_S:
            raise ValueError(
                f"window_s={window:g} is outside (0, {OVERPASS_RETRY_WINDOW_CEILING_S:g}]; "
                f"the fetch's {OVERPASS_RETRY_WINDOW_CEILING_S:g} s window plus one full "
                f"attempt is what its SIGALRM deadline covers"
            )

    def nominal_wait_s(self, failures: int) -> float:
        """The un-jittered wait after the ``failures``-th failed attempt (1-based)."""
        if failures < 1:
            raise ValueError(f"failures={failures} must be at least 1")
        # Capped before exponentiating so a large attempt count cannot overflow.
        exponent = min(failures - 1, 64)
        return min(self.max_wait_s, self.initial_wait_s * 2.0**exponent)

    def wait_s(self, failures: int, u: float) -> float:
        """The jittered wait after failure ``failures``, for a uniform draw ``u`` in [0, 1)."""
        return self.nominal_wait_s(failures) * (1.0 + self.jitter * u)


def policy_for_child_timeout(policy: OverpassRetryPolicy, timeout_s: float) -> OverpassRetryPolicy:
    """
    ``policy``, shortened if a child with ``timeout_s`` could not survive it.

    The scheduler SIGKILLs a collection child at its per-city timeout, and that
    timeout is clamped to what is left of the batch deadline, floored at
    ``_MIN_CLAMPED_TIMEOUT_S`` (300 s). A refusal now takes at least ~450 s, so
    a walk launched into a clamped timeout would be killed MID-WINDOW -- and a
    SIGKILL carries no exit code, so it counts a `consecutive_failure` and the
    breaker never learns the host refused us. That is strictly worse than the
    short window this PR replaced, which always finished (issue #357 review).

    So the window handed to the child is the smaller of the configured one and
    what its timeout can actually hold: the timeout, less one full final attempt
    (which starts at the window's edge) and less what the child spends getting
    to the fetch at all. When even that is gone, the child is given a SINGLE
    attempt -- the fetch still has to happen, and one attempt is the least it
    can cost.

    Returns ``policy`` unchanged when it already fits, so nothing changes on an
    unclamped night.
    """
    budget = timeout_s - OVERPASS_FINAL_ATTEMPT_RESERVE_S - OVERPASS_CHILD_STARTUP_RESERVE_S
    if budget >= policy.window_s:
        return policy
    if budget <= 0:
        return replace(policy, max_attempts=1)
    return replace(policy, window_s=budget)


class RetriesExhausted(Exception):
    """
    Every attempt :func:`call_with_retry` was allowed failed with a retryable fault.

    Carries the count and the elapsed wall clock so the caller's error can say
    how long the host was actually asked, and chains the last fault as
    ``__cause__`` so the real exception is one hop away rather than buried the
    way tenacity's ``RetryError`` buried it on 2026-08-14.
    """

    def __init__(self, attempts: int, elapsed_s: float, last: BaseException):
        super().__init__(f"{attempts} attempt(s) over {elapsed_s:.0f} s; last: {last}")
        self.attempts = attempts
        self.elapsed_s = elapsed_s
        self.last = last


def call_with_retry(
    fn: Callable[[], T],
    policy: OverpassRetryPolicy,
    *,
    retryable: tuple[type[BaseException], ...],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    rand: Callable[[], float],
    what: str = "Overpass",
) -> T:
    """
    Call ``fn`` until it returns, raises a non-retryable error, or ``policy`` gives up.

    ``sleep``, ``clock`` and ``rand`` are required rather than defaulted so a
    caller cannot silently get real sleeping in a test, and so the schedule is
    measurable end to end with a fake clock. A non-retryable exception
    propagates unchanged on the attempt that raised it -- a settled answer costs
    one round trip. Exhaustion raises :class:`RetriesExhausted` from the last
    retryable fault.
    """
    started = clock()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except retryable as e:
            elapsed = clock() - started
            if attempt >= policy.max_attempts:
                raise RetriesExhausted(attempt, elapsed, e) from e
            wait = policy.wait_s(attempt, rand())
            if elapsed + wait > policy.window_s:
                # The next attempt would start past the window, where the
                # fetch's deadline no longer covers a full attempt. Stop now
                # rather than sleep toward a request we will not be allowed
                # to finish.
                raise RetriesExhausted(attempt, elapsed, e) from e
            logger.warning(
                "%s unreachable (attempt %d of at most %d, %.0f s elapsed): %s; retrying in %.0f s",
                what,
                attempt,
                policy.max_attempts,
                elapsed,
                e,
                wait,
            )
            sleep(wait)


# ---------------------------------------------------------------------------
# The argv hop: scheduler config -> walk child (issue #357)
#
# The flag names live here, once, so the scheduler that writes them and the
# collector that parses them cannot drift apart. (flag, field) pairs, in the
# order they are emitted.
# ---------------------------------------------------------------------------

_ARGV_FIELDS = (
    ("--overpass-retry-attempts", "max_attempts"),
    ("--overpass-retry-initial-wait", "initial_wait_s"),
    ("--overpass-retry-max-wait", "max_wait_s"),
    ("--overpass-retry-jitter", "jitter"),
    ("--overpass-retry-window", "window_s"),
)


def overpass_retry_argv(policy: OverpassRetryPolicy) -> list[str]:
    """The ``collect`` flags that reproduce ``policy`` in the child, every field explicit."""
    argv: list[str] = []
    for flag, name in _ARGV_FIELDS:
        argv += [flag, f"{getattr(policy, name)!r}"]
    return argv


def add_overpass_retry_arguments(parser) -> None:
    """Register the ``--overpass-retry-*`` flags on an argparse parser.

    Defaults are None ("this field's policy default") rather than the numbers
    themselves, so :func:`overpass_retry_from_args` is the one place a default
    is applied and the help text cannot advertise a number the policy does not
    use.
    """
    defaults = OverpassRetryPolicy()
    helps = {
        "max_attempts": "most attempts at one network fetch, the first included",
        "initial_wait_s": "wait after the first failure, doubling after each "
        "further one (floor: the usage policy's "
        f"{OVERPASS_MIN_RETRY_WAIT_S:g} s)",
        "max_wait_s": "cap on the doubling wait, before jitter",
        "jitter": "each wait is stretched by up to this fraction, upward only",
        "window_s": "give up rather than start a wait that would end this many "
        f"seconds after the first attempt (at most {OVERPASS_RETRY_WINDOW_CEILING_S:g})",
    }
    for flag, name in _ARGV_FIELDS:
        parser.add_argument(
            flag,
            dest=f"overpass_retry_{name}",
            type=int if name == "max_attempts" else float,
            default=None,
            help=(
                f"Overpass retry (issue #357): {helps[name]}. "
                f"Default: {getattr(defaults, name)!r}; the scheduler passes [overpass]."
            ),
        )


def overpass_retry_from_args(args) -> OverpassRetryPolicy:
    """Build the policy from parsed ``--overpass-retry-*`` flags; ValueError if invalid."""
    kwargs = {}
    for _flag, name in _ARGV_FIELDS:
        value = getattr(args, f"overpass_retry_{name}", None)
        if value is not None:
            kwargs[name] = value
    return OverpassRetryPolicy(**kwargs)
