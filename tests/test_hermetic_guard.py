"""The suite's no-real-network guard (tests/conftest.py `_no_real_network`).

Added in the PR #411 review after a test file drove `run-due` without stubs and
fetched Google's driving-plan feed for real. These pin that the guard both
RAISES at the connect and FAILS the test even when the code under test
swallows the error -- the second half is the one that matters, because the
driving-plan hook reports a failed fetch as a string.
"""

import os
import socket

import pytest

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
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:playwright", "-p", "no:base_url"
    )
    # The connect raised (the assert inside passed), AND teardown errored the test.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*attempted 1 real network connection*"])


def test_an_e2e_test_is_exempt_from_the_guard(pytester):
    """Review 411e #1: tests/e2e talks to the network by design (the CARTO
    watermark detector fetches a real tile) and CI runs `pytest tests/e2e -m
    e2e`, so an e2e-marked test gets the REAL connect and getaddrinfo while an
    unmarked one in the same run is guarded. Asserted by identity, so no packet
    leaves the machine. Killed by dropping the exemption."""
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {_REPO!r})\n"
        "from tests.conftest import _no_real_network  # noqa: F401\n"
    )
    pytester.makepyfile(
        f"""
        import socket
        import sys

        import pytest

        sys.path.insert(0, {_REPO!r})
        from tests.conftest import _REAL_GETADDRINFO, _REAL_SOCKET_CONNECT

        @pytest.mark.e2e
        def test_e2e_gets_the_real_network():
            assert socket.socket.connect is _REAL_SOCKET_CONNECT
            assert socket.getaddrinfo is _REAL_GETADDRINFO

        def test_an_ordinary_test_is_guarded():
            assert socket.socket.connect is not _REAL_SOCKET_CONNECT
            assert socket.getaddrinfo is not _REAL_GETADDRINFO
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:playwright", "-p", "no:base_url"
    )
    result.assert_outcomes(passed=2)


def test_a_file_under_tests_e2e_is_exempt_without_its_marker():
    """The path half of the exemption: a new e2e file that forgets its marker
    is still not failed by the guard."""
    from pathlib import Path
    from types import SimpleNamespace

    from tests.conftest import _E2E_DIR, _is_e2e

    def node(path):
        return SimpleNamespace(
            node=SimpleNamespace(get_closest_marker=lambda name: None, path=Path(path))
        )

    assert _is_e2e(node(os.path.join(_E2E_DIR, "test_new.py")))
    assert not _is_e2e(node(os.path.join(os.path.dirname(_E2E_DIR), "test_scheduler.py")))


def test_a_dns_lookup_of_a_real_name_is_blocked_and_fails_the_test(pytester):
    """Review 411e #6: no DNS query leaks either; a swallowed gaierror still
    errors the test."""
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {_REPO!r})\n"
        "from tests.conftest import _no_real_network  # noqa: F401\n"
    )
    pytester.makepyfile(
        """
        import socket

        def test_swallows_dns():
            try:
                socket.getaddrinfo("example.invalid", 80)
            except socket.gaierror as e:
                assert type(e).__name__ == "RealDNSBlocked"
        """
    )
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:playwright", "-p", "no:base_url"
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*real network connection(s) or lookup(s)*example.invalid*"])


def test_loopback_means_the_whole_loopback_range():
    from tests.conftest import _is_loopback

    for host in ("127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1", "localhost"):
        assert _is_loopback(host), host
    for host in ("192.0.2.1", "::ffff:192.0.2.1", "example.com", "10.0.0.1"):
        assert not _is_loopback(host), host


def test_loopback_names_and_ip_literals_still_resolve():
    socket.getaddrinfo("localhost", 80)
    socket.getaddrinfo("192.0.2.1", 80)  # a literal resolves locally, no query


def test_write_backup_is_stubbed_unless_the_test_asks_for_it():
    """The exemption is a marker, not a module name."""
    from streetscape_metadata_tracker import catalog_backup

    assert catalog_backup.write_backup.__name__ == "<lambda>"


@pytest.mark.real_catalog_backup
def test_write_backup_is_real_under_its_marker():
    from streetscape_metadata_tracker import catalog_backup

    assert catalog_backup.write_backup.__name__ == "write_backup"


def test_scheduler_config_defaults_are_the_repo_dirs_and_tests_redirect_them(tmp_path):
    """The real defaults are the operator's <repo>/data, /backups and /logs --
    pinned so a change to them is a decision -- and a test's default config is
    redirected under tmp_path by conftest, never the repo."""
    from streetscape_metadata_tracker import scheduler as sched

    fields = sched.SchedulerConfig.__dataclass_fields__
    root = os.path.dirname(os.path.dirname(os.path.abspath(sched.__file__)))
    assert fields["data_dir"].default == os.path.join(root, "data")
    assert fields["backup_dir"].default == os.path.join(root, "backups")
    assert fields["log_dir"].default == os.path.join(root, "logs")
    cfg = sched.SchedulerConfig()
    for path in (cfg.data_dir, cfg.backup_dir, cfg.log_dir):
        assert path.startswith(str(tmp_path)), path
