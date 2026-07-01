import os
import glob
import json
import zipfile
import tempfile
import shutil
import base64

import streamlit as st
import geopandas as gpd
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import osmnx as ox
import rasterio
from rasterio.mask import mask
from shapely.geometry import box, MultiPolygon, Polygon, mapping
import requests
import gdown
from urllib.parse import quote
from shapely.geometry import LinearRing
from shapely.ops import unary_union, polygonize
from shapely.geometry import MultiLineString


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
ox.settings.default_user_agent = "RISE-IN App - aclaveau@seitiss.com"
ox.settings.nominatim_delay = 1
ox.settings.nominatim_endpoint = "https://nominatim.openstreetmap.org/"

st.set_page_config(page_title="RISE-IN - Analyse Eau de Pluie", layout="wide")

# ─────────────────────────────────────────────
# LOGO
# ─────────────────────────────────────────────
logo_path = os.path.join(os.path.dirname(__file__), "newasys.jpg")
if os.path.exists(logo_path):
    with open(logo_path, "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()
    st.markdown(
        f"""<div style='position:fixed;bottom:20px;right:30px;z-index:9999;'>
            <img src='data:image/jpeg;base64,{logo_b64}'
                 style='height:90px;border-radius:10px;display:block;'/>
        </div>""",
        unsafe_allow_html=True
    )

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
st.sidebar.markdown("""
**Tool presentation**

This tool provides a preliminary assessment of rainwater harvesting potential at the city scale.
Developed within the framework of the European project [RISE-IN](https://www.rise-in.eu/), it only
requires entering a [Wikidata code](https://www.wikidata.org/) to automatically retrieve open data
and estimate both water demand and rooftop rainwater potential. It is designed as a fast-assessment
interface to support early decision-making and feasibility discussions.

**Methodology**

The tool connects to OpenStreetMap (via the Overpass API) to extract geospatial features such as
buildings, industrial areas, green spaces, roads and other land-use elements. It also integrates
CHELSA climate data to retrieve local precipitation values.

**Expected results**

The output provides indicative values of water demand and rainwater recovery potential for the
selected area, highlighting possible water-saving impacts. These results are not intended to replace
detailed engineering studies, but to guide first estimations and compare scenarios.
""")

st.title("Rainwater harvesting and reuse potential analysis tool")

# ─────────────────────────────────────────────
# PLUIE : TÉLÉCHARGEMENT / EXTRACTION (cached)
# ─────────────────────────────────────────────
FILE_ID = "13AF33Ig93hPAKoy5p5EugPwJX7vmp0vP"
OUTPUT_ZIP = os.path.join(tempfile.gettempdir(), "donnees_pluie.zip")
EXTRACTION_PATH = os.path.join(tempfile.gettempdir(), "donnees_pluie")

if not os.path.exists(OUTPUT_ZIP):
    st.info("⬇️ Downloading rain data from Google Drive…")
    gdown.download(f"https://drive.google.com/uc?id={FILE_ID}&export=download",
                   OUTPUT_ZIP, quiet=False)

if not os.path.exists(EXTRACTION_PATH):
    st.info("📦 Extracting rain data…")
    with zipfile.ZipFile(OUTPUT_ZIP, "r") as z:
        files = z.namelist()
        prog = st.progress(0)
        os.makedirs(EXTRACTION_PATH, exist_ok=True)
        for i, f in enumerate(files):
            z.extract(f, EXTRACTION_PATH)
            prog.progress((i + 1) / len(files))
    st.success("✅ Rain data ready.")
else:
    st.info("✅ Rain data already ready.")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def boundary_from_wikidata(wikidata_id: str) -> gpd.GeoDataFrame:
    """
    Fetch an OSM relation boundary using its Wikidata tag via the Overpass API.
    Works for ANY relation type (administrative, natural, park, basin…)
    and handles MultiPolygon correctly.
    """
    query = f"""[out:json][timeout:60];
relation["wikidata"="{wikidata_id}"];
out geom;"""

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "RISE-IN App - aclaveau@seitiss.com",
        "Accept": "application/json",
    }

    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ]

    data = None
    last_error = None
    for url in mirrors:
        try:
            resp = requests.post(
                url,
                data=f"data={quote(query)}",
                headers=headers,
                timeout=90
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("elements"):
                break
        except Exception as e:
            last_error = e
            continue

    if data is None:
        raise ValueError(f"All Overpass mirrors failed. Last error: {last_error}")
    if not data.get("elements"):
        raise ValueError(f"No OSM relation found for Wikidata ID: {wikidata_id}")

    elements = data["elements"]
    rel = elements[0]


    # Collecter les segments outer/inner comme LineStrings (pas besoin qu'ils soient fermés)
    outer_lines = []
    inner_lines = []
    for member in rel.get("members", []):
        if member.get("type") != "way" or "geometry" not in member:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in member["geometry"]]
        if len(coords) < 2:
            continue
        role = member.get("role", "outer")
        if role == "outer":
            outer_lines.append(coords)
        elif role == "inner":
            inner_lines.append(coords)

    if not outer_lines:
        raise ValueError("No outer ring found in the relation geometry.")

    # Utiliser polygonize pour assembler les segments dans le bon ordre
    from shapely.geometry import LineString
    outer_polys = list(polygonize(unary_union([LineString(c) for c in outer_lines])))
    inner_polys = list(polygonize(unary_union([LineString(c) for c in inner_lines]))) if inner_lines else []

    if not outer_polys:
        raise ValueError("Could not polygonize outer rings.")

    merged_outer = unary_union(outer_polys).buffer(0)  # buffer(0) corrige les géométries invalides
    if inner_polys:
        merged_inner = unary_union(inner_polys).buffer(0)
        geom = merged_outer.difference(merged_inner)
    else:
        geom = merged_outer
    tags = rel.get("tags", {})
    gdf = gpd.GeoDataFrame(
        [{"geometry": geom, **tags}],
        crs="EPSG:4326"
    )
    return gdf


def boundary_to_polylines(gdf: gpd.GeoDataFrame):
    """Return a list of (lons, lats) tuples for all exterior rings (handles MultiPolygon)."""
    lines = []
    for geom in gdf.geometry:
        if geom.geom_type == "Polygon":
            ext = geom.exterior
            lons, lats = ext.xy
            lines.append((list(lons), list(lats)))
        elif geom.geom_type == "MultiPolygon":
            for part in geom.geoms:
                ext = part.exterior
                lons, lats = ext.xy
                lines.append((list(lons), list(lats)))
    return lines


@st.cache_data(show_spinner=False)
def fetch_osm_features(geom_wkt: str, tags: dict, geom_types: list):
    from shapely import wkt as shapely_wkt
    geom = shapely_wkt.loads(geom_wkt)
    try:
        gdf = ox.features_from_polygon(geom, tags)
        gdf = gdf[gdf.geometry.type.isin(geom_types)].copy().reset_index(drop=True)
        return gdf
    except Exception:
        st.warning(f"⚠️ OSM fetch failed for tags {tags}: {e}")
        return gpd.GeoDataFrame()


@st.cache_data(show_spinner=False)
def compute_rain_mean(geom_wkt: str, tif_paths: list):
    """Cached mean annual precipitation over the zone."""
    from shapely import wkt as shapely_wkt
    from shapely.geometry import mapping
    geom = shapely_wkt.loads(geom_wkt)
    gdf_zone = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")

    tif_paths_chelsa = sorted([p for p in tif_paths if "CHELSA" in os.path.basename(p)])
    if not tif_paths_chelsa:
        tif_paths_chelsa = tif_paths[:12]

    with rasterio.open(tif_paths_chelsa[0]) as src:
        raster_crs = src.crs
    zone_proj = gdf_zone.to_crs(raster_crs)

    masked_arrays = []
    for path in tif_paths_chelsa:
        with rasterio.open(path) as src:
            out_image, _ = mask(src, zone_proj.geometry, crop=True)
            data = out_image[0].astype(np.float32)
            data[data == src.nodata] = np.nan
            masked_arrays.append(data)

    stacked = np.stack(masked_arrays, axis=0)
    rain_sum = np.nansum(stacked, axis=0)
    valid = rain_sum[(~np.isnan(rain_sum)) & (rain_sum > 0)]
    return float(np.mean(valid)) if len(valid) > 0 else 0.0


def gdf_to_choroplethmapbox(gdf, color, name, opacity=0.5, line_color="black", lw=0.5):
    if gdf is None or gdf.empty:
        return go.Scattermapbox(lat=[], lon=[], mode="markers", name=name, showlegend=True)
    gdf = gdf.copy().reset_index(drop=True)
    gdf["_id"] = gdf.index.astype(str)
    geojson = json.loads(gdf.to_json())
    return go.Choroplethmapbox(
        geojson=geojson,
        locations=gdf["_id"],
        featureidkey="properties._id",
        z=[1] * len(gdf),
        showscale=False,
        marker_opacity=opacity,
        marker_line_color=line_color,
        marker_line_width=lw,
        colorscale=[[0, color], [1, color]],
        name=name,
        showlegend=True
    )


def make_zip_from_gdfs(layers: dict) -> bytes:
    """Serialize a dict of {name: GeoDataFrame} to a zip of Shapefiles."""
    tmp = tempfile.mkdtemp()
    zip_path = os.path.join(tmp, "export.zip")
    try:
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, gdf in layers.items():
                if gdf is None or gdf.empty:
                    continue
                layer_dir = os.path.join(tmp, name)
                os.makedirs(layer_dir, exist_ok=True)
                shp_path = os.path.join(layer_dir, f"{name}.shp")
                # ensure exportable (no list columns)
                gdf_export = gdf.copy()
                for col in gdf_export.columns:
                    if col == "geometry":
                        continue
                    if gdf_export[col].dtype == object:
                        try:
                            gdf_export[col] = gdf_export[col].astype(str)
                        except Exception:
                            gdf_export = gdf_export.drop(columns=[col])
                gdf_export.to_file(shp_path, driver="ESRI Shapefile")
                for ext in [".shp", ".shx", ".dbf", ".prj"]:
                    fp = os.path.join(layer_dir, f"{name}{ext}")
                    if os.path.exists(fp):
                        zf.write(fp, arcname=f"{name}/{name}{ext}")
        with open(zip_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────────────────────
# SAISIE UTILISATEUR
# ─────────────────────────────────────────────
st.markdown("""
Enter the **Wikidata ID** of any geographic area (city, municipality, park, watershed…).
Find the ID on [wikidata.org](https://www.wikidata.org) — it starts with **Q** followed by digits.
Examples: `Q578638` (Barentin), `Q90` (Paris), `Q456` (Cesena)
""")

col1, col2 = st.columns([2, 1])
with col1:
    city_label = st.text_input("Area label (display only)", value="Barentin")
with col2:
    wikidata_id = st.text_input("Wikidata ID", value="Q578638")

run_analysis = st.button("▶ Start analysis", type="primary")

# ─────────────────────────────────────────────
# ANALYSE
# ─────────────────────────────────────────────
if run_analysis:
    st.session_state.pop("results", None)   # reset

    with st.spinner("🔍 Fetching zone boundary from OSM…"):
        try:
            zone_gdf = boundary_from_wikidata(wikidata_id)
        except Exception as e:
            st.error(f"❌ Could not retrieve boundary for Wikidata ID `{wikidata_id}`: {e}")
            st.stop()

    zone_geom = zone_gdf.geometry.union_all()
    zone_geom_wkt = zone_geom.wkt   # hashable for cache
    zone_proj_gdf = zone_gdf.to_crs(epsg=3857)

    # ── DONNÉES PLUIE ─────────────────────────
    tif_paths = sorted(set(
        glob.glob(os.path.join(EXTRACTION_PATH, "**", "*.tif"), recursive=True) +
        glob.glob(os.path.join(EXTRACTION_PATH, "**", "*.TIF"), recursive=True)
    ), key=lambda x: os.path.basename(x).lower())

    if not tif_paths:
        st.error("❌ No .tif rainfall files found.")
        st.stop()

    with st.spinner("🌧️ Computing mean annual rainfall…"):
        rain_mean = compute_rain_mean(zone_geom_wkt, tuple(tif_paths))

    # ── OSM FEATURES (parallélisées) ───
    from concurrent.futures import ThreadPoolExecutor, as_completed

    osm_queries = {
        "buildings":   ({"building": True},                       ["Polygon", "MultiPolygon"]),
        "roads":       ({"highway": True},                        ["LineString", "MultiLineString"]),
        "green":       ({"leisure": ["park", "garden", "pitch"]}, ["Polygon", "MultiPolygon"]),
        "stades":      ({"leisure": ["stadium", "pitch"], "surface": "grass"}, ["Polygon", "MultiPolygon"]),
        "industrial":  ({"landuse": "industrial"},                ["Polygon", "MultiPolygon"]),
        "residential": ({"landuse": "residential"},               ["Polygon", "MultiPolygon"]),
    }

    with st.spinner("🗺️ Fetching OSM features in parallel…"):
        osm_results = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_osm_features, zone_geom_wkt, tags, geom_types): name
                for name, (tags, geom_types) in osm_queries.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    osm_results[name] = future.result()
                except Exception as e:
                    st.warning(f"⚠️ Failed to fetch {name}: {e}")
                    osm_results[name] = gpd.GeoDataFrame()

    buildings_raw       = osm_results["buildings"]
    routes_raw          = osm_results["roads"]
    green_raw           = osm_results["green"]
    stades_raw          = osm_results["stades"]
    zones_industrielles = osm_results["industrial"]
    zones_urbaines      = osm_results["residential"]

    # Reprojections
    buildings_m = buildings_raw.to_crs(epsg=3857)
    buildings_m["surface_m2"] = buildings_m.geometry.area
    batiments_grands = buildings_m[buildings_m["surface_m2"] > 1000].copy().to_crs(epsg=4326).reset_index(drop=True)

    routes_proj = routes_raw.to_crs(epsg=3857)

    if "leisure" in green_raw.columns and not green_raw.empty:
        parks = green_raw[green_raw["leisure"].isin(["park", "garden"])]
        pitches = green_raw[
            (green_raw["leisure"] == "pitch") &
            (green_raw.get("surface", pd.Series(dtype=str)) == "grass")
        ] if "surface" in green_raw.columns else green_raw.iloc[0:0]
        green_filtered = pd.concat([parks, pitches]).reset_index(drop=True)
    else:
        green_filtered = green_raw

    green_m = green_filtered.to_crs(epsg=3857)
    green_m["surface_m2"] = green_m.geometry.area
    green_m["besoin_m3_an"] = green_m["surface_m2"] * 2.6 / 1000
    gdf_polys = green_m.to_crs(epsg=4326).reset_index(drop=True)

    stades_m = stades_raw.to_crs(epsg=3857)
    stades_m["surface_m2"] = stades_m.geometry.area
    stades_m["besoin_m3_an"] = stades_m["surface_m2"] * 0.57
    stades_herbe = stades_m.to_crs(epsg=4326).reset_index(drop=True)

    # ── GRILLE 500m ────────────────────────────
    with st.spinner("🔲 Building grid and road surfaces…"):
        bounds = zone_proj_gdf.total_bounds
        xmin, ymin, xmax, ymax = bounds
        cell_size = 500
        grid_cells = [
            box(x, y, x + cell_size, y + cell_size)
            for x in np.arange(xmin, xmax, cell_size)
            for y in np.arange(ymin, ymax, cell_size)
        ]
        grid = gpd.GeoDataFrame({"geometry": grid_cells}, crs="EPSG:3857")
        grid = gpd.overlay(grid, zone_proj_gdf[["geometry"]], how="intersection")

        # Surface routes par maille
        routes_buf = routes_proj.copy()
        routes_buf["geometry"] = routes_buf.geometry.buffer(5)
        routes_union = routes_buf.geometry.union_all()

        grid["surface_routes_m2"] = grid.geometry.apply(
            lambda cell: routes_union.intersection(cell).area
        )
        grid_plot = grid.to_crs(epsg=4326).reset_index(drop=True)
        grid_plot["_id"] = grid_plot.index.astype(str)

    # ── POTENTIEL / BESOINS ────────────────────
    # Potentiel toitures
    bat_pot = batiments_grands.to_crs(epsg=3857).copy()
    bat_pot["surface_m2"] = bat_pot.geometry.area
    bat_pot["potentiel_m3"] = 0.7 * rain_mean * bat_pot["surface_m2"] / 1000
    bat_pot_pts = bat_pot.copy()
    bat_pot_pts["geometry"] = bat_pot_pts.geometry.centroid
    bat_pot_pts = bat_pot_pts.set_geometry("geometry").to_crs(epsg=4326)
    bat_pot_pts["lon"] = bat_pot_pts.geometry.x
    bat_pot_pts["lat"] = bat_pot_pts.geometry.y
    bat_pot_layer = bat_pot.to_crs(epsg=4326).reset_index(drop=True)   # pour export

    # Besoins espaces verts
    gdf_pts = gdf_polys.copy().to_crs(epsg=3857)
    gdf_pts["geometry"] = gdf_pts.geometry.centroid
    gdf_pts = gdf_pts.set_geometry("geometry").to_crs(epsg=4326)
    gdf_pts["lon"] = gdf_pts.geometry.x
    gdf_pts["lat"] = gdf_pts.geometry.y

    # Besoins stades
    stades_pts = stades_herbe.to_crs(epsg=3857).copy()
    stades_pts["geometry"] = stades_pts.geometry.centroid
    stades_pts = stades_pts.set_geometry("geometry").to_crs(epsg=4326)
    stades_pts["lon"] = stades_pts.geometry.x
    stades_pts["lat"] = stades_pts.geometry.y

    df_besoins = pd.concat(
        [gdf_pts[["lat", "lon", "besoin_m3_an"]],
         stades_pts[["lat", "lon", "besoin_m3_an"]]],
        ignore_index=True
    )

    # ── COUCHE BESOINS EAU (polygones avec valeur) ─
    besoin_layer = pd.concat([
        gdf_polys[["geometry", "surface_m2", "besoin_m3_an"]],
        stades_herbe[["geometry", "surface_m2", "besoin_m3_an"]]
    ], ignore_index=True).reset_index(drop=True)
    besoin_layer = gpd.GeoDataFrame(besoin_layer, crs="EPSG:4326")

    # ── TRACES PLOTLY ──────────────────────────

    # Contour (multipolygon safe)
    contour_traces = []
    for lons, lats in boundary_to_polylines(zone_gdf):
        contour_traces.append(go.Scattermapbox(
            lat=lats, lon=lons,
            mode="lines",
            line=dict(width=2, color="red"),
            name="Zone boundary",
            showlegend=len(contour_traces) == 0,
            legendgroup="boundary"
        ))

    # Routes
    lons_all, lats_all = [], []
    for geom in routes_raw.geometry:
        if geom is None or geom.is_empty:
            continue
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        for p in parts:
            xs, ys = p.xy
            lons_all += list(xs) + [None]
            lats_all += list(ys) + [None]
    route_trace = go.Scattermapbox(
        lon=lons_all, lat=lats_all,
        mode="lines",
        line=dict(width=2, color="#0033FF"),
        name="Roads", showlegend=True
    )

    # Grille routes
    geojson_grid = json.loads(grid_plot.to_json())
    grid_trace = go.Choroplethmapbox(
        geojson=geojson_grid,
        locations=grid_plot["_id"],
        featureidkey="properties._id",
        z=grid_plot["surface_routes_m2"],
        colorscale="YlOrRd",
        marker_line_width=0.5, marker_line_color="black",
        marker_opacity=0.5, showscale=False,
        name="Street washing demand (road surface/cell)",
        showlegend=True
    )

    bat_trace = gdf_to_choroplethmapbox(batiments_grands, "#444444",
                                         "Buildings > 1000 m²", opacity=0.9)
    green_trace = gdf_to_choroplethmapbox(gdf_polys, "rgba(0,200,0,0.3)",
                                           "Green spaces", line_color="green")
    stades_trace = gdf_to_choroplethmapbox(stades_herbe, "rgba(0,128,0,0.5)",
                                            "Grass fields", line_color="darkgreen", opacity=0.7)
    indus_trace = gdf_to_choroplethmapbox(zones_industrielles, "orange",
                                           "Industrial areas", opacity=0.4, line_color="gray")
    urban_trace = gdf_to_choroplethmapbox(zones_urbaines, "violet",
                                           "Urban/residential areas", opacity=0.4, line_color="darkviolet")

    sref_besoins = (2. * df_besoins["besoin_m3_an"].max() / (30 ** 2)
                    if not df_besoins.empty and df_besoins["besoin_m3_an"].max() > 0 else 1)
    sref_pot = (2. * bat_pot_pts["potentiel_m3"].max() / (30 ** 2)
                if not bat_pot_pts.empty and bat_pot_pts["potentiel_m3"].max() > 0 else 1)

    points_besoins = go.Scattermapbox(
        lat=df_besoins["lat"], lon=df_besoins["lon"],
        mode="markers",
        marker=go.scattermapbox.Marker(
            size=df_besoins["besoin_m3_an"], sizemode="area",
            sizeref=sref_besoins, color="red", opacity=0.7
        ),
        text=[f"Demand: {int(v):,} m³/yr" for v in df_besoins["besoin_m3_an"]],
        hoverinfo="text",
        name="Water demand (irrigation & sports)", showlegend=True
    )
    points_potentiel = go.Scattermapbox(
        lat=bat_pot_pts["lat"], lon=bat_pot_pts["lon"],
        mode="markers",
        marker=go.scattermapbox.Marker(
            size=bat_pot_pts["potentiel_m3"], sizemode="area",
            sizeref=sref_pot, color="blue", opacity=0.7
        ),
        text=[f"Recoverable: {int(v):,} m³/yr" for v in bat_pot_pts["potentiel_m3"]],
        hoverinfo="text",
        name="Rooftop rainwater harvesting potential", showlegend=True
    )

    # ── FIGURE ────────────────────────────────
    center_lat = zone_gdf.geometry.centroid.y.mean()
    center_lon = zone_gdf.geometry.centroid.x.mean()

    fig = go.Figure(
        contour_traces + [
            grid_trace, bat_trace, green_trace, stades_trace,
            indus_trace, urban_trace,
            points_besoins, points_potentiel,
            route_trace,
        ]
    )
    fig.update_layout(
        mapbox=dict(style="open-street-map", zoom=12,
                    center=dict(lat=center_lat, lon=center_lon)),
        title=f"Rainwater harvesting and reuse potential — {city_label}",
        margin=dict(l=0, r=0, t=40, b=0),
        showlegend=True,
        legend_title="Layers",
    )

    # ── MÉTRIQUES GLOBALES ────────────────────
    total_besoin = df_besoins["besoin_m3_an"].sum()
    total_potentiel = bat_pot_pts["potentiel_m3"].sum()
    total_bat_surface = bat_pot["surface_m2"].sum()

    # ── STOCKAGE SESSION ──────────────────────
    st.session_state["results"] = {
        "fig": fig,
        "rain_mean": rain_mean,
        "total_besoin": total_besoin,
        "total_potentiel": total_potentiel,
        "total_bat_surface": total_bat_surface,
        "city_label": city_label,
        "export_layers": {
            "grid_road_demand":       grid_plot[["geometry", "surface_routes_m2", "_id"]].rename(columns={"_id": "id"}),
            "roads":                  routes_raw.to_crs(epsg=4326),
            "buildings_over_1000m2":  batiments_grands,
            "green_spaces":           gdf_polys,
            "grass_fields":           stades_herbe,
            "industrial_zones":       zones_industrielles,
            "urban_zones":            zones_urbaines,
            "water_demand":           besoin_layer,       # ← nouveau
            "rainwater_potential":    bat_pot_layer,      # ← nouveau
        }
    }

# ─────────────────────────────────────────────
# AFFICHAGE
# ─────────────────────────────────────────────
if "results" in st.session_state:
    r = st.session_state["results"]

    # Métriques
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🌧️ Mean annual rainfall", f"{r['rain_mean']:.0f} mm")
    m2.metric("🏗️ Large roof surface", f"{r['total_bat_surface']:,.0f} m²")
    m3.metric("💧 Water demand (irrig. + sports)", f"{r['total_besoin']:,.0f} m³/yr")
    m4.metric("♻️ Rooftop harvesting potential", f"{r['total_potentiel']:,.0f} m³/yr")

    st.plotly_chart(r["fig"], use_container_width=True)

    html_buffer = r["fig"].to_html(full_html=True, include_plotlyjs="cdn", config={"scrollZoom": True})
    st.download_button(
        label="🗺️ Download map as HTML",
        data=html_buffer,
        file_name=f"RISE-IN_{r['city_label'].replace(' ', '_')}_map.html",
        mime="text/html"
    )

    # ── EXPORT ────────────────────────────────
    st.subheader("📦 Export layers")
    export_layers = r["export_layers"]

    layer_labels = {
        "grid_road_demand":       "🔲 Grid — street washing demand",
        "roads":                  "🛣️ Roads",
        "buildings_over_1000m2":  "🏗️ Buildings > 1000 m²",
        "green_spaces":           "🌳 Green spaces",
        "grass_fields":           "⚽ Grass fields",
        "industrial_zones":       "🏭 Industrial zones",
        "urban_zones":            "🏘️ Urban / residential zones",
        "water_demand":           "💧 Water demand (irrigation + sports)",
        "rainwater_potential":    "♻️ Rooftop rainwater harvesting potential",
    }

    with st.form("form_export_shp"):
        selected = {
            name: st.checkbox(label, value=(name in ("water_demand", "rainwater_potential")),
                              key=f"chk_{name}")
            for name, label in layer_labels.items()
        }
        export_clicked = st.form_submit_button("Export selected layers as Shapefile (.zip)")

    if export_clicked:
        to_export = {name: export_layers[name] for name, checked in selected.items() if checked}
        if not to_export:
            st.warning("Please select at least one layer.")
        else:
            with st.spinner("Packaging shapefiles…"):
                zip_bytes = make_zip_from_gdfs(to_export)
            st.download_button(
                label="⬇️ Download .zip",
                data=zip_bytes,
                file_name=f"RISE-IN_{r['city_label'].replace(' ', '_')}_export.zip",
                mime="application/zip"
            )
