"""
Pin the official-Google copyright rule across the Python/JS boundary.

The rule lives twice: ``analysis.GOOGLE_COPYRIGHT_RE`` (stats, walks, maps,
the #442 work lists) and ``GOOGLE_COPYRIGHT_RE`` in
``www/js/streetscape-utils.js`` (the city page's Google-only filter). On
2026-10-21 Google began writing ``© 2026 Google`` instead of ``© Google``;
the exact-string check in both places then classified every pano as a
contributor's for three weeks, zeroing every GSV road walk and every grid
run's official counts and capture-date statistics, with nothing failing.
Widening one copy and not the other would reopen half of that silently, so
the JS pattern is read out of its source and held to the Python one.

``[0-9]`` rather than ``\\d`` in both, so the rule does not depend on the
regex engine: JS's ``\\d`` is ASCII-only, Python's ``re`` matches any Unicode
digit, and pandas' pyarrow-backed strings run RE2, ASCII-only again. Under
pyarrow strings the corpus test cannot see a ``\\d`` regression in Python;
the pattern-equality test is what catches it.
"""

import re
from pathlib import Path

import pandas as pd

from streetscape_metadata_tracker.analysis import GOOGLE_COPYRIGHT_RE, is_google_copyright

JS_SOURCE = Path(__file__).resolve().parent.parent / "www" / "js" / "streetscape-utils.js"

# Every official form seen on prod, and real contributor strings that a looser
# rule would misread (all four "Google" names were measured in 2026-10/11 runs).
CORPUS = {
    "© Google": True,
    "© 2026 Google": True,
    "© MIB 2023 - Google Virtual Tours Agency": False,
    "© MIB 360 - Google Virtual Tours Agency": False,
    "© IWorkForGoogleForFree": False,
    "© A Google User": False,
    "© Lucerna Google": False,
    "© Google Maps Developer David Ramsey": False,
    "© ٢٠٢٦ Google": False,  # Arabic-Indic digits: JS \\d would refuse them
    "© 2026 Google ": False,
    "© google": False,
    "": False,
}


def _js_pattern() -> str:
    m = re.search(r"const GOOGLE_COPYRIGHT_RE = /\^(.+)\$/;", JS_SOURCE.read_text(encoding="utf-8"))
    assert m, "GOOGLE_COPYRIGHT_RE not found in streetscape-utils.js (renamed or reshaped?)"
    return m.group(1)


def test_js_pattern_is_the_python_pattern():
    assert _js_pattern() == GOOGLE_COPYRIGHT_RE


def test_python_classifies_the_corpus():
    got = is_google_copyright(pd.Series(list(CORPUS))).tolist()
    assert got == list(CORPUS.values())


def test_js_pattern_text_classifies_the_corpus():
    # Runs the JS pattern TEXT through Python's engine; with only literals,
    # a group, a quantifier and [0-9] the two engines read it identically.
    js = re.compile(_js_pattern())
    assert [bool(js.fullmatch(s)) for s in CORPUS] == list(CORPUS.values())


def test_nan_and_index_are_preserved():
    s = pd.Series(["© 2026 Google", None, float("nan")], index=[7, 8, 9])
    out = is_google_copyright(s)
    assert out.tolist() == [True, False, False]
    assert out.index.tolist() == [7, 8, 9]
    assert out.dtype == bool
