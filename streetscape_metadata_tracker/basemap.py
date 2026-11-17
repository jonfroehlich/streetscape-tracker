"""CARTO basemap tiles for every map this project generates.

CARTO requires a key on ``basemaps.cartocdn.com`` (observed 2026-08-28), and it
enforces the key by watermarking the tile rather than by refusing the request:
a keyless request returns HTTP 200 and a PNG stamped "API KEY REQUIRED". So a
map built with a bare tile name (folium's ``tiles="CartoDB positron"``) renders
the watermark with no error anywhere. That is how the ``--check-boundary``
preview kept showing the watermark after the web frontend was fixed.

The key lives in exactly one place, ``www/js/streetscape-utils.js``, the
frontend module that also serves it to the public site. Generated HTML cannot
load that module, so the key is read out of it here rather than pasted into a
second copy (see ``docs/experiments/carto-basemap-key.md``).

Example::

    import folium
    from streetscape_metadata_tracker.basemap import add_carto_basemap

    m = folium.Map(location=[47.6, -122.3], zoom_start=12, tiles=None)
    add_carto_basemap(m, "light_all")
"""

import os
import re
from urllib.parse import quote

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASEMAP_KEY_SOURCE = os.path.join(_PROJECT_ROOT, "www", "js", "streetscape-utils.js")
_CARTO_KEY_RE = re.compile(r'CARTO_BASEMAP_KEY\s*=\s*"([^"]+)"')

CARTO_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    'contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
)


def carto_basemap_key(source_path: str = BASEMAP_KEY_SOURCE) -> str:
    """Read the CARTO basemap key out of the frontend module that owns it.

    Raises rather than degrading: a missing or renamed const would otherwise
    leave the map requesting tiles without a key, and CARTO answers that with
    HTTP 200 and a watermarked tile, so the failure would look like a cosmetic
    oddity instead of an error.
    """
    with open(source_path, encoding="utf-8") as f:
        match = _CARTO_KEY_RE.search(f.read())
    if not match:
        raise ValueError(f"no CARTO_BASEMAP_KEY const found in {source_path}")
    return match.group(1)


def carto_tile_url(style: str) -> str:
    """Return the keyed Leaflet tile URL template for a CARTO raster style.

    ``style`` is CARTO's path segment, e.g. ``"light_all"`` (Positron) or
    ``"dark_all"`` (Dark Matter).
    """
    key = quote(carto_basemap_key(), safe="")
    return f"https://{{s}}.basemaps.cartocdn.com/{style}/{{z}}/{{x}}/{{y}}{{r}}.png?key={key}"


def add_carto_basemap(folium_map, style: str, **tile_layer_kwargs):
    """Add a keyed CARTO basemap layer to a folium map and return the layer."""
    import folium

    layer = folium.TileLayer(
        tiles=carto_tile_url(style),
        attr=CARTO_ATTRIBUTION,
        name=f"CARTO {style}",
        **tile_layer_kwargs,
    )
    layer.add_to(folium_map)
    return layer
