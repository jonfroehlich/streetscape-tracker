import logging

import numpy as np
import pandas as pd
import pycountry  # for ISO country codes
import us  # for US states
from geopy.distance import geodesic
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim

logger = logging.getLogger(__name__)

# Nominatim usage policy requires an identifiable user agent and max 1 req/sec
NOMINATIM_USER_AGENT = "streetscape_metadata_tracker (jonf@cs.uw.edu)"

_geolocator = Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=10)
_rate_limited_geocode = RateLimiter(_geolocator.geocode, min_delay_seconds=1.1)


def geocode_boundary_raw(
    city_name: str, state_name: str | None = None, country_name: str | None = None
) -> dict | None:
    """
    Rate-limited Nominatim lookup returning the raw top result including
    the boundary polygon GeoJSON, or None when nothing matches.

    Uses a structured query ({city, state, country}) rather than free-form
    text: structured search restricts Nominatim to settlement-type matches,
    which avoids the township/neighborhood mismatches the boundary audit
    (issue #91) exists to catch. Deliberately does NOT bias by a known
    center — the caller may be auditing whether that center is itself the
    product of a bad geocode.

    Geocoder exceptions (timeouts, service unavailable) propagate so
    callers can distinguish transient failures (retry later) from a real
    "not found" (a stable, cacheable answer).

    Example:
        >>> raw = geocode_boundary_raw('Seattle', 'Washington', 'United States')
        >>> raw['geojson']['type']
        'MultiPolygon'
    """
    query = {
        key: value
        for key, value in [("city", city_name), ("state", state_name), ("country", country_name)]
        if value
    }
    location = _rate_limited_geocode(query, language="en", addressdetails=True, geometry="geojson")
    return dict(location.raw) if location is not None else None


def get_state_abbreviation(state_name: str | None) -> str | None:
    """
    Get the standard two-letter abbreviation for a US state.
    Returns original string if no abbreviation is found.

    Args:
        state_name: Full name of the US state

    Returns:
        Two-letter state abbreviation, original string if not found, or None if input is None
    """
    if not state_name:
        return None

    try:
        state = us.states.lookup(state_name)
        return state.abbr if state else state_name
    except AttributeError:
        return state_name


def get_country_code(country_name: str | None) -> str | None:
    """
    Get the standard two-letter ISO country code.

    Args:
        country_name: Full name of the country

    Returns:
        Two-letter ISO country code, or None when the input is None or no
        code could be resolved. (It used to fall back to the full name,
        which polluted `country.code` fields downstream with non-codes.)
    """
    if not country_name:
        return None

    # Handle common variations in country names
    name_variations = {
        "United States": "USA",
        "United States of America": "USA",
        "USA": "USA",
        "UK": "United Kingdom",
        "Great Britain": "United Kingdom",
        "Russia": "Russian Federation",
        "South Korea": "Korea, Republic of",
        "North Korea": "Korea, Democratic People's Republic of",
        "Taiwan": "Taiwan, Province of China",
    }

    # Normalize the country name
    normalized_name = name_variations.get(country_name, country_name)

    try:
        # First try direct lookup
        country = pycountry.countries.get(name=normalized_name)
        if country:
            return country.alpha_2

        # Try fuzzy matching if direct lookup fails
        matches = pycountry.countries.search_fuzzy(normalized_name)
        if matches:
            return matches[0].alpha_2

    except (LookupError, AttributeError):
        pass

    logging.warning(f"No ISO country code found for {country_name!r}")
    return None


class EnhancedLocation:
    """
    A wrapper class for geopy Location objects that adds convenient access to country and state information.

    This class uses the delegation pattern to wrap the original Location object while adding new properties
    for easier access to commonly needed address components. It maintains all original Location functionality
    through delegation while providing cleaner access to country and state data that would normally require
    multiple dictionary lookups.

    https://geopy.readthedocs.io/en/stable/index.html?highlight=location#geopy.location.Location

    Attributes:
        _location: The wrapped geopy Location object
        _country: Cached country name extracted from location.raw['address']
        _state: Cached state/region name extracted from location.raw['address']

    Properties:
        country: The country name, or None if not available
        state: The state/region name, or None if not available

    All other attributes are delegated to the wrapped Location object, so this class can be used
    as a drop-in replacement for the original Location class.
    """

    def __init__(self, city_query_str, location):
        """
        Initialize the enhanced location wrapper.

        Args:
            location: A geopy Location object to be wrapped

        The constructor extracts and caches country and state information from the location's
        raw address data if available. For state/region information, it checks multiple possible
        field names to account for different naming conventions across countries:
        - state: Used in US, Australia, etc.
        - county: Used in UK
        - state_district: Used in some European countries
        - region: Generic fallback used in various countries
        """
        self._location = location
        self._country = None
        self._state = None
        self._city = None
        self._country_code = None
        self._state_code = None
        self._city_query_str = city_query_str
        self._area = None
        self._top_left = None
        self._bot_right = None
        self._width = None
        self._height = None

        logger.debug(f"EnhancedLocation created for {location}")

        if hasattr(location, "raw"):
            address_data = location.raw.get("address", {})
            logger.debug(f"EnhancedLocation address_data {address_data}")

            self._country = address_data.get("country")

            COUNTRY_CODE_FIELD = "country_code"
            if COUNTRY_CODE_FIELD in address_data:
                self._country_code = address_data.get(COUNTRY_CODE_FIELD)
                logger.debug(
                    f"EnhancedLocation country code '{self._country_code}' found with field '{COUNTRY_CODE_FIELD}'"
                )
            elif self._country:
                self._country_code = get_country_code(self._country)
                logger.debug(
                    f"EnhancedLocation country '{self._country}' and code '{self._country_code}' extracted from '{address_data}'"
                )

            # Try different possible state field names
            for field in ["state", "county", "state_district", "region"]:
                if field in address_data:
                    self._state = address_data.get(field)
                    logger.debug(
                        f"EnhancedLocation state '{self._state}' found with field '{field}'"
                    )
                    break

            ISO_3166_2_FIELD = "ISO3166-2-lvl4"
            if ISO_3166_2_FIELD in address_data:
                iso_code = address_data.get(ISO_3166_2_FIELD)

                # Split on hyphen and take the second part
                # e.g., 'US-WA' becomes ['US', 'WA'] and we take 'WA'
                self._state_code = iso_code.split("-")[1] if "-" in iso_code else None
                logger.debug(
                    f"EnhancedLocation state code '{self._state_code}' extracted from '{iso_code}' and field '{ISO_3166_2_FIELD}'"
                )
            elif self._state:
                self._state_code = get_state_abbreviation(self._state)
                logger.debug(
                    f"EnhancedLocation state code '{self._state_code}' derived from state name '{self._state}'"
                )

            # Try different possible city field names
            for field in ["city", "town", "township", "village", "municipality", "suburb"]:
                if field in address_data:
                    self._city = address_data.get(field)
                    logger.debug(
                        f"EnhancedLocation state '{self._city}' found with field '{field}'"
                    )
                    break

            if self._city is None:
                logger.warning(
                    f"Could not find city in {address_data}, will attempt to extract from query string '{self._city_query_str}'"
                )
                if "," in self._city_query_str:
                    self._city = self._city_query_str.split(",")[0].strip()
                else:
                    self._city = self._city_query_str

                logger.info(f"Extracted city from query string: {self._city}")

            if "boundingbox" in self._location.raw:
                try:
                    bbox = self._location.raw["boundingbox"]
                    logger.debug(f"Found bounding box data: {bbox}")

                    # Nominatim returns boundingbox as [south, north, west, east]
                    south, north, west, east = map(float, bbox)

                    # Store corners as tuples (lat, lng)
                    self._top_left = (north, west)
                    self._bot_right = (south, east)

                    # Calculate dimensions using geodesic distance
                    self._width = geodesic((south, west), (south, east)).kilometers
                    self._height = geodesic((south, west), (north, west)).kilometers
                    self._area = self._width * self._height

                    logger.debug(
                        f"EnhancedLocation bounding box calculated: area={self._area:.2f}km², "
                        f"width={self._width:.2f}km, height={self._height:.2f}km"
                    )
                except Exception as e:
                    logger.warning(f"Error processing bounding box data: {e}")

    @property
    def country_code(self) -> str | None:
        """
        Get the two-letter ISO country code.

        Returns:
            str or None: The ISO 3166-1 alpha-2 country code if available, None otherwise
        """
        return self._country_code

    @property
    def state_code(self) -> str | None:
        """
        Get the state/region abbreviation (currently supports US states).

        Returns:
            str or None: The state abbreviation if available (e.g., 'CA' for California),
            None otherwise
        """
        return self._state_code

    @property
    def country(self) -> str | None:
        """
        Get the country name.

        Returns:
            str or None: The country name if available, None otherwise
        """
        return self._country

    @property
    def state(self) -> str | None:
        """
        Get the state/region name.

        Returns:
            str or None: The state or region name if available, None otherwise.
            This could be a state (US), county (UK), or other regional division
            depending on the country.
        """
        return self._state

    @property
    def city(self) -> str | None:
        """
        Get the city name.

        Returns:
            str or None: The city name if available, None otherwise.
            This could come from various fields in the raw data:
            - city: Most common for cities
            - town: Used for smaller municipalities
            - village: Used for very small municipalities
            - municipality: Used in some countries
            - suburb: Used for city subdivisions in some areas
        """
        return self._city

    @property
    def area(self) -> float | None:
        """
        Get the area of the bounding box in square kilometers.

        Returns:
            float or None: The area in km² if bounding box is available, None otherwise
        """
        return self._area

    @property
    def top_left(self) -> tuple | None:
        """
        Get the top-left coordinates of the bounding box.

        Returns:
            tuple or None: (latitude, longitude) tuple if available, None otherwise
        """
        return self._top_left

    @property
    def bottom_right(self) -> tuple | None:
        """
        Get the bottom-right coordinates of the bounding box.

        Returns:
            tuple or None: (latitude, longitude) tuple if available, None otherwise
        """
        return self._bot_right

    @property
    def bbox_center(self) -> tuple | None:
        """
        Get the midpoint of the OSM bounding box.

        This is the correct grid center for a search rectangle: unlike the
        geocoder's reported point (location.latitude/longitude), it is
        guaranteed to sit at the geometric center of the bounding box the
        grid dimensions are derived from, so the sampled rectangle actually
        covers the boundary.

        Returns:
            tuple or None: (latitude, longitude) midpoint if the bounding box
            is available, None otherwise
        """
        if self._top_left is None or self._bot_right is None:
            return None
        north, west = self._top_left
        south, east = self._bot_right
        return ((north + south) / 2.0, (west + east) / 2.0)

    @property
    def width(self) -> float | None:
        """
        Get the width of the bounding box in kilometers.

        Returns:
            float or None: The width in km if bounding box is available, None otherwise
        """
        return self._width

    @property
    def height(self) -> float | None:
        """
        Get the height of the bounding box in kilometers.

        Returns:
            float or None: The height in km if bounding box is available, None otherwise
        """
        return self._height

    @property
    def bbox_tuple(self) -> tuple | None:
        """
        Get the bounding box coordinates in (north, south, east, west) format.
        This format is compatible with libraries like osmnx for network analysis.

        Returns:
            tuple or None: A tuple of (north, south, east, west) coordinates if
            bounding box is available, None otherwise

        Example:
            >>> location = EnhancedLocation(...)
            >>> north, south, east, west = location.bbox_tuple
            >>> G = ox.graph_from_bbox(north, south, east, west, network_type='drive')
        """
        if self._top_left and self._bot_right:
            north, west = self._top_left
            south, east = self._bot_right
            return (north, south, east, west)
        return None

    def __getattr__(self, name):
        """
        Delegate any unknown attribute access to the wrapped Location object.

        This allows the EnhancedLocation to be used anywhere a Location object
        would be used, maintaining backward compatibility while adding new features.

        Args:
            name: The name of the attribute being accessed

        Returns:
            The value of the attribute from the wrapped Location object

        Raises:
            AttributeError: If the attribute doesn't exist on the wrapped Location object
        """
        return getattr(self._location, name)

    def __detailed_str__(self) -> str:
        """
        Returns a detailed, formatted string representation of the location with
        hierarchical components including coordinates and geographical bounds.

        Returns:
            str: A multi-line formatted string containing detailed location information

        Example output:
            City: San Francisco
            State: California (CA)
            Country: United States (US)
            Center: (37.7749, -122.4194)
            Bounding Box:
            Top-Left: (37.8199, -122.4784)
            Bottom-Right: (37.7299, -122.3604)
            Area: 121.4 km²
        """
        lines = []

        # Add city
        if self._city:
            lines.append(f"City: {self._city}")

        # Add state with code if available
        if self._state:
            state_str = self._state
            if self._state_code:
                state_str += f" ({self._state_code})"
            lines.append(f"State: {state_str}")

        # Add country with code if available
        if self._country:
            country_str = self._country
            if self._country_code:
                country_str += f" ({self._country_code})"
            lines.append(f"Country: {country_str}")

        # Add center coordinates
        if hasattr(self._location, "latitude") and hasattr(self._location, "longitude"):
            lines.append(f"Center: ({self._location.latitude:.4f}, {self._location.longitude:.4f})")

        # Add bounding box if available
        if self._top_left and self._bot_right:
            lines.append("Bounding Box:")
            lines.append(f"  Top-Left: ({self._top_left[0]:.4f}, {self._top_left[1]:.4f})")
            lines.append(f"  Bottom-Right: ({self._bot_right[0]:.4f}, {self._bot_right[1]:.4f})")
            lines.append(f"Area: {self._area:.1f} km²")

        return "\n".join(lines) if lines else "Unknown Location"

    def __str__(self) -> str:
        """
        Returns a human-readable string representation of the location.

        The string includes available location components in a hierarchical format:
        city, state (state_code), country (country_code). Components that aren't
        available are omitted.

        Returns:
            str: A formatted string containing the available location information

        Examples:
            "San Francisco, California (CA), United States (US)"
            "London, United Kingdom (GB)"
            "Paris, Île-de-France, France (FR)"
        """
        components = []

        # Add city if available
        if self._city:
            components.append(self._city)

        # Add state with code if available
        if self._state:
            state_str = self._state
            if self._state_code:
                state_str += f" ({self._state_code})"
            components.append(state_str)

        # Add country with code if available
        if self._country:
            country_str = self._country
            if self._country_code:
                country_str += f" ({self._country_code})"
            components.append(country_str)

        # Handle empty case
        if not components:
            return "Unknown Location"

        return ", ".join(components)


# Success-only memo for get_city_location_data. Deliberately NOT lru_cache:
# that cached None results, so one geocoder timeout poisoned the query as
# "city not found" for the rest of the process (e.g. a whole run_cities.py
# batch). Genuine not-founds re-query, which is rare and rate-limited anyway.
_location_cache: dict[tuple[str, float | None, float | None], "EnhancedLocation"] = {}


def get_city_location_data(
    city_query_str: str, center_lat: float | None = None, center_lng: float | None = None
) -> EnhancedLocation | None:
    """
    Get location information for a city including coordinates, country, and bounding box.
    If center coordinates are provided, uses them to help disambiguate common city names.
    Successful lookups are cached for the process lifetime; failures are not.

    If you supply center_lat and center_lng, the function will attempt to find the closest
    matching city. For example:

    # Will find Springfield, Illinois
    location = get_city_location_data("Springfield", 39.7817, -89.6501)

    # Will find Springfield, Massachusetts
    location = get_city_location_data("Springfield", 42.1015, -72.5898)

    Args:
        city_query_str: Name of the city to look up
        center_lat: Optional latitude to help disambiguate city location
        center_lng: Optional longitude to help disambiguate city location

    Returns:
        Location object with these key attributes:
            - latitude: float (e.g., 48.8566)
            - longitude: float (e.g., 2.3522)
            - address: str (full formatted address)
            - raw['address']: dict with components including:
                - city: str
                - country: str
                - state: str (if available)
            - raw['boundingbox']: list of [south, north, west, east] coordinates
                Example: ['48.8155755', '48.9021560', '2.2241989', '2.4697602']
        Returns None if city not found or on error

    Example:
        >>> # Basic usage
        >>> location = get_city_location_data("Paris")
        >>>
        >>> # With disambiguation coordinates (e.g., for Springfield, IL)
        >>> location = get_city_location_data("Springfield", 39.7817, -89.6501)
        >>> if location:
        >>>     print(location.latitude, location.longitude)
        >>>     print(location.address)
        >>>     print(location.raw['address']['country'])
        >>>     bbox = location.raw['boundingbox']
        >>>     print(f"City bounds: N:{bbox[1]} S:{bbox[0]} E:{bbox[3]} W:{bbox[2]}")
    """
    if not city_query_str or not city_query_str.strip():
        logging.error("City name cannot be empty")
        return None

    cache_key = (city_query_str, center_lat, center_lng)
    cached = _location_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        # If center coordinates provided, use them for disambiguation
        found_loc = None
        if center_lat is not None and center_lng is not None:
            # Get multiple location results
            locations = _rate_limited_geocode(
                city_query_str, language="en", addressdetails=True, exactly_one=False
            )

            if locations:
                # Find closest location to provided coordinates
                center_point = (center_lat, center_lng)
                closest_location = min(
                    locations,
                    key=lambda loc: (
                        geodesic(center_point, (loc.latitude, loc.longitude)).kilometers
                    ),
                )
                logging.info(
                    f"Found closest match for {city_query_str} near ({center_lat}, {center_lng}): "
                    f"{closest_location.latitude}, {closest_location.longitude}"
                )
                found_loc = closest_location

        if not found_loc:
            # If no center coordinates or no results found, fall back to basic search
            found_loc = _rate_limited_geocode(city_query_str, language="en", addressdetails=True)

        if found_loc is not None:
            logging.info(
                f"Found coordinates for {city_query_str}: {found_loc.latitude}, {found_loc.longitude}"
            )

            enhancedLoc = EnhancedLocation(city_query_str, found_loc)
            logging.info(f"From query string '{city_query_str}', created '{enhancedLoc}'")
            _location_cache[cache_key] = enhancedLoc
            return enhancedLoc

        else:
            logging.warning(f"Could not find coordinates for {city_query_str}")
            return None

    except GeocoderTimedOut:
        logging.error(f"Timeout looking up coordinates for {city_query_str}")
        return None
    except GeocoderUnavailable:
        logging.error(f"Geocoding service unavailable for {city_query_str}")
        return None
    except Exception as e:
        logging.error(f"Error looking up coordinates for {city_query_str}: {str(e)}")
        return None


# Mean Earth radius (IUGG), in metres. The spherical model is off by at most
# ~0.5% against the WGS84 ellipsoid, which is far below the tolerances it is
# used against (a 50 m query radius, #367). www/js/streetscape-utils.js mirrors
# this constant so the frontend and the stats agree about a distance.
EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1, lon1, lat2, lon2):
    """
    Great-circle distance in metres between two points (or arrays of points).

    Vectorized: every argument may be a scalar, a NumPy array or a pandas
    Series, and the result has the broadcast shape (a Series keeps its index).
    NaN in any coordinate yields NaN, never 0 -- a missing pano position must
    not read as a pano standing exactly on its query point.

    Replaces the planar ``sqrt(dlat**2 + dlon**2) * 111000`` the coverage stats
    used before issue #367, which ignored cos(latitude) and so overstated every
    east-west offset (by 2x at 60 degrees north).

    Example:
        >>> round(haversine_m(60.0, 0.0, 60.0, 1.0) / 1000, 1)  # 1 deg lon at 60N
        55.6
        >>> round(haversine_m(0.0, 0.0, 1.0, 0.0) / 1000, 1)  # 1 deg of latitude
        111.2
    """
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = phi2 - phi1
    dlmb = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlmb / 2) ** 2
    # clip: float error can push `a` a hair past 1 for antipodal points
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def get_bounding_box(df: pd.DataFrame) -> dict[str, float]:
    """
    Get the bounding box coordinates from a DataFrame of points.

    Args:
        df: DataFrame with 'pano_lat' and 'pano_lon' columns

    Returns:
        Dictionary with 'south', 'west', 'north', and 'east' keys
    """
    return {
        "south": df["pano_lat"].min(),
        "west": df["pano_lon"].min(),
        "north": df["pano_lat"].max(),
        "east": df["pano_lon"].max(),
    }


def get_bounding_box_size(df: pd.DataFrame) -> tuple[float, float]:
    """
    Calculate the width and height of a bounding box containing all points
    using geopy's geodesic calculations.

    Returns:
        tuple of (width_meters, height_meters)
    """
    # Get bounding box coordinates
    min_lat = df["pano_lat"].min()
    max_lat = df["pano_lat"].max()
    min_lon = df["pano_lon"].min()
    max_lon = df["pano_lon"].max()

    # Calculate width using the middle latitude
    mid_lat = (min_lat + max_lat) / 2
    width = geodesic((mid_lat, min_lon), (mid_lat, max_lon)).meters

    # Calculate height
    height = geodesic((min_lat, min_lon), (max_lat, min_lon)).meters

    return width, height


def get_search_dimensions(
    city_name: str, default_width: float, default_height: float
) -> tuple[float, float]:
    """
    Calculate the width and height dimensions for a city search area.

    Attempts to determine search dimensions by calculating them from the city's
    geographic boundaries using OpenStreetMap data. Falls back to the provided
    default dimensions if boundary data cannot be obtained.

    The dimensions are calculated using geodesic distances:
    - Width is measured along the middle latitude of the bounding box
    - Height is measured along the western edge of the bounding box

    Args:
        city_name: Name of the city to look up boundaries for
        default_width: Default width in meters if boundary lookup fails
        default_height: Default height in meters if boundary lookup fails

    Returns:
        tuple[float, float]: A tuple of (width_meters, height_meters) representing
        the search area dimensions

    Example:
        >>> width, height = get_search_dimensions("Paris", 5000, 5000)
        Using inferred city boundaries for Paris: 11532m x 8377m
        Search area for Paris: 96.6 square km
    """

    width, height = default_width, default_height

    try:
        location = get_city_location_data(city_name)
        if location and "boundingbox" in location.raw:
            # boundingbox is [south, north, west, east]
            bbox = location.raw["boundingbox"]
            # Convert string coordinates to float
            south, north, west, east = map(float, bbox)

            # Calculate width using middle latitude
            mid_lat = (north + south) / 2
            west_point = (mid_lat, west)
            east_point = (mid_lat, east)
            width = geodesic(west_point, east_point).meters

            # Calculate height
            sw_point = (south, west)
            nw_point = (north, west)
            height = geodesic(sw_point, nw_point).meters

            print(f"Using inferred city boundaries for {city_name}: {width:.0f}m x {height:.0f}m")
            logger.info(
                f"Using inferred city boundaries for {city_name}: {width:.0f}m x {height:.0f}m"
            )
        else:
            print(f"Could not find boundary data for {city_name}, using defaults")
            logger.warning(f"Could not find boundary data for {city_name}, using defaults")
    except Exception as e:
        print(f"Failed to infer city boundaries: {str(e)}")
        logger.error(f"Failed to infer city boundaries: {str(e)}")

    area = (width * height) / 1000000.0  # Convert to square km
    print(f"Search area for {city_name}: {area:,.1f} square km")
    logger.info(f"Search area for {city_name}: {area:,.1f} square km")

    return width, height


def get_best_folium_zoom_level(
    search_grid_width_in_meters: float, search_grid_height_in_meters: float
) -> int:
    """
    Determine the optimal Folium map zoom level based on search area dimensions.

    Selects the most appropriate zoom level to ensure the entire search grid is
    visible while maintaining useful detail. Uses the larger dimension between
    width and height to determine zoom level. Zoom levels correspond to these
    approximate viewing scales:

    Zoom | Approximate Coverage | Typical Use Case
    -----|---------------------|------------------
    20   | 50m                 | Building level detail
    19   | 100m                | City block
    18   | 200m                | Multiple blocks
    17   | 400m                | Neighborhood
    16   | 800m                | Small district
    15   | 1.5km               | Large district
    14   | 3km                 | Small city
    13   | 6km                 | Medium city
    12   | 12km+               | Large city/region

    Args:
        search_grid_width_in_meters: Width of the search area in meters
        search_grid_height_in_meters: Height of the search area in meters

    Returns:
        int: Folium zoom level between 12 (most zoomed out) and 19 (most zoomed in)
        that best fits the search area dimensions

    Example:
        >>> zoom = get_best_folium_zoom_level(1200, 800)
        >>> print(zoom)  # Returns 15 since max dimension (1200m) fits in 1.5km scale
        15
    """
    max_dimension = max(search_grid_width_in_meters, search_grid_height_in_meters)
    if max_dimension <= 100:
        zoom_level = 19
    elif max_dimension <= 200:
        zoom_level = 18
    elif max_dimension <= 400:
        zoom_level = 17
    elif max_dimension <= 800:
        zoom_level = 16
    elif max_dimension <= 1500:
        zoom_level = 15
    elif max_dimension <= 3000:
        zoom_level = 14
    elif max_dimension <= 6000:
        zoom_level = 13
    else:
        zoom_level = 12

    return zoom_level
