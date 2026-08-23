"""Contract tests for CLAUDE.md as a router, and for the docs it delegates to.

CLAUDE.md is loaded into every session, and Claude Code truncates it past
150,000 characters. It reached 182,499 by growing roughly a paragraph per PR --
structural growth, not a one-off -- and was split into a router plus eleven
topic docs to get back under the limit. Nothing about that split is
self-enforcing: the file resumes growing the day it lands, a stub can name a doc
that was never written, and a doc can be left with no stub pointing at it.

This repo's convention is to enforce a contract rather than document it (see
``census.py``'s ``image_columns``, and
``test_every_scheduled_channel_declares_its_per_ip_hosts`` asserting set
equality). These are the equivalent for the split. Each one corresponds to a
defect the split actually shipped: the two experiment lists disagreed on the
first commit, and a hand-written stub introduced ``--network_type`` -- a flag
argparse rejects -- into the file every session reads.
"""

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ROUTER = _ROOT / "CLAUDE.md"

# Claude Code's limit. The router is expected to grow; this fails while there is
# still room to act, rather than after the file has silently truncated.
_SIZE_LIMIT = 150_000

# The docs that exist because CLAUDE.md delegates to them. Not every doc needs a
# stub (docs/city_sampling.md is reached from README.md), but each of these was
# carved out of the router and is unreachable if its stub goes missing.
_SPLIT_OUT_DOCS = (
    "docs/capture-dates.md",
    "docs/catalog-backups.md",
    "docs/census.md",
    "docs/driving-plan.md",
    "docs/experiments/README.md",
    "docs/frontend.md",
    "docs/operations.md",
    "docs/provider-access.md",
    "docs/scheduler.md",
    "docs/street-coverage.md",
    "docs/testing.md",
)

_DOC_LINK_RE = re.compile(r"docs/[A-Za-z0-9_/-]+\.md")
_WRITEUP_RE = re.compile(r"`([a-z0-9-]+\.md)`")
# A long option, anchored so that a slug like ``saskatoon--sk_...`` is not read
# as one. All 42 argparse long options in this repo are hyphenated.
_UNDERSCORE_FLAG_RE = re.compile(r"(?:^|[\s`(])(--[a-z][a-z0-9]*(?:_[a-z0-9]+)+)")


def _docs_text():
    """The router plus every doc it can hand off to, as (path, text) pairs."""
    paths = [_ROUTER] + sorted(_ROOT.glob("docs/**/*.md"))
    return [(p.relative_to(_ROOT).as_posix(), p.read_text(encoding="utf-8")) for p in paths]


def test_claude_md_stays_under_claude_codes_size_limit():
    size = len(_ROUTER.read_text(encoding="utf-8"))
    assert size < _SIZE_LIMIT, (
        f"CLAUDE.md is {size:,} chars, over Claude Code's {_SIZE_LIMIT:,} limit, so it "
        f"truncates in every session. Move the evidence for a rule into its docs/ file and "
        f"leave the rule here -- that is what the split exists to do."
    )


def test_every_docs_link_in_claude_md_resolves():
    """A stub pointing at a file that does not exist is a dead end mid-session.

    CLAUDE.md linked docs/experiments/README.md for months before the file was
    written; that is the failure this pins.
    """
    dangling = sorted(
        {
            link
            for link in _DOC_LINK_RE.findall(_ROUTER.read_text(encoding="utf-8"))
            if not (_ROOT / link).is_file()
        }
    )
    assert not dangling, f"CLAUDE.md links docs that do not exist: {dangling}"


@pytest.mark.parametrize("doc", _SPLIT_OUT_DOCS)
def test_every_split_out_doc_is_reachable_from_the_router(doc):
    """The detail is only one link away if the router still carries the link."""
    assert (_ROOT / doc).is_file(), f"{doc} was split out of CLAUDE.md but is missing"
    assert doc in _ROUTER.read_text(encoding="utf-8"), (
        f"{doc} exists but CLAUDE.md never names it, so nothing in a session points at it. "
        f"Every split-out doc needs a stub carrying its rule."
    )


def test_the_router_and_the_experiments_readme_name_the_same_writeups():
    """Two lists of the same thing drift, and git cannot flag a stale one.

    They shipped out of sync on the split's first commit: the router named six
    writeups and docs/experiments/README.md named five, omitting
    kartaview-sweep-cost.md, while both said "keep the two in sync".
    """
    on_disk = {p.name for p in (_ROOT / "docs" / "experiments").glob("*.md")} - {"README.md"}
    in_router = set(_WRITEUP_RE.findall(_ROUTER.read_text(encoding="utf-8"))) & on_disk
    readme = _ROOT / "docs" / "experiments" / "README.md"
    in_readme = set(_WRITEUP_RE.findall(readme.read_text(encoding="utf-8"))) & on_disk

    assert in_router == on_disk, (
        f"CLAUDE.md does not name every writeup in docs/experiments/: "
        f"missing {sorted(on_disk - in_router)}"
    )
    assert in_readme == on_disk, (
        f"docs/experiments/README.md does not name every writeup beside it: "
        f"missing {sorted(on_disk - in_readme)}"
    )


def test_no_doc_writes_a_long_flag_with_an_underscore():
    """Every argparse long option in this repo is hyphenated, so an underscore
    flag in the prose is always a typo -- and one copied out of CLAUDE.md fails
    at the shell. The split introduced ``--network_type`` for ``--network-type``.
    """
    offenders = [
        (path, flag) for path, text in _docs_text() for flag in _UNDERSCORE_FLAG_RE.findall(text)
    ]
    assert not offenders, (
        f"long options are hyphenated, not underscored: "
        f"{sorted({f'{p}: {f}' for p, f in offenders})}"
    )
