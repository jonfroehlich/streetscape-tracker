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

Two limits worth knowing before trusting this file, both found by the #360
review:

* **It is the registry it compares against, not production.** ``KNOWN_PROVIDERS``
  is what these tests read, so the state that actually caused #334 — a provider
  being COLLECTED in production while still unregistered here, which is where
  Panoramax sat for three PRs — is outside what this can see. What covers that
  half is the JS↔Python registry pin (#334, #338). "The fixture cannot fall
  behind the registry" is the property enforced; "the fixture cannot fall
  behind production" is the one people will remember it as.
* **It is stricter than the width property it protects.** The column COUNT is
  derived from the union over the whole payload (``grid.js:pivotGridRows``,
  ``streets.js:walkProvidersIn``), so four providers on four different cities
  render exactly as wide as four on one — measured in the browser, the same
  174px/266px overflow either way. What a single rich row buys is not width but
  POPULATED CELLS, which is a different and still necessary thing: see the two
  row tests below.
"""

import csv
import gzip
import json
import pathlib
import re
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import naming
from streetscape_metadata_tracker.fileutils import dtypes_for_run_path, load_city_csv_file
from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS
from tests.e2e import build_fixture

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "e2e" / "fixture"

# Artifacts that are not per-(city, provider) and so carry no provider token:
# the aggregate, the walk manifest and the driving-plan join. A run diff is
# excluded by its own name shape rather than listed, since its date pair moves.
_UNTOKENED_ARTIFACTS = {"cities.json.gz", "streetwalks.json.gz", "driving_plan.json.gz"}

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


# --------------------------------------------------------------------------
# The checks, as functions of their inputs.
#
# Written this way for one reason: with FIXTURE_OMITTED_PROVIDERS empty — which
# it is, and which is the point — every assertion about it is made over an
# empty comprehension and holds no matter what the predicate says. The #360
# review deleted all three of its checks and the file still passed. So the
# predicates live here, where a test can hand them the dict that makes each one
# fire, and the tests below apply them to the committed artifacts.
# --------------------------------------------------------------------------


def _expected_providers(omissions=None):
    """The providers the fixture owes, i.e. the registry less the omissions."""
    if omissions is None:
        omissions = build_fixture.FIXTURE_OMITTED_PROVIDERS
    return set(KNOWN_PROVIDERS) - set(omissions)


def _row_gap(expected, rows):
    """What is missing from the FULLEST row, or ``[]`` if one row holds them all.

    ``any(expected <= row)`` is the property; the rest is the message. Reporting
    the smallest gap rather than the largest row keeps the failure pointed at
    the row a maintainer should extend, and an empty payload reports everything
    missing instead of raising out of ``max()`` on no rows.
    """
    gaps = sorted((sorted(expected - row) for row in rows), key=lambda gap: (len(gap), gap))
    return gaps[0] if gaps else sorted(expected)


def _omission_problems(omissions, published):
    """Every way the omissions dict has rotted, as messages.

    An omission has to stay a DECISION, and there are three ways it stops
    being one: a key that is no longer a provider (renamed, retired) exempts
    nothing; a key with no reason is indistinguishable from an oversight, the
    reason being the entire difference between an exemption and an omission;
    and a key whose provider IS in the fixture claims a gap that has closed.
    """
    problems = []

    stale = sorted(set(omissions) - set(KNOWN_PROVIDERS))
    if stale:
        problems.append(
            f"build_fixture.FIXTURE_OMITTED_PROVIDERS exempts {stale}, which is "
            "not in naming.KNOWN_PROVIDERS — the exemption no longer exempts "
            "anything and should be deleted."
        )

    unreasoned = sorted(p for p, why in omissions.items() if not (why or "").strip())
    if unreasoned:
        problems.append(
            f"build_fixture.FIXTURE_OMITTED_PROVIDERS exempts {unreasoned} with no "
            "reason written down; the reason is the whole difference between an "
            "exemption and an omission."
        )

    contradicted = sorted(set(omissions) & set(published))
    if contradicted:
        problems.append(
            f"build_fixture.FIXTURE_OMITTED_PROVIDERS exempts {contradicted}, but "
            "the committed fixture publishes them — drop the exemption rather "
            "than leaving it to excuse a gap that is closed."
        )

    return problems


# --------------------------------------------------------------------------
# Readers over the committed artifacts.
# --------------------------------------------------------------------------


def _read_fixture_json(name):
    with gzip.open(FIXTURE_DIR / name, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _grid_rows():
    """One provider set per grid row, i.e. per city."""
    cities = _read_fixture_json("cities.json.gz")["cities"]
    return [set(city["providers"]) for city in cities]


def _streets_rows():
    """One provider set per streets row, i.e. per (city, network_type)."""
    by_row = {}
    for walk in _read_fixture_json("streetwalks.json.gz")["walks"]:
        by_row.setdefault((walk["city_id"], walk["network_type"]), set()).add(walk["provider"])
    return list(by_row.values())


def _published_providers():
    """Every provider named by either published payload."""
    published = {p for row in _grid_rows() for p in row}
    return published | {p for row in _streets_rows() for p in row}


def _artifact_provider(name):
    """The provider this build resolves a fixture FILENAME to, or ``None``.

    ``None`` means "no provider-tagged name this build can parse", which covers
    both the artifacts that carry no token at all (the aggregate, the manifest,
    the driving-plan join, a run diff) and the case the sweep is looking for: a
    token the registry does not list, which ``parse_filename`` refuses.
    """
    candidates = [name]
    suffix = "_coverage.json.gz"
    if name.endswith(suffix):
        # A walk's coverage sidecar is its csv name plus a suffix; the walk
        # parser is the one that knows the rest of the shape.
        candidates.append(name[: -len(suffix)] + ".csv.gz")
    for candidate in candidates:
        for parse in (naming.parse_filename, naming.parse_streetwalk_filename):
            try:
                return parse(candidate).provider
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------
# The guard.
# --------------------------------------------------------------------------


def test_the_fixture_carries_every_known_provider_on_a_single_grid_row():
    """One CITY has to hold them all, not the fixture as a whole.

    Not for WIDTH: ``grid.js:pivotGridRows`` builds the provider list from the
    union over every city and then emits one leaf per provider on every row,
    filling the absent ones with em-dashes, so four providers spread over four
    cities render exactly as wide as four on one (measured — the same 174px
    overflow either way). The per-row property is about what those leaves
    CONTAIN: only a city collected by all four makes every provider cell hold a
    real number, which is what the positional assertions in ``test_smoke.py``
    read — Alpha City's 12 ``provider-cell-link`` hrefs, and the ``nth(i)``
    coverage cells that are identified by nothing but their column position.
    A row of em-dashes satisfies a union check and pins none of that.
    """
    expected = _expected_providers()
    missing = _row_gap(expected, _grid_rows())
    assert not missing, (
        f"no fixture city carries a grid run for every known provider; {missing} "
        f"missing from the fullest city. {_REMEDY}"
    )


def test_the_fixture_carries_every_known_provider_on_a_single_streets_row():
    """The same property for ``streets.html``, whose row is a (city, network).

    Asserted separately rather than inferred from the grid one because the two
    payloads are separate: a provider with a grid run and no walk populates
    ``grid.html``'s cells and leaves ``streets.html``'s reading em-dashes, and
    nothing about the grid half would notice.

    Grouped by network for the same cell-population reason: two networks are
    two different street-km denominators and never share a row, so a provider
    walked only on ``all_public`` leaves every ``drive`` row's cell for it
    empty — even though the column itself would still be rendered, since
    ``walkProvidersIn`` unions over the whole manifest, networks included.
    """
    expected = _expected_providers()
    missing = _row_gap(expected, _streets_rows())
    assert not missing, (
        "no (city, network) in the streetwalk manifest was walked by every "
        f"known provider; {missing} missing from the fullest row. {_REMEDY}"
    )


def test_the_fixture_names_no_provider_this_build_does_not_know():
    """The other direction, swept over both the payloads and the FILENAMES.

    ``getProviderFromFilename`` returned ``"gsv"`` for an unrecognised token
    until #338 and returns ``null`` now, so a fixture artifact carrying a
    provider the registry has never heard of does not render as itself either
    way. The payload half is where a token reaches the tables; the filename
    half is where it reaches ``city.html``, which is addressed by run filename
    and derives the provider from it — and the #360 review noted that a test
    naming that mechanism while reading only the payload would miss a stray
    artifact entirely.
    """
    unknown = sorted(_published_providers() - set(KNOWN_PROVIDERS))
    assert not unknown, (
        f"the committed fixture publishes {unknown}, which naming."
        "KNOWN_PROVIDERS does not list — the frontend cannot render it as "
        "itself."
    )

    unplaceable = sorted(
        name
        for name in (p.name for p in FIXTURE_DIR.iterdir())
        if name not in _UNTOKENED_ARTIFACTS
        and "_diff_" not in name
        and _artifact_provider(name) is None
    )
    assert not unplaceable, (
        f"the committed fixture holds {unplaceable}, whose name this build "
        "cannot resolve to a provider — either the token is one "
        "naming.KNOWN_PROVIDERS does not list (city.html refuses such a run "
        "since #338, and rendered it as Google's before) or the name is not "
        "one the naming generators emit at all."
    )


def test_every_fixture_omission_is_an_explicit_named_decision():
    """The live dict, against the live fixture.

    Empty today, so this passes trivially — which is why the predicates it
    applies are exercised on their own, below, rather than trusted because this
    is green.
    """
    problems = _omission_problems(build_fixture.FIXTURE_OMITTED_PROVIDERS, _published_providers())
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# The guard's own behaviour, on inputs that make it fire (#360 review).
#
# The five rows of the control-run table this PR reported by hand. They were
# each verified once and would never have been re-verified, and the exemption
# dict is the ONE lever that can weaken everything above it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "omissions, published, expected_phrase",
    [
        pytest.param(
            {"notaprovider": "stale"},
            {"gsv"},
            "not in naming.KNOWN_PROVIDERS",
            id="key-is-not-a-provider",
        ),
        pytest.param(
            {"kartaview": ""},
            {"gsv"},
            "no reason written down",
            id="reason-is-empty",
        ),
        pytest.param(
            {"kartaview": "   "},
            {"gsv"},
            "no reason written down",
            id="reason-is-whitespace",
        ),
        pytest.param(
            {"kartaview": None},
            {"gsv"},
            "no reason written down",
            id="reason-is-none",
        ),
        pytest.param(
            {"kartaview": "not collected yet"},
            {"gsv", "kartaview"},
            "the committed fixture publishes them",
            id="gap-has-closed",
        ),
    ],
)
def test_a_rotten_omission_is_reported(omissions, published, expected_phrase):
    problems = _omission_problems(omissions, published)
    assert problems, f"{omissions} should have been reported and was not"
    assert any(expected_phrase in p for p in problems), problems


def test_a_named_omission_with_a_reason_is_the_escape_hatch():
    """The direction that has to KEEP working, or the dict is a trap.

    A provider named with a reason and genuinely absent from the fixture is
    the one state that is allowed: it drops out of what the row tests demand
    and reports no problem of its own.
    """
    omissions = {"kartaview": "no walk collected on the test grid yet"}
    assert "kartaview" not in _expected_providers(omissions)
    assert _omission_problems(omissions, {"gsv", "mapillary", "panoramax"}) == []
    rows = [{"gsv", "mapillary", "panoramax"}]
    assert _row_gap(_expected_providers(omissions), rows) == []


def test_a_provider_on_no_single_row_is_reported():
    """The case the guard exists for, and the two near-misses around it.

    A fifth provider nobody collected fails. So does four providers spread one
    per row — the union is complete and no row is — which is the arrangement
    that would satisfy a union check. And an empty payload reports every
    provider rather than raising out of an aggregate over no rows.
    """
    expected = {"gsv", "mapillary", "kartaview", "panoramax"}

    assert _row_gap(expected | {"newprovider"}, [expected]) == ["newprovider"]
    # One provider per row: the union is complete, every row is three short,
    # and the message names the smallest gap (ties broken alphabetically, so
    # this is the row that holds "panoramax").
    assert _row_gap(expected, [{p} for p in sorted(expected)]) == [
        "gsv",
        "kartaview",
        "mapillary",
    ]
    assert _row_gap(expected, []) == sorted(expected)
    assert _row_gap(expected, [expected]) == []


# --------------------------------------------------------------------------
# The bytes themselves (#360 review).
# --------------------------------------------------------------------------


def test_every_committed_run_csv_writes_its_integer_columns_as_integers():
    """A fixture run file has to read back as the schema its NAME claims.

    The instance this caught: ``make_kartaview_city_df`` built
    ``sequence_index`` from Python ints beside the ``None`` its ZERO_RESULTS
    row carries, pandas inferred float64, and the CSV said ``0.0`` — so
    ``city.js`` rendered ``details/11616154/0.0``, a link that opens nothing.
    The browser is the only place that showed, because the browser reads the
    CSV TEXT; and deleting the fix left the whole fast suite green (#360
    review).

    Asserted over the raw text as well as the parsed frame, for that reason,
    and over every committed run rather than that one column — the class is
    "a nullable integer written through a float", and every census schema
    has candidates.
    """
    checked = 0
    for path in sorted(FIXTURE_DIR.iterdir()):
        name = path.name
        if not name.endswith(".csv.gz") or "_diff_" in name or "_streetwalk_" in name:
            continue
        dtypes = dtypes_for_run_path(name)
        int_columns = [
            column
            for column, dtype in dtypes.items()
            if isinstance(dtype, pd.Int64Dtype) or dtype is int
        ]

        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows, f"{name} has no rows"

        for column in int_columns:
            if column not in rows[0]:
                continue
            for row in rows:
                value = row[column]
                assert value == "" or re.fullmatch(r"-?\d+", value), (
                    f"{name} writes {column}={value!r}, which is not an integer — "
                    "a nullable int column that picked up a None inferred float64 "
                    "somewhere in the builder, and the browser reads this text."
                )

        # ...and the frame the readers get back carries the declared types,
        # which is the same contract from the other end.
        frame = load_city_csv_file(str(path))
        for column, dtype in dtypes.items():
            if column in frame.columns and isinstance(dtype, pd.api.extensions.ExtensionDtype):
                assert frame[column].dtype == dtype, (
                    f"{name} column {column} loads as {frame[column].dtype}, not {dtype}"
                )
        checked += 1

    assert checked >= len(KNOWN_PROVIDERS), (
        f"only {checked} run CSVs were checked; the fixture should hold at least one per provider"
    )


def test_every_census_builder_writes_its_integer_columns_as_integers(tmp_path):
    """The same contract one step earlier, on the builders themselves.

    The test above reads the committed bytes, so a builder edited without a
    regeneration is invisible to it — and that is the order the mistake is
    actually made in. This one writes each census builder's frame through the
    fixture's own writer and reads the text back, so deleting the cast in
    ``make_kartaview_city_df`` fails here immediately rather than waiting for
    someone to run ``build_fixture.py``.

    Parametrized over the three census builders rather than the one that broke,
    because the class is "a nullable integer column that picked up a ``None``",
    and every census schema has a candidate.
    """
    from tests.conftest import (
        make_kartaview_city_df,
        make_mapillary_city_df,
        make_panoramax_city_df,
        write_city_csv_gz,
    )

    builders = {
        "mapillary": make_mapillary_city_df,
        "kartaview": make_kartaview_city_df,
        "panoramax": make_panoramax_city_df,
    }
    panos = [("id1", "2024-05-01"), ("id2", "2025-05-01")]

    for provider, builder in builders.items():
        # n_empty=1 is what makes this bite: the ZERO_RESULTS row is the None
        # that turns an int column into float64.
        frame = builder(panos, n_empty=1, n_flat_only=1)
        name = naming.generate_run_filename(
            "alpha-city--alphastate--testland", 100, 100, 20, date(2026, 4, 15), provider=provider
        )
        path = tmp_path / f"{name}.csv.gz"
        write_city_csv_gz(frame, str(path))

        dtypes = dtypes_for_run_path(path.name)
        assert dtypes is not None
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for column, dtype in dtypes.items():
            if not isinstance(dtype, pd.Int64Dtype) or column not in rows[0]:
                continue
            for row in rows:
                value = row[column]
                assert value == "" or re.fullmatch(r"-?\d+", value), (
                    f"{provider}'s builder writes {column}={value!r}; a nullable "
                    "int column inferred float64 on the way to the CSV."
                )


def test_every_committed_artifact_is_byte_reproducible():
    """Identical content has to be identical bytes, or a fixture diff lies.

    ``gzip.open`` stamps the current time into every member's MTIME header and
    three artifacts carry a wall-clock ``generated_at``, so before the #360
    review a regeneration rewrote all 24 files whether or not any content had
    moved — and on a commit whose entire substance is regenerated artifacts,
    "which of these actually changed" is the review. ``build_fixture``'s
    ``_normalize_for_commit`` is what makes the directory content-addressed;
    this is what stops the next regeneration quietly dropping it.
    """
    for path in sorted(FIXTURE_DIR.iterdir()):
        header = path.read_bytes()[:10]
        assert header[:2] == b"\x1f\x8b", f"{path.name} is not gzip"
        mtime = int.from_bytes(header[4:8], "little")
        assert mtime == 0, (
            f"{path.name} carries a gzip mtime of {mtime} — it was written by "
            "gzip.open rather than through build_fixture._normalize_for_commit, "
            "so its bytes change on every regeneration."
        )
        assert not header[3] & 0x08, (
            f"{path.name} carries a gzip FNAME header, which is one more thing "
            "its bytes depend on; _normalize_for_commit writes filename=''."
        )

        if not path.name.endswith(".json.gz"):
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            document = json.load(fh)
        if isinstance(document, dict) and "generated_at" in document:
            assert document["generated_at"] == build_fixture.FIXTURE_GENERATED_AT, (
                f"{path.name} carries a wall-clock generated_at; it should be "
                "build_fixture.FIXTURE_GENERATED_AT."
            )
