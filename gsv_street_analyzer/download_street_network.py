import argparse
import logging
import sys
from pathlib import Path

import networkx as nx
import osmnx as ox
from tenacity import retry, stop_after_attempt, wait_exponential

# Add parent directory to Python path
sys.path.append(str(Path(__file__).parent.parent))
from streetscape_metadata_tracker import geoutils

# Configure logging
logger = logging.getLogger(__name__)

# Configure OSMnx
ox.settings.log_console = True
ox.settings.use_cache = True
# osmnx 2.x renamed this to requests_timeout; `ox.settings` is a plain module,
# so the old `ox.settings.timeout = 30` here silently created an attribute
# nothing read. This module is legacy and unused by the active pipeline, but a
# known-wrong line invites being copied — see issue #209 and the live version
# in streetscape_street_analyzer/download_street_network.py.
ox.settings.requests_timeout = 180


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def download_street_network(city_name: str, save_dir: str = "data") -> nx.MultiDiGraph | None:
    """
    Download street network for a given city using OSMnx with retry logic.

    Args:
        city_name: Name of the city to download
        save_dir: Directory to save the network data

    Returns:
        NetworkX MultiDiGraph object containing the street network

    Example:
        >>> G = download_street_network("Amsterdam")
        >>> print(f"Downloaded {len(G.nodes)} nodes and {len(G.edges)} edges")
    """
    try:
        # Get city bounding box using your existing utility
        location = geoutils.get_city_location_data(city_name)
        if not location:
            logger.error(f"Could not find location data for {city_name}")
            return None

        logger.info(f"Downloading street network for {location}")
        logger.info("\n" + location.__detailed_str__())

        north, south, east, west = location.bbox_tuple

        # Download street network
        # https://osmnx.readthedocs.io/en/stable/user-reference.html#osmnx.graph.graph_from_bbox
        G = ox.graph_from_bbox(
            bbox=(west, south, east, north),  # Pass as single tuple
            network_type="drive",
            simplify=True,
            retain_all=False,
            truncate_by_edge=True,
            custom_filter=None,
        )

        logger.info(f"Downloaded network with {len(G.nodes)} nodes and {len(G.edges)} edges")
        return G

    except Exception as e:
        logger.error(f"Error downloading street network: {str(e)}")
        raise  # Let retry decorator handle it


def save_network(G: nx.MultiDiGraph, city_name: str, save_dir: str = "data"):
    """
    Save street network in multiple formats for different use cases.

    Args:
        G: NetworkX graph object
        city_name: Name of the city (used for filename)
        save_dir: Directory to save files
    """
    save_path = Path(save_dir)
    save_path.mkdir(exist_ok=True)

    # Save as GraphML for complete network topology
    graphml_path = save_path / f"{city_name}_network.graphml"
    ox.save_graphml(G, graphml_path)

    # Save as GeoJSON for web visualization
    geojson_path = save_path / f"{city_name}_network.geojson"
    ox.save_graph_geopackage(G, filepath=geojson_path, directed=True)

    logger.info(f"Saved network to {save_path}")


def main():
    parser = argparse.ArgumentParser(description="Download street network for a city")
    parser.add_argument("city", help="Name of the city")
    parser.add_argument("--save_dir", default="data", help="Directory to save network data")
    parser.add_argument(
        "--log-level",
        type=str,
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level for output messages",
    )

    args = parser.parse_args()

    # Configure logging - this is sync but only happens once at startup
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Download network
    G = download_street_network(args.city, args.save_dir)
    if G is not None:
        save_network(G, args.city, args.save_dir)


if __name__ == "__main__":
    main()
