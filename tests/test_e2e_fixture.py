"""The committed e2e fixture may not fall behind the provider set (issue #354).

This is a FAST-suite test about the browser suite's data, and both halves of
that are deliberate.

It is about the committed ``tests/e2e/fixture/`` artifacts rather than about
``build_fixture.py``, because those bytes are what a browser renders: a builder
that constructs a fourth provider and a fixture directory that was never
regenerated is the same failure to a reader of ``grid.html``, and only the
artifacts can tell the two apart from a green suite.

And it is in the FAST suite rather than beside the tests it protects, because
the e2e job is ``continue-on-error: true`` — a guard that only runs there can
be red for weeks. It needs no browser and no Playwright to ask its question,
so there is no reason for it to live where nothing blocks on the answer.

What it guards, from issue #354: every width on the pivoted ``grid.html`` /
``streets.html`` tables is a function of the COLLECTED provider count, since
each metric group renders one leaf per provider. A fixture one provider short
of production therefore renders one column narrower per group, and an overflow
gate asked of it is asking a question the payload cannot answer yes to. That
has happened twice — the fixture carried two providers while production
carried three (#334), then three while production carried four (#350/#351) —
and both times it surfaced as a layout defect on the live site rather than as
a failing test.
"""

import gzip
import json
import pathlib

from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS
from tests.e2e.build_fixture import FIXTURE_OMITTED_PROVIDERS

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "e2e" / "fixture"

# What to do about a failure here, appended to every message. Spelled out
# because the fix is three steps and skipping the last one (committing the
# regenerated artifacts) leaves this test red with the builder already correct.
_REMEDY = (
    "Either add the provider to tests/e2e/build_fixture.py's build() — a grid "
    "run and a road walk on the multi-provider city — then re-run "
    "`python tests/e2e/build_fixture.py` and commit the regenerated "
    "tests/e2e/fixture/ artifacts; or, if it genuinely should not be in the "
    "fixture, name it in build_fixture.FIXTURE_OMITTED_PROVIDERS with the "
    "reason."
)


def _read_fixture_json(name):
    with gzip.open(FIXTURE_DIR / name, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _expected_providers():
    """The providers the fixture owes, i.e. the registry less the omissions."""
    return set(KNOWN_PROVIDERS) - set(FIXTURE_OMITTED_PROVIDERS)


def test_the_fixture_carries_every_known_provider_on_a_single_grid_row():
    """One CITY has to hold them all, not the fixture as a whole.

    A grid row is a city, so the widest row the chassis is asked to render is
    the richest single city's — spreading four providers over four cities
    would satisfy a union check and still leave every rendered row three
    columns narrower than production's. Alpha City is that city.
    """
    expected = _expected_providers()
    cities = _read_fixture_json("cities.json.gz")["cities"]
    by_city = {city["city_id"]: set(city["providers"]) for city in cities}

    widest = max(by_city.values(), key=len)
    assert expected <= widest, (
        "no fixture city carries a grid run for every known provider; missing "
        f"{sorted(expected - widest)} from the richest city. {_REMEDY}"
    )


def test_the_fixture_carries_every_known_provider_on_a_single_streets_row():
    """The same property for ``streets.html``, whose row is a (city, network).

    Asserted separately rather than inferred from the grid one because the two
    payloads are separate: a provider with a run and no walk widens
    ``grid.html`` and leaves ``streets.html`` a column short, which is a state
    this fixture was actually in (every provider was walked, but nothing said
    it had to be).

    Grouped by network because two networks are two different street-km
    denominators and never share a row — so a provider walked only on
    ``all_public`` would not widen the default ``drive`` view at all.
    """
    expected = _expected_providers()
    walks = _read_fixture_json("streetwalks.json.gz")["walks"]

    by_row = {}
    for walk in walks:
        by_row.setdefault((walk["city_id"], walk["network_type"]), set()).add(walk["provider"])

    widest = max(by_row.values(), key=len)
    assert expected <= widest, (
        "no (city, network) in the streetwalk manifest was walked by every "
        f"known provider; missing {sorted(expected - widest)} from the "
        f"richest row. {_REMEDY}"
    )


def test_the_fixture_names_no_provider_this_build_does_not_know():
    """The other direction, and the one that reads as data rather than layout.

    ``getProviderFromFilename`` returned ``"gsv"`` for an unrecognised token
    until #338 and returns ``null`` now, so a fixture artifact carrying a
    provider the registry has never heard of does not render as itself either
    way. Both published artifacts are swept, since a token can reach the
    browser through either.
    """
    cities = _read_fixture_json("cities.json.gz")["cities"]
    walks = _read_fixture_json("streetwalks.json.gz")["walks"]
    published = {p for city in cities for p in city["providers"]}
    published |= {walk["provider"] for walk in walks}

    unknown = sorted(published - set(KNOWN_PROVIDERS))
    assert not unknown, (
        f"the committed fixture publishes {unknown}, which naming."
        "KNOWN_PROVIDERS does not list — the frontend cannot render it as "
        "itself."
    )


def test_every_fixture_omission_is_an_explicit_named_decision():
    """An omission has to stay a decision, which means it can go stale.

    Two ways it rots, and the checks are for those rather than for the dict's
    shape: a key that is no longer a provider (renamed, retired) silently
    exempts nothing, and a key whose provider IS now in the fixture claims a
    gap that has been closed. Either leaves a line in the dict that reads as a
    live decision and is not one.
    """
    stale = sorted(set(FIXTURE_OMITTED_PROVIDERS) - set(KNOWN_PROVIDERS))
    assert not stale, (
        f"build_fixture.FIXTURE_OMITTED_PROVIDERS exempts {stale}, which is "
        "not in naming.KNOWN_PROVIDERS — the exemption no longer exempts "
        "anything and should be deleted."
    )

    unreasoned = sorted(
        p for p, why in FIXTURE_OMITTED_PROVIDERS.items() if not (why or "").strip()
    )
    assert not unreasoned, (
        f"build_fixture.FIXTURE_OMITTED_PROVIDERS exempts {unreasoned} with no "
        "reason written down; the reason is the whole difference between an "
        "exemption and an omission."
    )

    cities = _read_fixture_json("cities.json.gz")["cities"]
    walks = _read_fixture_json("streetwalks.json.gz")["walks"]
    published = {p for city in cities for p in city["providers"]}
    published |= {walk["provider"] for walk in walks}

    contradicted = sorted(set(FIXTURE_OMITTED_PROVIDERS) & published)
    assert not contradicted, (
        f"build_fixture.FIXTURE_OMITTED_PROVIDERS exempts {contradicted}, but "
        "the committed fixture publishes them — drop the exemption rather "
        "than leaving it to excuse a gap that is closed."
    )
