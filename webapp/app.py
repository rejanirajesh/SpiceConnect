from pathlib import Path
import json

import folium
import geopandas as gpd
import pandas as pd
import streamlit as st
from branca.colormap import LinearColormap
from streamlit_folium import st_folium

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data" / "processed"
CROP_FILE = DATA_DIR / "spiceconnect_crop_logistics_final.gpkg"
ROUTE_FILE = DATA_DIR / "kerala_block_to_kochi_port_routes.gpkg"
CROP_LAYER = "crop_logistics"
ROUTE_LAYER = "block_to_kochi_port_routes"

st.set_page_config(page_title="AgriConnect | Kerala Crop Logistics", page_icon="SC", layout="wide")
st.title("AgriConnect")
st.subheader("Kerala Crop Production & Transport Explorer")
st.caption("Explore statewide crop production and the estimated road route from a selected block to Kochi Port.")

if not CROP_FILE.exists() or not ROUTE_FILE.exists():
    st.error("Could not find the required GeoPackages:\n\n"
             f"- {CROP_FILE}\n- {ROUTE_FILE}")
    st.stop()


@st.cache_data(show_spinner="Loading AgriConnect data...")
def load_data(crop_path: str, route_path: str):
    crops = gpd.read_file(crop_path, layer=CROP_LAYER)
    routes = gpd.read_file(route_path, layer=ROUTE_LAYER)

    for frame, block_col in ((crops, "block_name"), (routes, "block")):
        frame[block_col] = frame[block_col].astype(str).str.strip()
        frame["district"] = frame["district"].astype(str).str.strip()
        frame["_block_key"] = frame[block_col].str.casefold()
        frame["_district_key"] = frame["district"].str.casefold()

    crops["crop"] = crops["crop"].astype(str).str.strip()
    crops["production"] = pd.to_numeric(crops["production"], errors="coerce")
    for col in ("distance_km", "travel_time_hours"):
        crops[col] = pd.to_numeric(crops[col], errors="coerce")
        routes[col] = pd.to_numeric(routes[col], errors="coerce")
    return crops, routes


@st.cache_data(show_spinner=False)
def crop_map_payload(crop_path: str, crop_name: str):
    """
    Cache a statewide block layer joined to the selected crop.
    Blocks without a record for this crop remain visible and are styled as no-data.
    """
    all_rows = gpd.read_file(crop_path, layer=CROP_LAYER)
    all_rows["crop"] = all_rows["crop"].astype(str).str.strip()
    all_rows["block_name"] = all_rows["block_name"].astype(str).str.strip()
    all_rows["district"] = all_rows["district"].astype(str).str.strip()
    all_rows["_block_key"] = all_rows["block_name"].str.casefold()
    all_rows["_district_key"] = all_rows["district"].str.casefold()
    all_rows["production"] = pd.to_numeric(all_rows["production"], errors="coerce")

    # One polygon per district/block gives us a statewide base, even where this crop
    # has no corresponding record. The geometry and source files are not modified.
    base = all_rows[
        ["block_name", "district", "_block_key", "_district_key", "geometry"]
    ].drop_duplicates(subset=["_district_key", "_block_key"]).copy()

    selected = all_rows[all_rows["crop"] == crop_name][
        ["_block_key", "_district_key", "production", "unit"]
    ].drop_duplicates(subset=["_district_key", "_block_key"]).copy()

    frame = base.merge(
        selected,
        on=["_district_key", "_block_key"],
        how="left",
        validate="one_to_one",
    )
    frame = gpd.GeoDataFrame(frame, geometry="geometry", crs=all_rows.crs)

    # Display fields make absent records explicit in hover tooltips.
    frame["production_display"] = frame["production"].map(
        lambda value: f"{value:,.3f}" if pd.notna(value) else "No data recorded"
    )
    frame["unit_display"] = frame["unit"].fillna("—").astype(str)

    # Simplification is only for display. Original geometries and values are unchanged.
    frame["geometry"] = frame.geometry.simplify(0.0004, preserve_topology=True)
    bounds = [float(v) for v in frame.total_bounds]
    values = frame["production"].dropna().astype(float)
    if values.empty:
        low, high = 0.0, 1.0
        quantile_breaks = []
    else:
        low, high = float(values.min()), float(values.max())
        if low == high:
            high = low + 1.0
        quantile_breaks = values.quantile([0.20, 0.40, 0.60, 0.80]).tolist()
    units = sorted(frame["unit"].dropna().astype(str).unique().tolist())
    unit = units[0] if units else ""
    return frame.to_json(), bounds, low, high, unit, quantile_breaks




try:
    crop_gdf, route_gdf = load_data(str(CROP_FILE), str(ROUTE_FILE))
except Exception as exc:
    st.error(f"Unable to read the GeoPackages: {exc}")
    st.stop()

crop_names = sorted(crop_gdf["crop"].dropna().unique().tolist())
if not crop_names:
    st.error("No crop names were found in the crop layer.")
    st.stop()

st.sidebar.header("Explore the data")
selected_crop = st.sidebar.selectbox("Crop", crop_names)

# The district and block selectors use ALL known blocks, not only blocks with a
# record for the chosen crop. This lets the map show missing crop records explicitly.
crop_data = crop_gdf[crop_gdf["crop"] == selected_crop].copy()
districts = sorted(crop_gdf["district"].dropna().unique().tolist())
selected_district = st.sidebar.selectbox("District", districts)

district_data = crop_gdf[
    crop_gdf["_district_key"] == selected_district.casefold()
]
blocks = sorted(district_data["block_name"].dropna().unique().tolist())
if not blocks:
    st.warning("No blocks were found in this district.")
    st.stop()
selected_block = st.sidebar.selectbox("Block", blocks)

selected_row = crop_data[
    (crop_data["_district_key"] == selected_district.casefold()) &
    (crop_data["_block_key"] == selected_block.casefold())
]
selected_row = selected_row.iloc[0] if not selected_row.empty else None

# Metrics are derived from the saved data; routing is never recalculated.
try:
    crop_json, crop_bounds, vmin, vmax, map_unit, quantile_breaks = crop_map_payload(str(CROP_FILE), selected_crop)
    # Filter the route GeoPackage already loaded and cached above. Do not reread the file
    # every time a different block is selected.
    selected_route = route_gdf[
        (route_gdf["_district_key"] == selected_district.casefold()) &
        (route_gdf["_block_key"] == selected_block.casefold())
    ].copy()
    if selected_route.empty:
        selected_route = route_gdf[
            route_gdf["_block_key"] == selected_block.casefold()
        ].copy()

    if selected_route.empty:
        route_json, route_bounds, route_info = None, None, None
    else:
        route_info_row = selected_route.iloc[0]
        # Simplify only the displayed route geometry; keep original route metrics unchanged.
        selected_route["geometry"] = selected_route.geometry.simplify(0.001, preserve_topology=True)
        route_bounds = [float(v) for v in selected_route.total_bounds]
        route_json = selected_route.to_json()
        route_info = {
            "distance_km": float(route_info_row["distance_km"]) if pd.notna(route_info_row["distance_km"]) else None,
            "travel_time_hours": float(route_info_row["travel_time_hours"]) if pd.notna(route_info_row["travel_time_hours"]) else None,
            "snap_warning": str(route_info_row.get("snap_warning", "OK")),
            "status": str(route_info_row.get("status", "OK")),
            "block": str(route_info_row["block"]),
            "district": str(route_info_row["district"]),
        }
except Exception as exc:
    st.error(f"Could not prepare map layers: {exc}")
    st.stop()

production = selected_row.get("production") if selected_row is not None else None
unit = str(selected_row.get("unit", map_unit)).strip() if selected_row is not None else map_unit
m1, m2, m3 = st.columns(3)
m1.metric(
    f"{selected_crop} production",
    f"{production:,.3f} {unit}" if production is not None and pd.notna(production) else "No data recorded",
)
m2.metric("Road distance to Kochi Port",
          f"{route_info['distance_km']:,.1f} km" if route_info and route_info["distance_km"] is not None else "Not available")
m3.metric("Estimated travel time",
          f"{route_info['travel_time_hours']:.2f} hours" if route_info and route_info["travel_time_hours"] is not None else "Not available")

if route_info:
    if (route_info["snap_warning"].casefold() not in {"ok", "", "none", "nan"}
            or route_info["status"].casefold() not in {"ok", "", "none", "nan"}):
        st.warning(
            f"Route quality flag: snap warning = {route_info['snap_warning']}; "
            f"status = {route_info['status']}. Treat route metrics as estimates."
        )

st.markdown("### Statewide production map and selected route")

bounds = crop_bounds
center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
m = folium.Map(location=center, zoom_start=7, tiles="OpenStreetMap", control_scale=True)

# The same statewide crop range is used regardless of district selection.
# Quantile classes use the selected crop's production values across all Kerala.
# Colors run from near-white/pink to strong red; grey is reserved for missing data.
palette = ["#fff5f5", "#fca5a5", "#f87171", "#ef4444", "#ff0000"]


def production_color(value):
    if value is None:
        return "#d1d5db"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "#d1d5db"
    if pd.isna(number):
        return "#d1d5db"
    # Map to quintile class. Duplicate quantile boundaries can occur when values tie;
    # searchsorted handles those ties without crashing (some colors may be unused).
    import bisect
    idx = bisect.bisect_left(quantile_breaks, number)
    return palette[min(idx, len(palette) - 1)]

selected_block_key = selected_block.casefold()
selected_district_key = selected_district.casefold()

def style_block(feature):
    props = feature["properties"]
    raw_value = props.get("production")
    fill = production_color(raw_value)
    chosen = (
        str(props.get("_block_key", "")).casefold() == selected_block_key
        and str(props.get("_district_key", "")).casefold() == selected_district_key
    )
    return {
        "fillColor": fill,
        "color": "#111827",
        "weight": 2.0 if chosen else 0.75,
        "fillOpacity": 0.88 if chosen else 0.78,
    }

crop_geojson = json.loads(crop_json)
folium.GeoJson(
    data=crop_geojson,
    name=f"{selected_crop} production",
    style_function=style_block,
    tooltip=folium.GeoJsonTooltip(
        fields=["block_name", "district", "production_display", "unit_display"],
        aliases=["Block:", "District:", "Production:", "Unit:"],
        localize=True,
        sticky=False,
    ),
    popup=folium.GeoJsonPopup(
        fields=["block_name", "district", "production_display", "unit_display"],
        aliases=["Block:", "District:", "Production:", "Unit:"],
        localize=True,
    ),
).add_to(m)

if route_json and route_info:
    folium.GeoJson(
        data=json.loads(route_json),
        name=f"{selected_block} to Kochi Port",
        style_function=lambda feature: {"color": "#2563eb", "weight": 5, "opacity": 0.98},
        tooltip=folium.GeoJsonTooltip(
            fields=["block", "district", "distance_km", "travel_time_hours"],
            aliases=["Origin block:", "District:", "Distance (km):", "Estimated time (hours):"],
            localize=True,
        ),
    ).add_to(m)
else:
    st.info("No saved route matched this block and district.")

legend_items = [
    ("Q1 · Lowest", palette[0]),
    ("Q2 · Low", palette[1]),
    ("Q3 · Middle", palette[2]),
    ("Q4 · High", palette[3]),
    ("Q5 · Highest", palette[4]),
    ("No data recorded", "#d1d5db"),
]
legend_html = """
<div style="position:fixed;bottom:26px;left:26px;z-index:9999;background:#fff;
 padding:10px 12px;border:1px solid #999;border-radius:5px;color:#222;font-size:12px;
 box-shadow:0 1px 5px rgba(0,0,0,.25);">
 <div style="font-weight:700;margin-bottom:6px;">%s production · quantiles</div>%s
</div>
""" % (
    selected_crop,
    "".join(
        f'<div style="display:flex;align-items:center;margin:3px 0;">'
        f'<span style="display:inline-block;width:13px;height:13px;background:{color};'
        f'border:1px solid #555;margin-right:7px;"></span>{label}</div>'
        for label, color in legend_items
    ),
)
m.get_root().html.add_child(folium.Element(legend_html))
folium.LayerControl(collapsed=True).add_to(m)

# Zoom to the selected route plus the selected block's polygon.
# A route to Kochi may span much of Kerala, so this includes the full route when possible.
if route_bounds:
    minx = min(route_bounds[0], crop_bounds[0])
    miny = min(route_bounds[1], crop_bounds[1])
    maxx = max(route_bounds[2], crop_bounds[2])
    maxy = max(route_bounds[3], crop_bounds[3])
    # Fit primarily to route bounds; statewide polygon bounds are only fallback.
    m.fit_bounds([[route_bounds[1], route_bounds[0]], [route_bounds[3], route_bounds[2]]], padding=(24, 24))
else:
    m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

st_folium(m, use_container_width=True, height=650, returned_objects=[])

st.caption(
    "Production colors use a consistent statewide scale for the selected crop. "
    "Distances and times are estimates from the saved road-network routes, not live traffic predictions. "
    "The mapped origin represents the block/route origin, not a verified processing-centre location."
)

with st.expander("About this view"):
    st.write(
        "The map always shows production for the selected crop across Kerala. "
        "District selection filters the block selector but does not change the statewide color scale. "
        "The selected block's saved route is highlighted in red. Map geometries are simplified for display only; "
        "the original GeoPackages and calculated metrics are unchanged."
    )
