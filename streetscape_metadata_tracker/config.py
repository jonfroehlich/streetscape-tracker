import logging
import os
import stat
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Standard metadata schema — the shared core written by BOTH providers.
# GSV's documented metadata endpoint returns only copyright/date/location/
# pano_id/status (audited: nothing else free to capture), so this 9-column
# core is GSV-complete. Mapillary carries a superset (see below).
METADATA_DTYPES = {
    "query_lat": np.float64,
    "query_lon": np.float64,
    "query_timestamp": str,  # initially read as a str; stored in (ISO 8601 with timezone)
    "pano_lat": pd.Float64Dtype(),  # nullable float
    "pano_lon": pd.Float64Dtype(),  # nullable float
    "pano_id": pd.StringDtype(),  # nullable string
    # Read as a str, then parsed by fileutils.load_city_csv_file. Runs collected
    # since 2026 are written YYYY-MM-DD (standardize_capture_date), but the
    # legacy pre-2026 runs on disk carry MONTH precision and are never
    # rewritten, so the on-disk contract is "any ISO 8601 date precision", not
    # YYYY-MM-DD. Saying otherwise here is what produced issue #226: the loader
    # parsed with a strict '%Y-%m-%d' and NaT'd every date in those files.
    "capture_date": str,
    "copyright_info": pd.StringDtype(),  # nullable string
    "status": str,  # status is never null
}

# Mapillary-only columns appended after the core (issue: capture all free tile
# metadata). Every one of these comes, for zero extra requests, from the z14
# `image` layer the tile downloader already fetches — Mapillary publishes far
# more per-image metadata than GSV's metadata endpoint exposes. Kept OUT of the
# shared METADATA_DTYPES (like the history harvester's HISTORY_DTYPES) so the
# GSV path and files are untouched. Readers coerce these via
# MAPILLARY_METADATA_DTYPES below; pandas ignores dtype keys absent from a file,
# so GSV runs and pre-change Mapillary files load unchanged.
#   - organization_id: identifies systematic city-wide capture programs
#     (municipal fleets, ridesharing scooter sweeps); null == individual.
#   - on_foot: pedestrian vs vehicle capture (tile field is named `foot`).
#   - quality_score: 0-1, for screening blurry sequences.
#   - compass_angle: capture bearing (for a future bearing check).
#   - sequence_id: groups images into one capture drive.
#   - creator_id: contributor id, also embedded in copyright_info for parity.
MAPILLARY_EXTRA_DTYPES = {
    "creator_id": pd.StringDtype(),  # nullable string (large int id kept as string)
    "organization_id": pd.StringDtype(),  # nullable string; null for individual contributors
    "sequence_id": pd.StringDtype(),  # nullable string
    "is_pano": pd.BooleanDtype(),  # nullable bool (null on ZERO_RESULTS rows)
    "on_foot": pd.BooleanDtype(),  # nullable bool (tile prop `foot`)
    "quality_score": pd.Float64Dtype(),  # nullable float, 0-1
    "compass_angle": pd.Float64Dtype(),  # nullable float, degrees
}

# The full Mapillary run schema: shared core + Mapillary extras.
MAPILLARY_METADATA_DTYPES = {**METADATA_DTYPES, **MAPILLARY_EXTRA_DTYPES}

# KartaView-only columns, same posture as the Mapillary block above: every one
# of these rides on the bulk `nearby-photos` row the sweep already fetched, so
# capturing them costs nothing and re-fetching them later would cost a full
# census (issue #225).
#   - username: contributor, also embedded in copyright_info for parity. The
#     imagery is CC BY-SA 4.0, so that string is an attribution requirement
#     rather than the drive-vs-photosphere filter it is for GSV.
#   - sequence_id / sequence_index: the drive and the position in it. KartaView
#     publishes a drive id, so pano-spacing analysis can group by drive before
#     measuring — the correction that is impossible for GSV (pano-spacing.md).
#   - is_pano: projection == "SPHERE"; PLANE is flat dashcam imagery (#116).
#   - field_of_view: published beside the projection, not used to derive it.
#   - compass_angle: KartaView's `heading`, named for the Mapillary column that
#     measures the same thing so one bearing check can read both (#97).
#   - date_added: server-side upload time. Published because capture_date may be
#     null BECAUSE of it — see download_kartaview.shot_date_to_iso_date — so
#     dropping it would destroy the provenance of the date rule.
#   - org_code: publisher code (e.g. "CMNT" for a community upload), which is
#     what separates a Grab fleet ingest from an individual contributor.
#   - way_id: the OSM way KartaView snapped the photo to.
KARTAVIEW_EXTRA_DTYPES = {
    "username": pd.StringDtype(),  # nullable string
    "sequence_id": pd.StringDtype(),  # nullable string
    "sequence_index": pd.Int64Dtype(),  # nullable int; position within the drive
    "is_pano": pd.BooleanDtype(),  # nullable bool (null on ZERO_RESULTS rows)
    "field_of_view": pd.Float64Dtype(),  # nullable float, degrees
    "compass_angle": pd.Float64Dtype(),  # nullable float, degrees
    "date_added": pd.StringDtype(),  # nullable string; upload time, NOT a capture date
    "org_code": pd.StringDtype(),  # nullable string
    "way_id": pd.StringDtype(),  # nullable string
}

# The full KartaView run schema: shared core + KartaView extras.
KARTAVIEW_METADATA_DTYPES = {**METADATA_DTYPES, **KARTAVIEW_EXTRA_DTYPES}

# Provider token (as it appears in a run filename) -> that provider's run
# schema. The read side of the naming contract: a run CSV is only self-
# describing through its filename, and pandas INFERS any column a dtype
# mapping omits -- so reading a census provider's run with another's schema
# silently turns a nullable Int64 index into float64 and a numeric-looking
# string id into a float. fileutils.dtypes_for_run_path is the lookup; keep a
# provider here in step with naming.KNOWN_PROVIDERS or its runs read as gsv.
PROVIDER_RUN_DTYPES = {
    "gsv": METADATA_DTYPES,
    "mapillary": MAPILLARY_METADATA_DTYPES,
    # UNREACHABLE UNTIL naming.KNOWN_PROVIDERS GAINS "kartaview" (phase 3b of
    # issue #225). Both filename regexes gate on that tuple -- parse_filename
    # rejects an unknown token outright and _STREETWALK_FILENAME_RE builds its
    # provider alternation from it -- so dtypes_for_run_path cannot reach this
    # entry today and a KartaView run would fall through to the Mapillary
    # default: sequence_index inferred to float64, way_id inferred to a float
    # that eats its leading zeros. Harmless only because nothing writes a
    # KartaView run yet; test_a_run_schema_is_reachable_from_a_filename is the
    # strict-xfail that goes red the moment the token lands, so the two cannot
    # be brought into step in the wrong order.
    "kartaview": KARTAVIEW_METADATA_DTYPES,
}


def warn_if_credentials_world_readable(env_path: str) -> bool:
    """
    Warn when the ``.env`` credential file is readable by group or others.

    The ``.env`` carries a billable Google API key; on shared lab storage
    (group-readable NFS, see deploy/README.md) a default 0644 exposes it to
    every group member. This only warns — the deploy docs require
    ``chmod 600 .env``.

    Args:
        env_path: Path to the loaded .env file ("" or missing → no-op).

    Returns:
        True iff a warning was logged (mode had any group/other bits set).
    """
    try:
        mode = stat.S_IMODE(os.stat(env_path).st_mode)
    except OSError:
        return False
    if mode & 0o077:
        logger.warning(
            "Credential file %s is readable by other users (mode %03o). "
            "Restrict it with: chmod 600 %s",
            env_path,
            mode,
            env_path,
        )
        return True
    return False


def load_config(provider: str = "gsv") -> dict[str, Any]:
    """
    Load the API credential for the given provider from the environment.

    gsv requires GMAPS_API_KEY; mapillary requires MAPILLARY_ACCESS_TOKEN.
    Only the requested provider's credential is required, so a machine can
    run one provider without the other's key.

    'gsv_streets' and 'mapillary_streets' are ISOLATED credential channels for
    street-coverage collection (issue #99): separate keys so street-sampling
    experiments can't exhaust the production grid collector's quota, metered
    under their own api_usage ledger rows. Dormant for now — nothing calls
    them until the road-walk collector (#99) / --streets pipeline flag (#100)
    land. These are credential channels, not filename provider tokens
    (naming.KNOWN_PROVIDERS is unchanged).
    """
    if provider == "gsv":
        config = {
            "api_key": os.environ.get("GMAPS_API_KEY"),
        }
        if not config["api_key"]:
            raise ValueError(
                "GMAPS_API_KEY not found in environment variables.\n\n"
                "Option 1: Create a .env file in your project root:\n"
                "  GMAPS_API_KEY=YOUR_API_KEY\n\n"
                "Option 2: Set it as an environment variable:\n"
                "  macOS/Linux:\n"
                "    > export GMAPS_API_KEY=YOUR_API_KEY\n"
                "  Windows (Command Prompt):\n"
                "    > set GMAPS_API_KEY=YOUR_API_KEY\n"
                "  Windows (PowerShell):\n"
                "    > $env:GMAPS_API_KEY='YOUR_API_KEY'\n\n"
                "If you do not have a Google Maps API key, you can create one at "
                "https://console.cloud.google.com/apis/credentials\n"
                "You will need to enable the Street View Static API for the key."
            )
        return config

    if provider == "mapillary":
        config = {
            "access_token": os.environ.get("MAPILLARY_ACCESS_TOKEN"),
        }
        if not config["access_token"]:
            raise ValueError(
                "MAPILLARY_ACCESS_TOKEN not found in environment variables.\n\n"
                "Option 1: Add it to the .env file in your project root:\n"
                "  MAPILLARY_ACCESS_TOKEN=MLY|YOUR|TOKEN\n\n"
                "Option 2: Set it as an environment variable:\n"
                "  macOS/Linux:\n"
                "    > export MAPILLARY_ACCESS_TOKEN='MLY|YOUR|TOKEN'\n\n"
                "Create a (free) client token by registering an application at "
                "https://www.mapillary.com/dashboard/developers"
            )
        return config

    if provider == "kartaview":
        config = {
            "access_token": os.environ.get("KARTAVIEW_ACCESS_TOKEN"),
        }
        if not config["access_token"]:
            raise ValueError(
                "KARTAVIEW_ACCESS_TOKEN not found in environment variables.\n\n"
                "KartaView serves anonymous clients at 100 requests/hour and "
                "authenticated ones at 1,000 (issue #225). The token is required "
                "rather than optional because the anonymous rate is not a slower "
                "channel, it is no channel: a p95 city is ~636 requests (6.4 hours) "
                "and Singapore ~9,974 (100 hours), against a nightly batch.\n\n"
                "Add it to the .env file in your project root:\n"
                "  KARTAVIEW_ACCESS_TOKEN=YOUR_TOKEN\n\n"
                "Sign in at https://kartaview.org with Google or Facebook and read "
                "the token from the session. NOTE: their OpenStreetMap login has "
                "been broken since 2024-06 (they call OSM's OAuth2 endpoint with "
                "OAuth1 parameters), so OSM is not a usable sign-in route — see "
                "https://github.com/kartaview/openstreetcam.org/issues/404"
            )
        return config

    if provider == "gsv_streets":
        config = {
            "api_key": os.environ.get("GMAPS_STREETS_API_KEY"),
        }
        if not config["api_key"]:
            raise ValueError(
                "GMAPS_STREETS_API_KEY not found in environment variables.\n\n"
                "Street-coverage collection uses its own Google API key so it "
                "can't exhaust the production grid collector's quota (issue #99).\n\n"
                "Add it to the .env file in your project root:\n"
                "  GMAPS_STREETS_API_KEY=YOUR_API_KEY\n\n"
                "Create a separate key at "
                "https://console.cloud.google.com/apis/credentials\n"
                "You will need to enable the Street View Static API for the key."
            )
        return config

    if provider == "mapillary_streets":
        config = {
            "access_token": os.environ.get("MAPILLARY_STREETS_ACCESS_TOKEN"),
        }
        if not config["access_token"]:
            raise ValueError(
                "MAPILLARY_STREETS_ACCESS_TOKEN not found in environment variables.\n\n"
                "Street-coverage work uses its own Mapillary token for rate-limit "
                "hygiene, separate from the tile-census collector (issue #99).\n\n"
                "Add it to the .env file in your project root:\n"
                "  MAPILLARY_STREETS_ACCESS_TOKEN=MLY|YOUR|TOKEN\n\n"
                "Create a (free) client token by registering an application at "
                "https://www.mapillary.com/dashboard/developers"
            )
        return config

    raise ValueError(
        f"Unknown provider {provider!r} (known: gsv, mapillary, gsv_streets, mapillary_streets)"
    )
