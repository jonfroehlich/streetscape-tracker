"""
Filename conventions for GSV metadata files.

This module is the single source of truth for generating and parsing the
data filenames used throughout the project. Three filename generations
exist on disk and all must parse:

1. Legacy (undated):        seattle--wa_width_1000_height_1000_step_20.csv.gz
2. Legacy (buggy float):    seattle--wa_width_1000_height_1000_step_20.0.csv.gz
3. Dated runs (current):    seattle--washington--united-states_width_1000_height_1000_step_20_2026-07-02.csv.gz
4. Non-GSV provider runs:   seattle--washington--united-states_width_1000_height_1000_step_20_mapillary_2026-07-02.csv.gz

New files use form 3 (GSV) or 4 (other providers): integer dimensions, an
optional provider token, and an ISO run date. The absence of a provider
token always means GSV, so every pre-existing filename and published URL
stays valid unchanged.
"""

import os
import re
from dataclasses import dataclass
from datetime import date

# Providers with a filename token. GSV files carry no token (legacy compat),
# so 'gsv' never appears in filenames but is the parse default.
DEFAULT_PROVIDER = "gsv"
KNOWN_PROVIDERS = ("gsv", "mapillary")

# Accepts int or float numeric groups, an optional provider token, and an
# optional trailing ISO run date. The groups can't bleed into each other:
# step is numeric, provider alphabetic, date digits-and-dashes.
FILENAME_RE = re.compile(
    r"^(?P<slug>.+?)"
    r"_width_(?P<w>\d+(?:\.\d+)?)"
    r"_height_(?P<h>\d+(?:\.\d+)?)"
    r"_step_(?P<s>\d+(?:\.\d+)?)"
    r"(?:_(?P<provider>[a-z]+))?"
    r"(?:_(?P<date>\d{4}-\d{2}-\d{2}))?$"
)

# Extensions stripped before parsing, longest first.
_KNOWN_EXTENSIONS = (".csv.gz", ".json.gz", ".csv", ".json", ".html")


@dataclass(frozen=True)
class ParsedFilename:
    """Components extracted from a GSV metadata filename."""

    slug: str  # sanitized location slug, e.g. 'grand-marais--mn'
    city_query_str: str  # human-readable reconstruction, e.g. 'Grand Marais, MN'
    width_meters: int
    height_meters: int
    step_meters: int
    run_date: date | None  # None for legacy undated files
    provider: str = DEFAULT_PROVIDER  # 'gsv' when no token in the filename


def sanitize_city_query_str(city_query_str: str) -> str:
    r"""
    Sanitize a city query string for use in filenames.

    Uses single dash (-) for spaces within location components and
    double dash (--) to separate location components (city, state, country).

    Handles problematic characters across Windows, macOS, and Linux:
    - Replaces spaces with single dashes
    - Uses double dashes to separate location components (e.g., city--state--country)
    - Removes characters that are invalid on Windows (< > : " / \ | ? *)
    - Removes any leading/trailing periods
    - Converts to lowercase

    Args:
        city_query_str: Query string that may contain city, state, and/or country.

    Returns:
        Sanitized string safe for filenames

    Examples:
        >>> sanitize_city_query_str("St. Louis, MO, USA")
        'st.-louis--mo--usa'
        >>> sanitize_city_query_str("Grand Marais, MN")
        'grand-marais--mn'
        >>> sanitize_city_query_str("Grand Marais")
        'grand-marais'

    Note: interior periods are preserved (only leading/trailing ones are
    stripped) — this matches the slugs of all previously collected data
    files, so it must not change.
    """
    parts = [p.strip() for p in city_query_str.split(",")]

    cleaned_parts = []
    for part in parts:
        # \s (not ' ') so Unicode whitespace like the non-breaking spaces
        # Nominatim sometimes returns (e.g. "Ann\xa0Arbor") is normalized too
        cleaned = re.sub(r"\s", "-", part)
        cleaned = re.sub(r'[<>:"/\\|?*]', "", cleaned)
        cleaned = cleaned.strip(".-")
        cleaned = cleaned.lower()
        cleaned_parts.append(cleaned)

    return "--".join(cleaned_parts)


def slug_to_query_str(slug: str) -> str:
    """
    Reconstruct a human-readable query string from a sanitized slug.

    >>> slug_to_query_str("grand-marais--mn--usa")
    'Grand Marais, Mn, Usa'
    """
    processed_parts = []
    for part in slug.split("--"):
        words = part.split("-")
        processed_parts.append(" ".join(word.capitalize() for word in words))
    return ", ".join(processed_parts)


def parse_filename(filename: str) -> ParsedFilename:
    """
    Parse a GSV metadata filename to extract its parameters.

    Accepts all filename generations: legacy undated names with integer or
    float-formatted numbers (an old bug wrote `_step_20.0`), current dated
    names with a trailing `_YYYY-MM-DD` run date, and provider-tagged names
    with a provider token before the date (no token = GSV).

    Args:
        filename: Name or path of the data file (any known extension)

    Returns:
        ParsedFilename with slug, reconstructed query string, integer
        dimensions, run_date (None for legacy undated files), and provider
        ('gsv' unless a provider token is present).

    Raises:
        ValueError: If the filename doesn't match the expected format or
            carries an unknown provider token

    Examples:
        >>> p = parse_filename("grand-marais--mn_width_1000_height_1000_step_20.csv.gz")
        >>> (p.city_query_str, p.width_meters, p.run_date, p.provider)
        ('Grand Marais, Mn', 1000, None, 'gsv')
        >>> p = parse_filename("bend--or_width_5000_height_5000_step_20.0_2026-07-02.csv.gz")
        >>> (p.step_meters, p.run_date.isoformat())
        (20, '2026-07-02')
        >>> p = parse_filename("bend--or_width_5000_height_5000_step_20_mapillary_2026-07-02.csv.gz")
        >>> (p.provider, p.run_date.isoformat())
        ('mapillary', '2026-07-02')
    """
    base = os.path.basename(filename)
    for ext in _KNOWN_EXTENSIONS:
        if base.endswith(ext):
            base = base[: -len(ext)]
            break

    match = FILENAME_RE.match(base)
    if not match:
        raise ValueError(f"Filename {filename} doesn't match expected format")

    provider = match.group("provider") or DEFAULT_PROVIDER
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(
            f"Filename {filename} has unknown provider token {provider!r} "
            f"(known: {', '.join(KNOWN_PROVIDERS)})"
        )

    run_date = None
    if match.group("date"):
        run_date = date.fromisoformat(match.group("date"))

    slug = match.group("slug")
    return ParsedFilename(
        slug=slug,
        city_query_str=slug_to_query_str(slug),
        width_meters=int(float(match.group("w"))),
        height_meters=int(float(match.group("h"))),
        step_meters=int(float(match.group("s"))),
        run_date=run_date,
        provider=provider,
    )


def generate_base_filename(
    city_query_str: str, grid_width: float, grid_height: float, step_length: float
) -> str:
    """
    Generate a legacy (undated) base filename for GSV metadata files.

    Used only for locating pre-existing undated files; new downloads should
    use generate_run_filename() which appends the run date.

    Examples:
        >>> generate_base_filename("St. Louis, MO, USA", 1000, 1000, 20)
        'st.-louis--mo--usa_width_1000_height_1000_step_20'
    """
    safe_name = sanitize_city_query_str(city_query_str)
    return f"{safe_name}_width_{int(grid_width)}_height_{int(grid_height)}_step_{int(step_length)}"


def generate_run_filename(
    city_id: str,
    grid_width: float,
    grid_height: float,
    step_length: float,
    run_date: date,
    provider: str = DEFAULT_PROVIDER,
) -> str:
    """
    Generate the dated base filename (no extension) for a collection run.

    Args:
        city_id: canonical sanitized city slug (see db.register_city)
        grid_width/grid_height/step_length: grid geometry in meters
        run_date: the run's date, embedded as an ISO suffix
        provider: imagery provider; 'gsv' emits no token so GSV filenames
            match the pre-provider convention exactly

    Examples:
        >>> from datetime import date
        >>> generate_run_filename("bend--oregon--united-states", 5000, 5000, 20, date(2026, 7, 2))
        'bend--oregon--united-states_width_5000_height_5000_step_20_2026-07-02'
        >>> generate_run_filename("bend--oregon--united-states", 5000, 5000, 20, date(2026, 7, 2), provider='mapillary')
        'bend--oregon--united-states_width_5000_height_5000_step_20_mapillary_2026-07-02'
    """
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (known: {', '.join(KNOWN_PROVIDERS)})")
    provider_token = "" if provider == DEFAULT_PROVIDER else f"_{provider}"
    return (
        f"{city_id}_width_{int(grid_width)}_height_{int(grid_height)}"
        f"_step_{int(step_length)}{provider_token}_{run_date.isoformat()}"
    )


# ── Historical-dates harvest files (issue #2) ──────────────────────────────
#
# The historical-dates harvester (download_gsv_history) writes a DIFFERENT
# artifact from a normal run: a census of every official Google panorama it
# could surface in the city, each with its capture date, harvested in one pass
# from an unpublished endpoint that may change or stop working at any time. It
# is NOT a provider run, so it deliberately does NOT go through
# generate_run_filename / the FILENAME_RE run-file contract. It carries its own
# '_gsv_history_' marker so it can never be confused with a sampled run, and so
# parse_filename() rejects it (callers already treat a ValueError as "not a run
# file"). Published as a normal *.csv.gz, so sync picks it up unchanged.

HISTORY_MARKER = "gsv_history"

_HISTORY_FILENAME_RE = re.compile(
    r"^(?P<slug>.+?)"
    r"_width_(?P<w>\d+)"
    r"_height_(?P<h>\d+)"
    r"_step_(?P<s>\d+)"
    r"_" + HISTORY_MARKER + r"_"
    r"(?P<date>\d{4}-\d{2}-\d{2})$"
)


@dataclass(frozen=True)
class ParsedHistoryFilename:
    """Components extracted from a historical-dates harvest filename."""

    slug: str
    city_query_str: str
    width_meters: int
    height_meters: int
    step_meters: int
    harvest_date: date


def generate_history_filename(
    city_id: str,
    grid_width: float,
    grid_height: float,
    step_length: float,
    harvest_date: date,
) -> str:
    """
    Base filename (no extension) for a historical-dates harvest.

    Example:
        >>> from datetime import date
        >>> generate_history_filename("bend--oregon--united-states", 5000, 5000, 20, date(2026, 7, 8))
        'bend--oregon--united-states_width_5000_height_5000_step_20_gsv_history_2026-07-08'
    """
    return (
        f"{city_id}_width_{int(grid_width)}_height_{int(grid_height)}"
        f"_step_{int(step_length)}_{HISTORY_MARKER}_{harvest_date.isoformat()}"
    )


def parse_history_filename(filename: str) -> ParsedHistoryFilename:
    """
    Parse a historical-dates harvest filename.

    Raises ValueError if the name is not a history file (including normal run
    files, which never carry the '_gsv_history_' marker).

    Example:
        >>> p = parse_history_filename("bend--or_width_5000_height_5000_step_20_gsv_history_2026-07-08.csv.gz")
        >>> (p.width_meters, p.harvest_date.isoformat())
        (5000, '2026-07-08')
    """
    base = os.path.basename(filename)
    for ext in _KNOWN_EXTENSIONS:
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    match = _HISTORY_FILENAME_RE.match(base)
    if not match:
        raise ValueError(f"Filename {filename} is not a {HISTORY_MARKER} harvest file")
    slug = match.group("slug")
    return ParsedHistoryFilename(
        slug=slug,
        city_query_str=slug_to_query_str(slug),
        width_meters=int(match.group("w")),
        height_meters=int(match.group("h")),
        step_meters=int(match.group("s")),
        harvest_date=date.fromisoformat(match.group("date")),
    )


# ── Street-coverage artifacts (issues #24/#103) ────────────────────────────

STREETS_SUFFIX = "_streets"


def streets_filename_for_run(csv_filename: str) -> str:
    """
    Name of the street-coverage artifact derived from a run's csv.gz.

    The artifact is a sibling '{run stem}_streets.json.gz' GeoJSON written by
    streetscape_street_analyzer; the trailing '_streets' token guarantees
    parse_filename() rejects it (a ValueError callers already treat as "not a
    run file"), same contract as history files. There is deliberately no
    parse_streets_filename yet — nothing needs to recognize the artifact on
    disk until the aggregate surfaces street coverage (issue #102). The web
    frontend derives the same name in street-coverage.js (streetsUrlForDataFile);
    keep the two in sync.

    Example:
        >>> streets_filename_for_run("bend--or_width_5000_height_5000_step_20_2026-07-08.csv.gz")
        'bend--or_width_5000_height_5000_step_20_2026-07-08_streets.json.gz'
    """
    if not csv_filename.endswith(".csv.gz"):
        raise ValueError(f"Not a run csv.gz filename: {csv_filename}")
    return csv_filename[: -len(".csv.gz")] + STREETS_SUFFIX + ".json.gz"


# ── Road-walk street-coverage collection (issue #99) ───────────────────────
#
# The road-walk collector (streetscape_street_analyzer.collect) is a SECOND
# collection modality: instead of a grid lattice it samples on-street points
# every `spacing` metres along each frozen OSM edge and queries GSV per point.
# Its raw snapshot is a normal METADATA_DTYPES csv.gz (one row per sampled
# on-street location), but it carries a '_streetwalk_' marker + the walk spacing
# so it can never be confused with a grid run: parse_filename() rejects it (the
# marker and the trailing '_sp{N}_' leave nothing a run name can match), exactly
# like the history/streets contracts. The derived per-edge coverage GeoJSON is a
# sibling '..._coverage.json.gz'.
#
# A walk is per (city, provider, date) — both providers walk the SAME sample
# points, so both can be collected the same night — and the artifacts therefore
# carry a provider token on exactly the run-filename convention: it sits after
# '_step_{S}', and gsv emits none, so every GSV walk name ever published is
# unchanged.
#
#   gsv:       {city}_width_W_height_H_step_S_streetwalk_sp15_{DATE}.csv.gz
#   mapillary: {city}_width_W_height_H_step_S_mapillary_streetwalk_sp15_{DATE}.csv.gz

STREETWALK_MARKER = "streetwalk"

# Spelled as an explicit alternation of the tokenized providers rather than
# FILENAME_RE's `[a-z]+`: a wildcard here would first try to swallow the literal
# 'streetwalk' marker and only recover by backtracking, which is needlessly
# subtle for a naming contract.
_STREETWALK_PROVIDER_ALT = "|".join(p for p in KNOWN_PROVIDERS if p != DEFAULT_PROVIDER)

_STREETWALK_FILENAME_RE = re.compile(
    r"^(?P<slug>.+?)"
    r"_width_(?P<w>\d+)"
    r"_height_(?P<h>\d+)"
    r"_step_(?P<s>\d+)"
    rf"(?:_(?P<provider>{_STREETWALK_PROVIDER_ALT}))?"
    r"_" + STREETWALK_MARKER + r"_sp(?P<spacing>\d+)_"
    r"(?P<date>\d{4}-\d{2}-\d{2})$"
)


@dataclass(frozen=True)
class ParsedStreetwalkFilename:
    """Components extracted from a road-walk collection filename."""

    slug: str
    city_query_str: str
    width_meters: int
    height_meters: int
    step_meters: int
    spacing_meters: int
    run_date: date
    provider: str = DEFAULT_PROVIDER  # 'gsv' when no token in the filename


def generate_streetwalk_filename(
    city_id: str,
    grid_width: float,
    grid_height: float,
    step_length: float,
    spacing_m: float,
    run_date: date,
    provider: str = DEFAULT_PROVIDER,
) -> str:
    """
    Base filename (no extension) for a road-walk collection snapshot.

    The grid ``width/height/step`` identify the city's frozen geometry (and thus
    its frozen OSM network, whose bbox is derived from that geometry); ``sp{N}``
    is the along-edge sample spacing in metres — the road-walk analogue of the
    grid step.

    Args:
        city_id: canonical sanitized city slug (see db.register_city)
        grid_width/grid_height/step_length: frozen grid geometry in metres
        spacing_m: along-edge sample spacing in metres
        run_date: the walk's date, embedded as an ISO suffix
        provider: imagery provider walked. Both providers walk the same sample
            points, so both can be collected on the same night — the token is
            what keeps their artifacts apart. 'gsv' emits no token, so GSV walk
            filenames match the pre-provider convention exactly.

    Examples:
        >>> from datetime import date
        >>> generate_streetwalk_filename("bend--oregon--united-states", 5000, 5000, 20, 15, date(2026, 7, 8))
        'bend--oregon--united-states_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08'
        >>> generate_streetwalk_filename("bend--or", 5000, 5000, 20, 15, date(2026, 7, 8), provider='mapillary')
        'bend--or_width_5000_height_5000_step_20_mapillary_streetwalk_sp15_2026-07-08'
    """
    if provider not in KNOWN_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (known: {', '.join(KNOWN_PROVIDERS)})")
    provider_token = "" if provider == DEFAULT_PROVIDER else f"_{provider}"
    return (
        f"{city_id}_width_{int(grid_width)}_height_{int(grid_height)}"
        f"_step_{int(step_length)}{provider_token}"
        f"_{STREETWALK_MARKER}_sp{int(spacing_m)}_{run_date.isoformat()}"
    )


def parse_streetwalk_filename(filename: str) -> ParsedStreetwalkFilename:
    """
    Parse a road-walk collection filename.

    Raises ValueError if the name is not a streetwalk file (including normal run
    files, which never carry the '_streetwalk_' marker). A name with no provider
    token is GSV, as everywhere else in the naming contract.

    Examples:
        >>> p = parse_streetwalk_filename("bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz")
        >>> (p.step_meters, p.spacing_meters, p.run_date.isoformat(), p.provider)
        (20, 15, '2026-07-08', 'gsv')
        >>> p = parse_streetwalk_filename("bend--or_width_5000_height_5000_step_20_mapillary_streetwalk_sp15_2026-07-08.csv.gz")
        >>> (p.slug, p.provider)
        ('bend--or', 'mapillary')
    """
    base = os.path.basename(filename)
    for ext in _KNOWN_EXTENSIONS:
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    match = _STREETWALK_FILENAME_RE.match(base)
    if not match:
        raise ValueError(f"Filename {filename} is not a {STREETWALK_MARKER} collection file")
    slug = match.group("slug")
    return ParsedStreetwalkFilename(
        slug=slug,
        city_query_str=slug_to_query_str(slug),
        width_meters=int(match.group("w")),
        height_meters=int(match.group("h")),
        step_meters=int(match.group("s")),
        spacing_meters=int(match.group("spacing")),
        run_date=date.fromisoformat(match.group("date")),
        provider=match.group("provider") or DEFAULT_PROVIDER,
    )


def streetwalk_coverage_filename(csv_filename: str) -> str:
    """
    Name of the per-edge coverage GeoJSON derived from a road-walk csv.gz.

    The artifact is a sibling '{streetwalk stem}_coverage.json.gz' written by
    the collector; the trailing '_coverage' plus the '_streetwalk_' marker keep
    parse_filename() rejecting it (a ValueError callers already treat as "not a
    run file").

    Example:
        >>> streetwalk_coverage_filename("bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz")
        'bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08_coverage.json.gz'
    """
    if not csv_filename.endswith(".csv.gz"):
        raise ValueError(f"Not a streetwalk csv.gz filename: {csv_filename}")
    return csv_filename[: -len(".csv.gz")] + "_coverage.json.gz"


def same_grid_geometry(filename_a: str, filename_b: str) -> bool:
    """
    True when both filenames parse and encode the same grid geometry
    (width, height, step). Provider token and run date are ignored.
    Unparseable filenames compare unequal, which callers treat as
    "don't diff" — the safe answer.

    Examples:
        >>> same_grid_geometry(
        ...     "seattle--wa_width_5000_height_5000_step_20_2026-07-02.csv.gz",
        ...     "seattle--wa_width_5000_height_5000_step_20_2026-04-01.csv.gz")
        True
        >>> same_grid_geometry(
        ...     "seattle--wa_width_5000_height_5000_step_20_2026-07-02.csv.gz",
        ...     "seattle--wa_width_1000_height_1000_step_30_2023-11-05.csv.gz")
        False
    """
    try:
        a = parse_filename(filename_a)
        b = parse_filename(filename_b)
    except ValueError:
        return False
    return (a.width_meters, a.height_meters, a.step_meters) == (
        b.width_meters,
        b.height_meters,
        b.step_meters,
    )
