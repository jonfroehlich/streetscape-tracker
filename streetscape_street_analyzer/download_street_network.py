"""
Fetch the frozen OSM street network for a city (issues #24/#103).

The network is fetched for the city's **frozen grid bounding box** (never
re-geocoded), so the streets line up exactly with the sampled grid the pano
runs use. Like frozen grid geometry, the network is a provider-agnostic city
asset (issue #103): fetched once, frozen to an unpublished GraphML cache under
``data/osm_cache/``, registered in the catalog's ``street_networks`` table,
and reused until ``--refresh`` (which replaces both the file and the catalog
row). GraphML (not GeoJSON) is used for the cache because it round-trips
osmnx's list-valued tags and is skipped by the publish whitelist (which only
ships ``*.csv.gz`` / ``*.json.gz``).
"""

from __future__ import annotations

import logging
import os
import sqlite3

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.db import CityRow
from streetscape_metadata_tracker.download_mapillary import grid_bbox

logger = logging.getLogger(__name__)

# Quiet by default; the CLI configures the root logger. use_cache also stores
# Overpass responses so re-fetches during development are cheap.
ox.settings.log_console = False
ox.settings.use_cache = True
ox.settings.timeout = 60

NETWORK_TYPE = "drive"


def _cache_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "osm_cache")


def network_cache_filename(city_id: str, network_type: str = NETWORK_TYPE) -> str:
    """
    GraphML basename for a city's frozen street network.

    The default 'drive' network keeps the original un-suffixed name so the
    caches (and catalog rows) predating network_type stay valid; other types
    (issue #99's 'walk'/'all') get an explicit suffix.
    """
    if network_type == NETWORK_TYPE:
        return f"{city_id}_streets_network.graphml"
    return f"{city_id}_streets_network_{network_type}.graphml"


def network_cache_path(city_id: str, data_dir: str, network_type: str = NETWORK_TYPE) -> str:
    """Unpublished GraphML path for a city's frozen street network."""
    return os.path.join(_cache_dir(data_dir), network_cache_filename(city_id, network_type))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def _download_graph(bbox, network_type: str) -> nx.MultiDiGraph:
    """Download a simplified drive network for the bbox, retrying on failure."""
    # osmnx 2.x expects bbox=(left, bottom, right, top) == (min_lon, min_lat,
    # max_lon, max_lat), exactly what grid_bbox returns.
    return ox.graph_from_bbox(
        bbox=bbox,
        network_type=network_type,
        simplify=True,
        retain_all=True,
        truncate_by_edge=True,
    )


def _register(
    conn: sqlite3.Connection, city_id: str, network_type: str, graph: nx.MultiDiGraph
) -> None:
    db.register_street_network(
        conn,
        city_id=city_id,
        graphml_filename=network_cache_filename(city_id, network_type),
        network_type=network_type,
        node_count=graph.number_of_nodes(),
        edge_count=graph.number_of_edges(),
        osmnx_version=ox.__version__,
    )


def fetch_graph(
    city_row: CityRow,
    data_dir: str,
    *,
    refresh: bool = False,
    network_type: str = NETWORK_TYPE,
    conn: sqlite3.Connection | None = None,
) -> nx.MultiDiGraph:
    """
    Return the city's street graph, from the frozen cache or Overpass.

    The bbox comes from the frozen grid geometry via `grid_bbox`, so the
    network matches the sampled area. When a cache exists and ``refresh`` is
    False it is loaded; otherwise the graph is downloaded and cached.

    When ``conn`` is given, the frozen network is registered in the catalog's
    ``street_networks`` table (issue #103): a fresh download registers (or, on
    ``refresh``, replaces) the row, and a cache hit whose row is missing is
    backfilled — this adopts GraphML caches created before the catalog table
    existed, so their ``fetched_at``/``osmnx_version`` reflect load time, not
    the original fetch. Without ``conn`` the module works standalone,
    catalog-free (unit tests, ad-hoc use).
    """
    # Keep osmnx's raw HTTP response cache inside the (unpublished) osm_cache
    # dir rather than a stray ./cache in the cwd.
    ox.settings.cache_folder = os.path.join(_cache_dir(data_dir), "osmnx")

    cache_path = network_cache_path(city_row.city_id, data_dir, network_type)
    if not refresh and os.path.exists(cache_path):
        logger.info("Loading frozen street network from %s", cache_path)
        graph = ox.load_graphml(cache_path)
        if conn is not None and db.get_street_network(conn, city_row.city_id, network_type) is None:
            logger.info("Backfilling street_networks catalog row for %s", city_row.city_id)
            _register(conn, city_row.city_id, network_type, graph)
        return graph

    bbox = grid_bbox(
        city_row.center_lat,
        city_row.center_lon,
        city_row.grid_width_m,
        city_row.grid_height_m,
        city_row.step_m,
    )
    logger.info(
        "Downloading OSM %s network for %s within frozen bbox %s",
        network_type,
        city_row.city_id,
        bbox,
    )
    graph = _download_graph(bbox, network_type)
    logger.info("Downloaded %d nodes / %d edges", graph.number_of_nodes(), graph.number_of_edges())

    os.makedirs(_cache_dir(data_dir), exist_ok=True)
    ox.save_graphml(graph, cache_path)
    logger.info("Froze street network to %s", cache_path)
    if conn is not None:
        _register(conn, city_row.city_id, network_type, graph)
    return graph


def graph_to_edges(graph: nx.MultiDiGraph) -> gpd.GeoDataFrame:
    """
    Flatten a street graph to a WGS84 edge GeoDataFrame for coverage matching.

    Keeps one row per *undirected* edge with a stable ``edge_id`` (the unordered
    OSM node pair, e.g. ``"12_57"``), ``highway``, ``length`` (metres), and
    LineString ``geometry``. osmnx emits both directions of a two-way street as
    two directed edges; we collapse them by their unordered (u, v) node pair. We
    deliberately do NOT dedup on geometry WKB: osmnx orients each directed edge's
    geometry in its own travel direction, so the reciprocal edge's LineString is
    coordinate-reversed and its WKB differs — a WKB compare would keep both and
    double-count every two-way segment.

    ``edge_id`` is derived from OSM node IDs on the *frozen* network (issue
    #103), so it is stable across runs until a ``--refresh`` re-freezes the
    graph. The road-walk collector (issue #99) keys per-edge coverage on it, and
    it makes future run-to-run streetwalk diffs comparable.
    """
    edges = ox.graph_to_gdfs(graph, nodes=False)
    # graph_to_gdfs indexes edges by (u, v, key); collapse reciprocal directed
    # edges (v, u) onto (u, v) via an order-independent node-pair key.
    u = edges.index.get_level_values("u")
    v = edges.index.get_level_values("v")
    undirected_key = pd.Series(
        [frozenset((a, b)) for a, b in zip(u, v, strict=True)], index=edges.index
    )
    # Human-stable string form of the same unordered pair, order-independent so
    # both directions of a two-way street map to one id.
    edge_id = pd.Series(
        [f"{min(a, b)}_{max(a, b)}" for a, b in zip(u, v, strict=True)], index=edges.index
    )

    # `service` distinguishes an alley from a driveway or a parking aisle, all of
    # which are highway=service; without it the by-type breakdown cannot tell a
    # real back street from someone's driveway. It is already in
    # ox.settings.useful_tags_way, so retaining it needs no re-fetch — but a
    # network with no service roads (and any drive GraphML cached before this)
    # simply won't have the column, hence the membership guard.
    keep = [c for c in ("highway", "service", "length", "geometry") if c in edges.columns]
    edges = edges[keep].copy()
    edges["edge_id"] = edge_id
    edges = edges.loc[~undirected_key.duplicated()].reset_index(drop=True)
    return edges


def fetch_street_edges(
    city_row: CityRow,
    data_dir: str,
    *,
    refresh: bool = False,
    network_type: str = NETWORK_TYPE,
    conn: sqlite3.Connection | None = None,
) -> gpd.GeoDataFrame:
    """Convenience wrapper: fetch the graph and return its edge GeoDataFrame."""
    graph = fetch_graph(city_row, data_dir, refresh=refresh, network_type=network_type, conn=conn)
    return graph_to_edges(graph)
