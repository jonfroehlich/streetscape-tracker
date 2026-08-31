"""load_config tests — the function cli.py's both-provider fail-fast relies on."""

import inspect
import os
import re
from unittest import mock

import pytest

from streetscape_metadata_tracker import config
from streetscape_metadata_tracker.config import load_config, warn_if_credentials_world_readable


def test_load_config_gsv_requires_only_its_own_key(monkeypatch):
    monkeypatch.setenv("GMAPS_API_KEY", "test-key")
    monkeypatch.delenv("MAPILLARY_ACCESS_TOKEN", raising=False)
    assert load_config("gsv") == {"api_key": "test-key"}


def test_load_config_mapillary_requires_only_its_own_token(monkeypatch):
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "MLY|test|token")
    monkeypatch.delenv("GMAPS_API_KEY", raising=False)
    assert load_config("mapillary") == {"access_token": "MLY|test|token"}


def test_load_config_streets_channels_are_isolated(monkeypatch):
    """The dormant street-collection channels (issue #99) read their OWN env
    vars — the production keys must never satisfy a street channel."""
    monkeypatch.setenv("GMAPS_API_KEY", "prod-key")
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "MLY|prod|token")
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "streets-key")
    monkeypatch.setenv("MAPILLARY_STREETS_ACCESS_TOKEN", "MLY|streets|token")
    assert load_config("gsv_streets") == {"api_key": "streets-key"}
    assert load_config("mapillary_streets") == {"access_token": "MLY|streets|token"}

    # With only production keys set, the street channels still fail fast.
    monkeypatch.delenv("GMAPS_STREETS_API_KEY")
    monkeypatch.delenv("MAPILLARY_STREETS_ACCESS_TOKEN")
    with pytest.raises(ValueError, match="GMAPS_STREETS_API_KEY"):
        load_config("gsv_streets")
    with pytest.raises(ValueError, match="MAPILLARY_STREETS_ACCESS_TOKEN"):
        load_config("mapillary_streets")


@pytest.mark.parametrize(
    "provider,env_var",
    [
        ("gsv", "GMAPS_API_KEY"),
        ("mapillary", "MAPILLARY_ACCESS_TOKEN"),
        ("gsv_streets", "GMAPS_STREETS_API_KEY"),
        ("mapillary_streets", "MAPILLARY_STREETS_ACCESS_TOKEN"),
        ("kartaview", "KARTAVIEW_ACCESS_TOKEN"),
    ],
)
def test_load_config_missing_credential_raises_with_var_name(monkeypatch, provider, env_var):
    monkeypatch.delenv(env_var, raising=False)
    with pytest.raises(ValueError, match=env_var):
        load_config(provider)


@pytest.mark.parametrize("provider,env_var", [("gsv", "GMAPS_API_KEY")])
def test_load_config_empty_credential_raises(monkeypatch, provider, env_var):
    monkeypatch.setenv(env_var, "")
    with pytest.raises(ValueError, match=env_var):
        load_config(provider)


def test_load_config_reads_no_env_var_outside_the_declared_table():
    """
    CHANNEL_ENV_VARS is only worth having if it cannot drift from the if-chain
    it describes -- and that chain is a sequence of `if provider == ...` blocks
    with no enumerable form, which is how #258 added a channel that no table
    mentioned.

    So: read the source and assert every credential variable load_config
    actually reaches for is declared. Adding a channel then fails HERE (and in
    the router's credentials-table test) until it is written down.
    """
    source = inspect.getsource(config.load_config)
    read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"\)', source))
    declared = {var for variables in config.CHANNEL_ENV_VARS.values() for var in variables}
    assert read == declared


@pytest.mark.parametrize("channel", sorted(config.CHANNEL_ENV_VARS))
def test_every_declared_channel_loads_from_its_own_variable(channel):
    """
    The other direction: a declared channel must be one load_config accepts,
    reading the variable the table names first. Pinned per channel so a typo in
    the table is a named failure rather than a KeyError somewhere downstream.
    """
    primary = config.CHANNEL_ENV_VARS[channel][0]
    with mock.patch.dict(os.environ, {primary: "SENTINEL"}, clear=True):
        loaded = config.load_config(channel)
    assert "SENTINEL" in loaded.values()


def test_the_kartaview_walk_falls_back_but_prefers_its_own_token():
    """
    The one declared fallback, in both directions. It exists because a KartaView
    walk and grid run are serialized by one machine-wide host lock, so there is
    no parallel quota burn to isolate -- but an operator who DOES set a separate
    token must get it, or the isolation they asked for silently does nothing.
    """
    with mock.patch.dict(os.environ, {"KARTAVIEW_ACCESS_TOKEN": "SHARED"}, clear=True):
        assert config.load_config("kartaview_streets")["access_token"] == "SHARED"

    with mock.patch.dict(
        os.environ,
        {"KARTAVIEW_ACCESS_TOKEN": "SHARED", "KARTAVIEW_STREETS_ACCESS_TOKEN": "OWN"},
        clear=True,
    ):
        assert config.load_config("kartaview_streets")["access_token"] == "OWN"


def test_load_config_unknown_provider_raises():
    # NOT "kartaview": that is a real channel now (issue #225), and a sentinel
    # that a later phase registers turns this test from a guard into a failure
    # nobody expected.
    with pytest.raises(ValueError, match="[Uu]nknown provider"):
        load_config("not-a-provider")


# --- warn_if_credentials_world_readable: .env mode check --------------------


def test_env_mode_600_is_quiet(tmp_path, caplog):
    env = tmp_path / ".env"
    env.write_text("GMAPS_API_KEY=k\n")
    env.chmod(0o600)
    assert warn_if_credentials_world_readable(str(env)) is False
    assert "chmod 600" not in caplog.text


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_env_group_or_world_readable_warns(tmp_path, caplog, mode):
    env = tmp_path / ".env"
    env.write_text("GMAPS_API_KEY=k\n")
    env.chmod(mode)
    assert warn_if_credentials_world_readable(str(env)) is True
    assert "chmod 600" in caplog.text
    # The warning must never include the credential value itself.
    assert "GMAPS_API_KEY=k" not in caplog.text


def test_env_missing_or_empty_path_is_noop():
    assert warn_if_credentials_world_readable("") is False
    assert warn_if_credentials_world_readable("/nonexistent/.env") is False
