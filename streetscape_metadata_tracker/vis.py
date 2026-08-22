# streetscape_metadata_tracker/vis.py

import json
import logging
from datetime import datetime
from math import cos, pi
from urllib.parse import quote

import branca.colormap as cm
import folium
import folium.plugins  # noqa: F401 — register folium.plugins.* (not auto-imported since folium 0.16)
import matplotlib.colors
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from .analysis import is_google_copyright, plausible_capture_mask
from .geoutils import get_best_folium_zoom_level, get_bounding_box, get_bounding_box_size
from .progress import progress

logger = logging.getLogger(__name__)


def display_search_area(
    city_name: str,
    city_center_lat: float,
    city_center_lng: float,
    search_grid_width_in_meters: float,
    search_grid_height_in_meters: float,
    step_size_in_meters: float = 20,
) -> folium.Map:
    """
    Creates an interactive map visualization showing a city's search area with grid overlay.

    Args:
        city_center_lat (float): Latitude of the city center
        city_center_lng (float): Longitude of the city center
        search_grid_width_in_meters (float): Width of the search area in meters
        search_grid_height_in_meters (float): Height of the search area in meters
        step_size_in_meters (float, optional): Size of each grid cell in meters. Defaults to 20.

    Returns:
        folium.Map: Interactive map object showing the search area and grid

    Note:
        The conversion between meters and degrees accounts for latitude-dependent
        distortion using the Haversine approximation. The grid lines are drawn
        taking into account that degrees of longitude vary with latitude.
    """

    # Grid styling parameters
    GRID_BACKGROUND_COLOR = "#3B82F6"  # Medium blue
    GRID_BACKGROUND_OPACITY = 0.15
    GRID_BORDER_COLOR = "#2563EB"  # Slightly darker blue
    GRID_BORDER_WEIGHT = 2
    GRID_LINE_COLOR = "blue"
    GRID_LINE_WEIGHT = 1.0
    GRID_LINE_OPACITY = 0.5

    # Calculate total number of grid points
    points_width = int(search_grid_width_in_meters / step_size_in_meters) + 1
    points_height = int(search_grid_height_in_meters / step_size_in_meters) + 1
    total_points = points_width * points_height

    # Calculate estimated download time (40 points/second)
    points_per_second = 40
    estimated_seconds = total_points / points_per_second
    hours = int(estimated_seconds // 3600)
    minutes = int((estimated_seconds % 3600) // 60)
    seconds = int(estimated_seconds % 60)

    if hours > 0:
        time_str = f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        time_str = f"{minutes}m {seconds}s"
    else:
        time_str = f"{seconds}s"

    # Create map centered on the city
    zoom_level = get_best_folium_zoom_level(
        search_grid_width_in_meters, search_grid_height_in_meters
    )

    # Map base style
    m = folium.Map(
        location=[city_center_lat, city_center_lng], zoom_start=zoom_level, tiles="CartoDB positron"
    )

    # Create styled HTML for popup
    popup_html = f"""
    <div style="font-family: Arial, sans-serif; line-height: 1.5; padding: 5px;">
        <h4 style="margin: 0 0 10px 0; color: #2c3e50;">{city_name}</h4>
        <table style="border-collapse: collapse; width: 100%;">
            <tr>
                <td style="padding: 3px; color: #7f8c8d;"><b>Center Pt:</b></td>
                <td style="padding: 3px;">{city_center_lat:.6f}, {city_center_lng:.6f}</td>
            </tr>
            <tr>
                <td style="padding: 3px; color: #7f8c8d;"><b>Search Area:</b></td>
                <td style="padding: 3px;">{search_grid_width_in_meters:,.1f} × {search_grid_height_in_meters:,.1f} meters</td>
            </tr>
            <tr>
                <td style="padding: 3px; color: #7f8c8d;"><b>Step Size:</b></td>
                <td style="padding: 3px;">{step_size_in_meters:,.1f} meters</td>
            </tr>
            <tr>
                <td style="padding: 3px; color: #7f8c8d;"><b>Grid Points:</b></td>
                <td style="padding: 3px;">{total_points:,} points ({points_width} × {points_height})</td>
            </tr>
            <tr>
                <td style="padding: 3px; color: #7f8c8d;"><b>Est. Time:</b></td>
                <td style="padding: 3px;">~{time_str} at 40 points/sec</td>
            </tr>
        </table>
    </div>
    """

    # Add center marker
    folium.Marker(
        [city_center_lat, city_center_lng],
        popup=folium.Popup(popup_html, max_width=300),
        icon=folium.Icon(color="red", icon="info-sign"),
    ).add_to(m)

    # Calculate degrees using proper latitude adjustment
    # Length of 1° latitude = ~111km (constant)
    # Length of 1° longitude = 111km * cos(latitude)
    meters_per_lat_degree = 111000  # More precise than 111320
    meters_per_lng_degree = meters_per_lat_degree * cos(city_center_lat * pi / 180)

    width_deg = search_grid_width_in_meters / meters_per_lng_degree
    height_deg = search_grid_height_in_meters / meters_per_lat_degree

    # Calculate bounds
    bounds = [
        [city_center_lat - height_deg / 2, city_center_lng - width_deg / 2],
        [city_center_lat + height_deg / 2, city_center_lng + width_deg / 2],
    ]

    # Draw rectangle for search area
    folium.Rectangle(
        bounds=bounds,
        color=GRID_BORDER_COLOR,
        fill=True,
        weight=GRID_BORDER_WEIGHT,
        fillColor=GRID_BACKGROUND_COLOR,
        fillOpacity=GRID_BACKGROUND_OPACITY,
        popup=f"Search Area: {search_grid_width_in_meters}m x {search_grid_height_in_meters}m",
    ).add_to(m)

    # Add grid overlay
    step_size_lat = step_size_in_meters / meters_per_lat_degree
    step_size_lng = step_size_in_meters / meters_per_lng_degree

    # Draw vertical grid lines
    lng = bounds[0][1]
    while lng <= bounds[1][1]:
        points = [[bounds[0][0], lng], [bounds[1][0], lng]]
        folium.PolyLine(
            points, color=GRID_LINE_COLOR, weight=GRID_LINE_WEIGHT, opacity=GRID_LINE_OPACITY
        ).add_to(m)
        lng += step_size_lng

    # Draw horizontal grid lines
    lat = bounds[0][0]
    while lat <= bounds[1][0]:
        points = [[lat, bounds[0][1]], [lat, bounds[1][1]]]
        folium.PolyLine(
            points, color=GRID_LINE_COLOR, weight=GRID_LINE_WEIGHT, opacity=GRID_LINE_OPACITY
        ).add_to(m)
        lat += step_size_lat

    # Add scale bar
    folium.plugins.MeasureControl(position="bottomleft").add_to(m)

    return m


def _kartaview_viewer_url(pano_id, row) -> str | None:
    """
    KartaView viewer deep-link for one census row, or None when unlinkable.

    KartaView's viewer is addressed by (sequence, index within it) rather than
    by photo id — mirroring PROVIDERS.kartaview.viewerUrl in
    www/js/streetscape-utils.js, including its call to render no link at all
    when either field is missing (the decoder keeps a null sequence rather than
    inventing one, and a link to nowhere is worse than none).
    """
    sequence = row.get("sequence_id")
    index = row.get("sequence_index")
    if pd.isna(sequence) or pd.isna(index) or sequence == "":
        return None
    return f"https://kartaview.org/details/{quote(str(sequence), safe='')}/{int(index)}"


# User-facing labels and pano viewer deep-links per provider (mirrors the
# PROVIDERS registry in www/js/streetscape-utils.js). viewer_url takes the whole
# row besides the pano id because KartaView's viewer is not addressable by photo
# id; it may return None, which the popup renders as no link at all. Every
# naming.KNOWN_PROVIDERS member must have an entry — a run's map is generated
# AFTER the run is registered, so a missing one fails a fully successful
# collection at the last step (a test pins the coverage).
PROVIDER_DISPLAY = {
    "gsv": {
        "label": "GSV",
        "viewer_url": lambda pano_id, row: (
            f"https://www.google.com/maps/@?api=1&map_action=pano&pano={pano_id}"
        ),
    },
    "mapillary": {
        "label": "Mapillary",
        "viewer_url": lambda pano_id, row: f"https://www.mapillary.com/app/?pKey={pano_id}",
    },
    "kartaview": {
        "label": "KartaView",
        "viewer_url": _kartaview_viewer_url,
    },
}


def create_visualization_map(df: pd.DataFrame, city_name: str, provider: str = "gsv") -> folium.Map:
    """
    Create an interactive map visualization of a run's pano metadata with a
    temporal histogram.

    Args:
        df: DataFrame containing run metadata (config.METADATA_DTYPES schema)
        city_name: Name of the city being visualized
        provider: imagery provider (any naming.KNOWN_PROVIDERS member);
            controls labels, per-pano viewer links, and the official-imagery
            filter

    Returns:
        folium.Map object with the visualization
    """
    display = PROVIDER_DISPLAY[provider]
    label = display["label"]
    logger.debug(f"Creating visualization map for {city_name} [{provider}] with {len(df)} rows")

    # Filter for valid data
    valid_rows = df[(df["status"] == "OK") & (df["pano_lat"].notna()) & (df["pano_lon"].notna())]
    logger.info(f"Rows with valid pano coordinates: {len(valid_rows)}")

    # Official-imagery filter applies only to GSV (Mapillary rows are all
    # provider imagery already)
    if provider == "gsv":
        valid_rows = valid_rows[is_google_copyright(valid_rows["copyright_info"])]
        logger.info(f"Filtered rows with official Google imagery: {len(valid_rows)}")

    # Filter for valid dates (same as before)
    valid_rows = valid_rows.dropna(subset=["capture_date"])
    logger.info(f"Filtered rows with valid capture dates: {len(valid_rows)}")

    # ...and for dates that could be true. A contributor photosphere with
    # corrupt EXIF (issue #213) would otherwise stretch the color scale and the
    # temporal histogram across centuries, hiding the real spread of a whole
    # city. The © Google filter above already removes most of them for gsv, so
    # this mostly protects Mapillary and the rare corrupt official date.
    #
    # Unreadable and impossible are counted separately: both leave the frame
    # here, but they are different defects with different responses — a 2611
    # date is a contributor's corrupt EXIF and expected in small numbers, while
    # a value we cannot parse at all points at the CSV or the decoder.
    dates = pd.to_datetime(valid_rows["capture_date"], errors="coerce")
    unreadable = int(dates.isna().sum())
    if unreadable:
        logger.warning(f"Dropped {unreadable} pano(s) whose capture date could not be parsed")
    plausible = plausible_capture_mask(dates, pd.Timestamp(datetime.now()), provider)
    impossible = int((~plausible).sum()) - unreadable
    if impossible:
        logger.warning(f"Dropped {impossible} pano(s) whose capture date cannot be true")
    # Keep the parsed dates rather than re-parsing the raw column further down:
    # this is the whole surviving census of a large Mapillary city.
    valid_rows = valid_rows.copy()
    valid_rows["capture_date"] = dates
    valid_rows = valid_rows[plausible.to_numpy()]

    # Filter for unique pano_ids
    valid_rows = valid_rows.drop_duplicates(subset="pano_id")
    logger.info(f"Final valid rows with unique pano_ids: {len(valid_rows)}")

    if len(valid_rows) == 0:
        logger.warning("No valid data to visualize")
        return folium.Map()

    # Calculate map center
    map_center = [valid_rows["pano_lat"].mean(), valid_rows["pano_lon"].mean()]

    # Get bounding box using existing function
    bbox = get_bounding_box(valid_rows)
    bbox_coords = [
        [bbox["south"], bbox["west"]],  # Southwest corner
        [bbox["north"], bbox["east"]],  # Northeast corner
    ]

    # Calculate bounding box dimensions
    width_meters, height_meters = get_bounding_box_size(valid_rows)
    logger.debug(
        f"Bounding box dimensions: {width_meters:.1f} x {height_meters:.1f} meters from {valid_rows.shape[0]} panos"
    )

    area_km2 = (width_meters * height_meters) / 1_000_000  # Convert to km²
    zoom_level = get_best_folium_zoom_level(width_meters, height_meters)

    logger.debug(f"Map center: {map_center}, Bounding box: {bbox_coords}, Area: {area_km2:.1f} km²")

    # Calculate temporal statistics. capture_date is already datetime64 — it was
    # parsed once for the plausibility filter above and kept.
    logger.debug(f"Calculating temporal statistics for {len(valid_rows)} rows")
    now = datetime.now()
    valid_rows["age_years"] = (now - valid_rows["capture_date"]).dt.days / 365.25

    avg_age = valid_rows["age_years"].mean()
    age_std = valid_rows["age_years"].std()
    median_age = valid_rows["age_years"].median()
    total_panos = len(valid_rows)

    logger.info(
        f"Average age: {avg_age:.1f} years, Median age: {median_age:.1f} years, Total panos: {total_panos}, Area: {area_km2:.1f} km²"
    )

    # Calculate coverage density. Guard against a degenerate 0-area bbox (a
    # single pano, or collinear panos, gives a 0 x 0 bounding box), which would
    # otherwise raise ZeroDivisionError and crash the run's local viz step
    # after the run is already cataloged (issue #69).
    density_per_km2 = total_panos / area_km2 if area_km2 > 0 else float("nan")
    density_str = f"{density_per_km2:.1f}" if area_km2 > 0 else "n/a"

    # Calculate temporal coverage
    date_range = (valid_rows["capture_date"].max() - valid_rows["capture_date"].min()).days / 365.25

    # Create base map
    folium_map = folium.Map(location=map_center, zoom_start=zoom_level, tiles=None)

    # Add dark theme tile layer
    folium.TileLayer(
        tiles="cartodbdark_matter",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        opacity=0.8,
    ).add_to(folium_map)

    # Create enhanced tooltip HTML
    bbox_tooltip_html = f"""
    <div style='font-family: Arial, sans-serif; font-size: 12px; line-height: 1.5'>
        <h3 style='margin: 0 0 8px 0'>{city_name}: {label} Coverage Area</h3>
        <div style='margin-bottom: 4px'>
            <strong>Grid Area:</strong> {width_meters:,.0f} x {height_meters:,.0f}m ({area_km2:.1f} km²)
        </div>
        <div style='margin-bottom: 4px'>
            <strong>Total {label} Panos:</strong> {total_panos:,} ({density_str} panos/km²)
        </div>
        <div style='margin-bottom: 4px'>
            <strong>Avg Pano Age:</strong> {avg_age:.1f} yrs (SD={age_std:.1f} yrs)
        </div>
        <div style='margin-bottom: 4px'>
            <strong>Median Pano Age:</strong> {median_age:.1f} yrs
        </div>
        <div style='margin-bottom: 4px'>
            <strong>Temporal Coverage:</strong> {date_range:.1f} years
        </div>
        <div style='margin-bottom: 4px'>
            <strong>Date Range:</strong> {valid_rows["capture_date"].min().strftime("%Y-%m")} to {valid_rows["capture_date"].max().strftime("%Y-%m")}
        </div>
    </div>
    """

    # Add bounding box rectangle with enhanced tooltip
    folium.Rectangle(
        bounds=bbox_coords,
        color="#4CC3D9",  # Muted cyan-blue
        weight=2,
        fill=False,
        opacity=0.7,
        popup=bbox_tooltip_html,  # Using the same content for popup and tooltip
        tooltip=bbox_tooltip_html,
    ).add_to(folium_map)

    # Change the colormap creation to use years
    oldest_date = valid_rows["capture_date"].min()
    years_since_oldest = (datetime.now() - oldest_date).days / 365.25
    colormap = cm.linear.YlOrRd_09.scale(0, years_since_oldest)
    colormap.caption = "Age (Years)"  # Update caption

    # Prepare histogram data - now using years instead of days
    hist_data = valid_rows.groupby("capture_date").size().reset_index()
    hist_data.columns = ["date", "count"]
    hist_data["date_str"] = hist_data["date"].dt.strftime("%Y-%m")
    hist_data["years_ago"] = (datetime.now() - hist_data["date"]).dt.days / 365.25
    hist_data["color"] = hist_data["years_ago"].apply(
        lambda x: matplotlib.colors.to_hex(colormap(x))
    )

    # Add markers
    marker_data = []
    markers_fg = folium.FeatureGroup(name="Pano Markers")  # Create feature group for markers
    for idx, row in progress(
        valid_rows.iterrows(),
        total=len(valid_rows),
        desc=f"Creating {label} point map markers",
        unit="marker",
    ):
        capture_date = row["capture_date"]
        date_str = capture_date.strftime("%Y-%m")
        age_years = (datetime.now() - capture_date).days / 365.25
        color = matplotlib.colors.to_hex(colormap(age_years))

        viewer_url = display["viewer_url"](row["pano_id"], row)
        viewer_link = (
            f'<br><a href="{viewer_url}" target="_blank">View in {label}</a>' if viewer_url else ""
        )
        popup = folium.Popup(
            f"""
            <div>
                Capture Date: {date_str}
                <br>Age: {age_years:.1f} years
                <br>Photographer: {row["copyright_info"]}{viewer_link}
            </div>
        """,
            max_width=300,
        )

        circle_marker = folium.CircleMarker(
            location=[row["pano_lat"], row["pano_lon"]],
            radius=2,
            color=color,
            stroke=False,
            fill=True,
            fill_color=color,
            fill_opacity=0.8,
            popup=popup,
            tooltip=f"Capture Date: {date_str}<br>Age: {age_years:.1f} years",
            name="gsv-pano-marker",
        )

        # Add custom data attributes
        # circle_marker.add_child(Element(f'<div data-date="{date_str}" data-age="{age_years}"></div>'))
        # circle_marker.add_child(Element(f'<path data-date="{date_str}" data-age="{age_years}"></path>'))
        markers_fg.add_child(circle_marker)

        marker_data.append({"element_id": f"marker_{idx}", "date": date_str})

    # Add feature group to map
    markers_fg.add_to(folium_map)

    # Add HTML/CSS for legend and histogram
    legend_and_hist_html = f"""
    <style>
        .overlay-panel {{
            background-color: rgba(255, 255, 255, 0.9);
            padding: 15px;
            border-radius: 6px;
            border: 1px solid #ccc;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2);
            z-index: 1000;
        }}

        .histogram-container {{
            position: fixed;
            bottom: 50px;
            right: 50px;
        }}

        .histogram-content {{
            width: 300px;
            height: 150px;
            margin-top: 10px;
        }}

        .legend {{
            background-color: rgba(255, 255, 255, 0.9) !important;
            padding: 6px !important;
            border-radius: 4px !important;
            border: 1px solid #ccc !important;
        }}

        .leaflet-control-colormap {{
            background-color: rgba(255, 255, 255, 0.9) !important;
            padding: 6px !important;
            border-radius: 4px !important;
            border: 1px solid #ccc !important;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2) !important;
        }}

        .panel-title {{
            font-size: 14px;
            font-weight: bold;
            margin: 0 0 10px 0;
            color: #333;
        }}

        .minimize-button {{
            position: absolute;
            top: 10px;
            right: 10px;
            background: none;
            border: none;
            cursor: pointer;
            padding: 5px;
            font-size: 16px;
            color: #666;
        }}

        .minimize-button:hover {{
            color: #333;
        }}

        .histogram-container.minimized {{
            height: 40px;  /* Just enough for the title bar */
            overflow: hidden;
        }}
    </style>
    <div class="overlay-panel histogram-container">
        <button class="minimize-button" onclick="toggleHistogram()">−</button>
        <div class="panel-title">{city_name}: {label} Coverage Over Time</div>
        <div class="histogram-content">
            <canvas id="histogramCanvas" width="300" height="150"></canvas>
        </div>
    </div>
    """
    folium_map.get_root().html.add_child(folium.Element(legend_and_hist_html))

    # Add required JavaScript libraries
    folium_map.get_root().html.add_child(
        folium.Element("""
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.7.0/chart.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-datalabels/2.0.0/chartjs-plugin-datalabels.min.js"></script>
    <script>
    Chart.register(ChartDataLabels);
    </script>
    """)
    )

    # Add JavaScript for interactive features
    hist_data_json = json.dumps(hist_data.to_dict("records"), default=str)
    marker_data_json = json.dumps(marker_data)
    histogram_js = f"""
    <script>
    var markerData = {marker_data_json};
    var histogramData = {hist_data_json};
    var currentHighlight = null;

    console.log('Histogram data:', histogramData);
    console.log('Marker data:', markerData);

    // First, verify data is properly passed
    if (!histogramData) {{
        console.error('Histogram data is undefined!');
    }}
    if (!markerData) {{
        console.error('Marker data is undefined!');
    }}

    function toggleHistogram() {{
        const container = document.querySelector('.histogram-container');
        const button = document.querySelector('.minimize-button');
        
        if (container.classList.contains('minimized')) {{
            // Maximize
            container.classList.remove('minimized');
            button.textContent = '−';  // Minus sign
        }} else {{
            // Minimize
            container.classList.add('minimized');
            button.textContent = '+';  // Plus sign
        }}
    }}

    function getGSVMarkers() {{
        const allMarkers = document.querySelectorAll('.leaflet-interactive');
        return Array.from(allMarkers).filter(marker => {{
            const pathData = marker.getAttribute('d');
            const isCircle = pathData && pathData.includes('a2,2');
            return isCircle;
        }});
    }}

    function highlightDate(targetDate) {{
        console.log('Highlighting date:', targetDate);
        console.log('Current highlight:', currentHighlight);

        if (currentHighlight !== targetDate) {{
            resetHighlight();
        }}

        const gsvMarkers = getGSVMarkers();
        console.log('Found GSV markers:', gsvMarkers ? gsvMarkers.length : 0);
        
        let highlightedCount = 0;
        let filteredCount = 0;
        gsvMarkers.forEach(function(marker, index) {{
            if (index < markerData.length) {{
                const markerDate = markerData[index].date;
                const isHighlighted = markerDate === targetDate;
                marker.style.opacity = isHighlighted ? '1' : '0.1';
                marker.style.fillOpacity = isHighlighted ? '1' : '0.1';
                
                if (isHighlighted) {{
                    highlightedCount++;
                }} else {{
                    filteredCount++;
                }}
            }}
        }});
        
        console.log(`Highlighted: ${{highlightedCount}}, Filtered out: ${{filteredCount}}`);
        
        
        currentHighlight = targetDate;

        if (window.histogramChart) {{
            window.histogramChart.data.datasets[0].backgroundColor = histogramData.map(d =>
                d.date_str === targetDate ? d.color : fadeColor(d.color, 0.2)
            );
            window.histogramChart.update();
        }}
    }}

    function resetHighlight() {{
        const gsvMarkers = getGSVMarkers();
        gsvMarkers.forEach(function(marker) {{
            marker.style.opacity = '1';
            marker.style.fillOpacity = '0.7';
        }});
        
        currentHighlight = null;

        if (window.histogramChart) {{
            window.histogramChart.data.datasets[0].backgroundColor = histogramData.map(d => d.color);
            window.histogramChart.update();
        }}
    }}

    function fadeColor(hexColor, opacity) {{
        var r = parseInt(hexColor.slice(1,3), 16);
        var g = parseInt(hexColor.slice(3,5), 16);
        var b = parseInt(hexColor.slice(5,7), 16);
        return `rgba(${{r}},${{g}},${{b}},${{opacity}})`;
    }}

    function createHistogram() {{
        // Calculate width based on number of bars
        const numBars = histogramData.length;
        const minWidth = 300;
        const widthPerBar = 30;  // Minimum width needed per bar
        const calculatedWidth = Math.max(minWidth, numBars * widthPerBar);
        
        // Update container and canvas size
        const container = document.querySelector('.histogram-content');
        container.style.width = calculatedWidth + 'px';
        const canvas = document.getElementById('histogramCanvas');
        canvas.width = calculatedWidth;
        
        // Update container position if it gets too wide
        const histContainer = document.querySelector('.histogram-container');
        if (calculatedWidth > minWidth) {{
            histContainer.style.right = '10px';
            histContainer.style.bottom = '10px';
            histContainer.style.maxWidth = '80vw';  // Limit to 80% of viewport width
            histContainer.style.overflowX = 'auto';
        }}

        var ctx = document.getElementById('histogramCanvas').getContext('2d');
        const maxCount = Math.max(...histogramData.map(d => d.count));
        const yAxisMax = Math.ceil(maxCount * 1.2);

        window.histogramChart = new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: histogramData.map(d => d.date_str),
                datasets: [{{
                    data: histogramData.map(d => d.count),
                    backgroundColor: histogramData.map(d => d.color),
                    borderColor: 'rgba(0,0,0,0.2)',
                    borderWidth: 1
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                layout: {{
                    padding: {{
                        top: 20
                    }}
                }},
                plugins: {{
                    legend: {{
                        display: false
                    }},
                    tooltip: {{
                        callbacks: {{
                            title: function(tooltipItems) {{
                                return 'Date: ' + tooltipItems[0].label;
                            }},
                            label: function(context) {{
                                return 'Count: ' + context.raw;
                            }}
                        }}
                    }},
                    datalabels: {{
                        color: '#000',
                        font: {{
                            weight: 'bold',
                            size: 12
                        }},
                        formatter: function(value) {{
                            return value;
                        }},
                        anchor: 'end',
                        align: 'top',
                        offset: 4,
                        clamp: true
                    }}
                }},
                onClick: (event, elements) => {{
                    if (elements.length > 0) {{
                        const index = elements[0].index;
                        highlightDate(histogramData[index].date_str);
                    }} else {{
                        resetHighlight();
                    }}
                }},
                scales: {{
                    y: {{
                        beginAtZero: true,
                        max: yAxisMax,
                        title: {{
                            display: true,
                            text: '{label} Images'
                        }},
                        ticks: {{
                            padding: 5
                        }}
                    }},
                    x: {{
                        display: true,
                        title: {{
                            display: true,
                            text: 'Capture Date',
                            padding: {{
                                top: 25  // Increase padding to avoid collision
                            }}
                        }},
                        ticks: {{
                            maxRotation: 45,
                            minRotation: 45,   
                        }}
                    }}
                }}
            }}
        }});
    }}

    // In the DOMContentLoaded handler:
    document.addEventListener('DOMContentLoaded', function() {{
        var checkInterval = setInterval(function() {{
            if (window.Chart) {{
                clearInterval(checkInterval);
                setTimeout(function() {{
                    console.log('Setting up event listeners');
                    createHistogram();
                    
                    // Log all interactive markers first
                    const allMarkers = document.querySelectorAll('.leaflet-interactive');
                    console.log('All interactive markers:', allMarkers.length);
                    /* allMarkers.forEach((marker, i) => {{
                        console.log(`\nMarker ${{i}} details:`);
                        console.log('- Classes:', marker.className);
                        console.log('- Tag name:', marker.tagName);
                        console.log('- Attributes:', Array.from(marker.attributes).map(attr => `${{attr.name}}="${{attr.value}}"`).join(', '));
                        console.log('- HTML:', marker.outerHTML);
                        console.log('- Path data:', marker.getAttribute('d'));
                    }});*/
                    const gsvMarkers = getGSVMarkers();
                    console.log('Found GSV markers for setup:', gsvMarkers.length);
                    //console.log('Marker data length:', markerData.length);

                    gsvMarkers.forEach(function(marker, index) {{
                        marker.addEventListener('click', function(e) {{
                            console.log('Marker clicked:', e.target);
                            if (index < markerData.length) {{
                                const date = markerData[index].date;
                                console.log('Found date:', date);
                                if (date) {{
                                    highlightDate(date);
                                }}
                            }}
                        }});
                    }});

                    var map = document.querySelector('.folium-map');
                    if (map) {{
                        map.addEventListener('click', function(e) {{
                            if (!e.target.closest('.leaflet-interactive') && 
                                !e.target.closest('#histogramCanvas')) {{
                                resetHighlight();
                            }}
                        }});
                    }}
                }}, 1000);
            }}
        }}, 100);
    }});
    </script>
    """
    folium_map.get_root().html.add_child(folium.Element(histogram_js))

    # Add colormap legend
    folium_map.add_child(colormap)

    return folium_map


def plot_status_distribution(df: pd.DataFrame, city_name: str, figsize: tuple = (10, 6)) -> None:
    """
    Draw a bar plot showing the distribution of different API response status types.

    Args:
        df: DataFrame containing the GSV metadata
        city_name: Name of the city for the plot title
        figsize: Tuple of (width, height) for the plot
    """
    plt.figure(figsize=figsize)

    # Create bar plot using seaborn
    ax = sns.countplot(x="status", data=df)

    # Customize the plot
    plt.title(f"Distribution of Status Occurrences in {city_name}")
    plt.xlabel("Status")
    plt.ylabel("Count")

    # Add count labels on top of bars
    for p in ax.patches:
        ax.annotate(
            f"{int(p.get_height())}",
            (p.get_x() + p.get_width() / 2.0, p.get_height()),
            ha="center",
            va="bottom",
        )

    # Rotate x-axis labels for better readability
    plt.xticks(rotation=45, ha="right")

    # Adjust layout to prevent label cutoff
    plt.tight_layout()

    plt.show()


def _plottable_dated_rows(df: pd.DataFrame, provider: str = "gsv") -> pd.DataFrame:
    """
    Rows a temporal plot can honestly draw: status OK, with a capture date that
    parses AND could be true (issue #213).

    Deliberately does NOT dedupe by pano_id, unlike analysis.dated_unique_panos:
    these plots have always counted rows (pano references, one per grid point
    that saw the pano), and narrowing that here would silently change what every
    existing histogram means. Only impossible dates are removed.
    """
    rows = df[df["status"] == "OK"].copy()
    rows["capture_date"] = pd.to_datetime(rows["capture_date"], errors="coerce")
    keep = plausible_capture_mask(rows["capture_date"], pd.Timestamp(datetime.now()), provider)
    dropped = int((rows["capture_date"].notna() & ~keep).sum())
    if dropped:
        logger.warning(f"Excluded {dropped} pano(s) whose capture date cannot be true")
    return rows[keep.to_numpy()]


def plot_temporal_distribution(
    df: pd.DataFrame,
    city_name: str,
    figsize: tuple = (12, 6),
    bin_freq: str = "M",  # 'M' for month, 'Y' for year, etc.
    color: str = "blue",
    kde: bool = False,
    provider: str = "gsv",
) -> None:
    """
    Create a histogram showing the distribution of GSV images over time.

    Args:
        df: DataFrame containing the GSV metadata
        city_name: Name of the city for the plot title
        figsize: Tuple of (width, height) for the plot
        bin_freq: Frequency for binning dates ('M' for monthly, 'Y' for yearly)
        color: Color for the histogram bars
        kde: Whether to show the kernel density estimation curve
        provider: imagery provider, selecting the earliest plausible capture
            date (see analysis.plausible_capture_mask)
    """
    # Filter for successful panos with a date that could be true: one corrupt
    # contributor EXIF date (issue #213) sets the x-axis range for the whole
    # plot, squeezing every real capture into a single bin.
    valid_data = _plottable_dated_rows(df, provider)

    if len(valid_data) == 0:
        logger.warning("No valid data for temporal distribution plot")
        return

    # Create figure and axes
    fig, ax = plt.subplots(figsize=figsize)

    # Create the histogram
    sns.histplot(data=valid_data, x="capture_date", bins=30, kde=kde, color=color, ax=ax)

    # Customize the plot
    ax.set_title(f"Distribution of Street View Images Over Time in {city_name}")
    ax.set_xlabel("Capture Date")
    ax.set_ylabel("Number of Images")

    # Format x-axis to show dates nicely
    if bin_freq == "M":
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    else:
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # Add count labels on top of bars
    for p in ax.patches:
        ax.annotate(
            f"{int(p.get_height())}",
            (p.get_x() + p.get_width() / 2.0, p.get_height()),
            ha="center",
            va="bottom",
        )

    # Rotate and align the tick labels so they look better
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

    # Use a tight layout to prevent label cutoff
    plt.tight_layout()

    plt.show()


def create_summary_visualization(df: pd.DataFrame, city_name: str, provider: str = "gsv") -> None:
    """
    Create a comprehensive statistical visualization including status distribution
    and temporal distribution.

    Args:
        df: DataFrame containing the GSV metadata
        city_name: Name of the city being analyzed
        provider: imagery provider, selecting the earliest plausible capture
            date (see analysis.plausible_capture_mask)
    """
    # Create a figure with two subplots
    plt.figure(figsize=(15, 6))

    # Add status distribution subplot
    plt.subplot(121)
    ax1 = sns.countplot(x="status", data=df)
    plt.title(f"Status Distribution in {city_name}")
    plt.xlabel("Status")
    plt.ylabel("Count")
    plt.xticks(rotation=45, ha="right")

    # Add count labels
    for p in ax1.patches:
        ax1.annotate(
            f"{int(p.get_height())}",
            (p.get_x() + p.get_width() / 2.0, p.get_height()),
            ha="center",
            va="bottom",
        )

    # Add temporal distribution subplot (impossible dates excluded, #213)
    plt.subplot(122)
    valid_data = _plottable_dated_rows(df, provider)

    if len(valid_data) > 0:
        sns.histplot(data=valid_data, x="capture_date", bins=30, color="blue")
        plt.title(f"Temporal Distribution in {city_name}")
        plt.xlabel("Capture Date")
        plt.ylabel("Number of Images")
        plt.xticks(rotation=45, ha="right")

    # Adjust layout
    plt.tight_layout()
    plt.show()


# Example usage:
"""
# Create individual plots
plot_status_distribution(df, "Waunakee")
plot_temporal_distribution(df, "Waunakee")

# Or create a summary visualization
create_summary_visualization(df, "Waunakee")
"""
