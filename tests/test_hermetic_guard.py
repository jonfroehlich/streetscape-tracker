"""The suite's no-real-network guard (tests/conftest.py `_no_real_network`).

Added in the PR #411 review after a test file drove `run-due` without stubs and
fetched Google's driving-plan feed for real. These pin that the guard both
RAISES at the connect and FAILS the test even when the code under test
swallows the error -- the second half is the one that matters, because the
driving-plan hook reports a failed fetch as a string.
"""

import os
import socket

pytest_plugins = ["pytester"]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_loopback_is_not_blocked():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=1)
        client.close()
    finally:
        server.close()


def test_a_swallowed_connection_attempt_still_fails_the_test(pytester):
    """Killed by not failing at teardown."""
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {_REPO!r})\n"
        "from tests.conftest import _no_real_network  # noqa: F401\n"
    )
    pytester.makepyfile(
        """
        import socket

        def test_swallows():
            try:
                socket.create_connection(("192.0.2.1", 80), timeout=0.01)  # TEST-NET-1
            except OSError as e:
                assert type(e).__name__ == "RealNetworkBlocked"
        """
    )
    result = pytester.runpytest(
        "-p", "no:cacheprovider", "-p", "no:playwright", "-p", "no:base_url"
    )
    # The connect raised (the assert inside passed), AND teardown errored the test.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*attempted 1 real network connection*"])
