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
    "capture_date": str,  # initially read as a str; stored in ISO 8601 format (YYYY-MM-DD)
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
