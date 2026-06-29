# --- Affichage de la carte ---
# Toutes les variables nécessaires sont maintenant définies
import streamlit as st
import geopandas as gpd
import plotly.graph_objects as go
import plotly.io as pio
import osmnx as ox
import json
import pandas as pd
import rasterio
from rasterio.mask import mask
import numpy as np
import glob
import os
import matplotlib.pyplot as plt
from rasterio.plot import show
import tempfile
import zipfile
from io import BytesIO
import shutil


# Indique un user agent personnalisé pour respecter les règles d'usage de Nominatim
ox.settings.default_user_agent = "RISE-IN App - aclaveau@seitiss.com"

ox.settings.nominatim_delay = 1  # délai de 1 seconde pour respecter la limite
ox.settings.nominatim_endpoint = "https://nominatim.openstreetmap.org/"



# st.plotly_chart(fig, use_container_width=True)  # Removed because 'fig' is not defined yet

# Bouton d'export Shapefile (à placer après la définition des variables nécessaires)
# Ajoutez ce bloc juste après la création de toutes les variables (après la figure Plotly)


# -----------------------------------------
# 1. Contour administratif de Cesena via OSM

##Barentin

# --- IMPORTS ---
import os
import glob
import streamlit as st
import plotly.graph_objects as go
import osmnx as ox
import json
import pandas as pd
import geopandas as gpd
import numpy as np
import base64

# --- CONFIGURATION STREAMLIT ---
st.set_page_config(page_title="RISE-IN - Analyse Eau de Pluie", layout="wide")

# --- LOGO EN HAUT À GAUCHE ---
logo_path = os.path.join(os.path.dirname(__file__), "newasys.jpg")
if os.path.exists(logo_path):
    with open(logo_path, "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()
    st.markdown(
        f"""
        <div style='position: fixed; bottom: 20px; right: 30px; z-index: 9999; margin:0; padding:0;'>
            <img src='data:image/jpeg;base64,{logo_b64}' style='height:90px; border-radius:10px; margin:0; padding:0; display:block;'/>
        </div>
        """,
        unsafe_allow_html=True
    )

st.sidebar.markdown("""
**Tool presentation**

This tool provides a preliminary assessment of rainwater harvesting potential at the city scale. Developed within the framework of the European project [RISE-IN](https://www.rise-in.eu/), it only requires entering a [city name and its Wikidata code](https://www.openstreetmap.org/) to automatically retrieve open data and estimate both water demand and rooftop rainwater potential. It is designed as a fast-assessment interface to support early decision-making and feasibility discussions.

**Methodology**

The tool automatically connects to OpenStreetMap to extract geospatial features such as buildings, industrial areas, green spaces, roads and other land-use elements displayed on the map. It also integrates CHELSA climate data to retrieve local precipitation values. Based on these datasets, the tool identifies roof surfaces and rainfall intensity, which are then used to estimate potential rainwater recovery and compare it with local water needs.

**Expected results**

The output provides indicative values of water demand and rainwater recovery potential for the selected city, highlighting possible water-saving impacts. These results are not intended to replace detailed engineering studies, but to guide first estimations, compare scenarios, and prioritize territories with the highest potential.
""")


st.title("Rainwater harvesting and reuse potential analysis tool")

import gdown

# --- Configuration du téléchargement Google Drive ---
# Remplace cet ID par celui de ton vrai fichier partagé
file_id = "13AF33Ig93hPAKoy5p5EugPwJX7vmp0vP"
output_zip = os.path.join(tempfile.gettempdir(), "donnees_pluie.zip")
extraction_path = os.path.join(tempfile.gettempdir(), "donnees_pluie")

# --- Téléchargement (si pas déjà fait) ---
if not os.path.exists(output_zip):
    st.info("⬇️ Downloading rain data from Google Drive...")
    url = f"https://drive.google.com/uc?id={file_id}&export=download"
    gdown.download(url, output_zip, quiet=False)

# --- Extraction avec barre de chargement ---
if not os.path.exists(extraction_path):
    st.info("📦 Extracting rain data...")
    with zipfile.ZipFile(output_zip, 'r') as zip_ref:
        file_list = zip_ref.namelist()
        total_files = len(file_list)
        progress_bar = st.progress(0)
        os.makedirs(extraction_path, exist_ok=True)

        for i, file in enumerate(file_list):
            zip_ref.extract(file, extraction_path)
            progress_bar.progress((i + 1) / total_files)

    st.success("✅ Rain data extracted successfully.")
else:
    st.info("✅ Rain data already ready.")


# --- SAISIE UTILISATEUR ---
default_city = "Barentin"
default_wikidata = "Q578638"
if 'city_name' not in st.session_state:
    st.session_state['city_name'] = default_city
if 'wikidata' not in st.session_state:
    st.session_state['wikidata'] = default_wikidata

city_name = st.text_input("Name of the city to analyze", value=st.session_state['city_name'], key="city_name_input")
wikidata = st.text_input("Wikidata code", value=st.session_state['wikidata'], key="wikidata_input")

if city_name != st.session_state['city_name'] or wikidata != st.session_state['wikidata']:
    st.session_state['city_name'] = city_name
    st.session_state['wikidata'] = wikidata

# --- ANALYSE AU CLIC ---
if st.button("Start analysis"):
    try:
        # --- RÉCUPÉRATION DES CONTOURS ADMINISTRATIFS ---
        tags_boundary = {"boundary": "administrative"}
        boundaries = ox.features_from_place(city_name, tags=tags_boundary)
        if wikidata:
            cesena_boundary = boundaries[(boundaries["wikidata"] == wikidata)].to_crs(epsg=4326)
        else:
            cesena_boundary = boundaries.to_crs(epsg=4326)
        if cesena_boundary.empty:
            st.error("❌ Impossible to find the city with these parameters.")
            st.stop()
        cesena_polyline = cesena_boundary.explode(index_parts=False).geometry.values[0].exterior

        # --- GRILLE 500m SUR LA ZONE ---
        from shapely.geometry import box
        zone_proj = cesena_boundary.to_crs(epsg=3857)
        bounds = zone_proj.total_bounds  # xmin, ymin, xmax, ymax
        xmin, ymin, xmax, ymax = bounds
        cell_size = 500  # mètres
        grid_cells = []
        x = xmin
        while x < xmax:
            y = ymin
            while y < ymax:
                grid_cells.append(box(x, y, x + cell_size, y + cell_size))
                y += cell_size
            x += cell_size
        grid = gpd.GeoDataFrame({'geometry': grid_cells}, crs='EPSG:3857')
        # Intersecter avec la zone pour ne garder que les mailles dans la commune
        grid = gpd.overlay(grid, zone_proj, how='intersection')
        grid = grid.to_crs(epsg=4326)
        grid["id"] = grid.index.astype(str)

        # --- IMPORT DES ROUTES OSM ---
        tags_routes = {"highway": True}
        cesena_geom = cesena_boundary.geometry.union_all()
        routes = ox.features_from_polygon(cesena_geom, tags_routes)
        # (info supprimée)
        routes = routes[routes.geometry.type.isin(["LineString", "MultiLineString"])]
        if routes.empty:
            st.warning("Aucune route OSM trouvée pour la zone d'étude. Vérifie la couverture OSM ou le polygone utilisé.")
        routes = routes.to_crs(epsg=3857)

        # --- CALCUL DE LA SURFACE DES ROUTES PAR MAILLE ---
        # Buffer de 5m autour des routes pour approximer la surface (largeur moyenne)
        routes_buffered = routes.copy()
        routes_buffered["geometry"] = routes_buffered.geometry.buffer(5)
        # Pour chaque maille, calculer la surface des routes présentes
        grid_proj = grid.to_crs(epsg=3857)
        grid_proj["surface_routes_m2"] = 0.0
        for idx, cell in grid_proj.iterrows():
            # Intersection avec les routes
            inter = routes_buffered[routes_buffered.intersects(cell.geometry)]
            if not inter.empty:
                # Calculer la surface totale des buffers dans la maille
                inter_clip = inter.copy()
                inter_clip["geometry"] = inter_clip.geometry.intersection(cell.geometry)
                grid_proj.at[idx, "surface_routes_m2"] = inter_clip.geometry.area.sum()
        # Reprojection pour Plotly
        grid_plot = grid_proj.to_crs(epsg=4326)
        grid_plot["id"] = grid_plot.index.astype(str)
        geojson_grid = json.loads(grid_plot.to_json())

        # --- COUCHE PLOTLY GRILLE COLORÉE ---
        grid_trace = go.Choroplethmapbox(
            geojson=geojson_grid,
            locations=grid_plot["id"],
            featureidkey="properties.id",
            z=grid_plot["surface_routes_m2"],
            colorscale="YlOrRd",
            marker_line_width=0.5,
            marker_line_color="black",
            marker_opacity=0.5,
            showscale=False,
            name="Area with high potential water demand for street washing",
            showlegend=True
        )

        # --- COUCHE PLOTLY ROUTES ---
        routes_plot = routes.to_crs(epsg=4326)
        # Regrouper tous les segments de routes en une seule trace Scattermapbox
        lons_all = []
        lats_all = []
        for geom in routes_plot.geometry:
            if geom.is_empty or geom is None:
                continue
            if geom.geom_type == "LineString":
                lons, lats = geom.xy
                lons_all.extend(list(lons) + [None])
                lats_all.extend(list(lats) + [None])
            elif geom.geom_type == "MultiLineString":
                for part in geom.geoms:
                    if part.is_empty or part is None:
                        continue
                    lons, lats = part.xy
                    lons_all.extend(list(lons) + [None])
                    lats_all.extend(list(lats) + [None])
        route_trace = go.Scattermapbox(
            lon=lons_all,
            lat=lats_all,
            mode="lines",
            line=dict(width=4, color="#0033FF"),
            name="Roads",
            showlegend=True
        )
    except Exception as e:
        st.error(f"❌ Une erreur est survenue dans la première analyse : {e}")
    try:
        # --- DÉTECTION AUTOMATIQUE DES FICHIERS PLUIE ---
        script_dir = os.path.dirname(os.path.abspath(__file__))
        chemin_dossier = extraction_path
        if not os.path.exists(chemin_dossier):
            st.error(f"❌ Le dossier 'Données pluie' n'existe pas : {chemin_dossier}")
            st.stop()
        tif_paths = sorted(set(
            glob.glob(os.path.join(extraction_path, "**", "*.tif"), recursive=True) +
            glob.glob(os.path.join(extraction_path, "**", "*.TIF"), recursive=True)
        ), key=lambda x: os.path.basename(x).lower())


        if len(tif_paths) == 0:
            st.error("❌ Aucun fichier .tif/.TIF trouvé. Vérifie le nom des fichiers et leur extension.")
            st.stop()
        if len(tif_paths) < 12:
            st.warning(f"⚠️ Seulement {len(tif_paths)} fichiers trouvés. L'analyse sera faite avec les fichiers disponibles.")
        if len(tif_paths) > 12:
            st.warning(f"⚠️ {len(tif_paths)} fichiers trouvés. Seuls les 12 premiers seront utilisés.")
            tif_paths = tif_paths[:12]

        # --- RÉCUPÉRATION DES CONTOURS ADMINISTRATIFS ---
        if cesena_boundary.empty:
            st.error("❌ Impossible de trouver la commune avec ces paramètres.")
            st.stop()
        cesena_polyline = cesena_boundary.geometry.exterior.values[0]

        # --- STADES EN HERBE ---
        tags_stades = {"leisure": ["stadium", "pitch"], "surface": "grass"}
        cesena_geom = cesena_boundary.geometry.union_all()
        stades_herbe = ox.features_from_polygon(cesena_geom, tags_stades)
        stades_herbe = stades_herbe[stades_herbe.geometry.type.isin(["Polygon", "MultiPolygon"])]
        stades_herbe = stades_herbe.copy().reset_index(drop=True)
        stades_herbe["id"] = stades_herbe.index.astype(str)
        geojson_stades = json.loads(stades_herbe.to_json())

        # --- ESPACES VERTS LOCAUX ---
        tags_green = {"leisure": ["park", "garden"]}
        polygon = cesena_boundary.geometry.union_all()
        gdf = ox.features_from_polygon(polygon, tags_green)
        gdf_filtered = gdf[((gdf["leisure"] == "pitch") & (gdf.get("surface") == "grass")) | (gdf["leisure"].isin(["park", "garden"]))]
        gdf_polygons = gdf_filtered[gdf_filtered.geometry.type.isin(["Polygon", "MultiPolygon"])]
        gdf_m = gdf_polygons.to_crs(epsg=3857)
        gdf_m["surface_m2"] = gdf_m.geometry.area
        gdf_m["besoin_m3_an"] = gdf_m["surface_m2"] * 2.6 / 1000
        gdf_m["centroid"] = gdf_m.geometry.centroid
        gdf_points = gdf_m.set_geometry("centroid").to_crs(epsg=4326).copy()
        gdf_points["lon"] = gdf_points.geometry.x
        gdf_points["lat"] = gdf_points.geometry.y
        gdf_polys = gdf_m.to_crs(epsg=4326).copy().drop(columns=["centroid"])
        gdf_polys = gdf_polys.reset_index(drop=True)
        gdf_polys["id"] = gdf_polys.index.astype(str)
        geojson_espaces_verts = json.loads(gdf_polys.to_json())

        # --- CALCUL PRÉCIPITATION ANNUELLE ---
        zone = cesena_boundary
        assert not zone.empty, "❌ La géométrie de la zone est vide !"
        tif_paths_chelsa = sorted([p for p in tif_paths if "CHELSA" in os.path.basename(p)])
        if len(tif_paths_chelsa) != 12:
            st.warning(f"⚠️ {len(tif_paths_chelsa)} fichiers CHELSA trouvés, 12 attendus. Le calcul sera fait avec les fichiers disponibles.")
        masked_arrays = []
        with rasterio.open(tif_paths_chelsa[0]) as src:
            raster_crs = src.crs
        zone_proj = zone.to_crs(raster_crs)
        for path in tif_paths_chelsa:
            with rasterio.open(path) as src:
                out_image, _ = mask(src, zone_proj.geometry, crop=True)
                data = out_image[0].astype(np.float32)
                data[data == src.nodata] = np.nan
                masked_arrays.append(data)
        stacked = np.stack(masked_arrays, axis=0)
        rain_sum_pixels = np.nansum(stacked, axis=0)
        rain_mean_zone = np.mean(rain_sum_pixels[(~np.isnan(rain_sum_pixels)) & (rain_sum_pixels > 0)])

        # --- BÂTIMENTS > 1000 m² ---
        tags_buildings = {"building": True}
        cesena_geom = cesena_boundary.geometry.union_all()
        buildings = ox.features_from_polygon(cesena_geom, tags_buildings)
        buildings = buildings[buildings.geometry.type.isin(["Polygon", "MultiPolygon"])]
        buildings_m = buildings.to_crs(epsg=3857)
        buildings_m["surface_m2"] = buildings_m.geometry.area
        batiments_grands = buildings_m[buildings_m["surface_m2"] > 1000].reset_index(drop=True)
        batiments_grands = batiments_grands.to_crs(epsg=4326)
        batiments_grands["id"] = batiments_grands.index.astype(str)
        geojson_batiments = json.loads(batiments_grands.to_json())

        # --- STADES EN HERBE ---
        tags_stades = {"leisure": ["stadium", "pitch"], "surface": "grass"}
        stades_herbe = ox.features_from_polygon(cesena_geom, tags_stades)
        stades_herbe = stades_herbe[stades_herbe.geometry.type.isin(["Polygon", "MultiPolygon"])]
        stades_herbe = stades_herbe.reset_index(drop=True).to_crs(epsg=4326)
        stades_herbe["id"] = stades_herbe.index.astype(str)
        geojson_stades = json.loads(stades_herbe.to_json())

        # --- BESOIN EN EAU DES STADES ---
        stades_proj = stades_herbe.to_crs(epsg=3857)
        stades_proj["surface_m2"] = stades_proj.geometry.area
        stades_proj["besoin_m3_an"] = stades_proj["surface_m2"] * 0.57
        stades_proj["geometry"] = stades_proj.geometry.centroid
        stades_centroids = stades_proj.set_geometry("geometry").to_crs(epsg=4326)
        stades_centroids["lon"] = stades_centroids.geometry.x
        stades_centroids["lat"] = stades_centroids.geometry.y

        # --- FUSION DES BESOINS (espaces verts + stades) ---
        df_besoins_eau = gdf_points[["lat", "lon", "besoin_m3_an"]].copy()
        df_stades_besoins = stades_centroids[["lat", "lon", "besoin_m3_an"]].copy()
        df_besoins_fusionnes = pd.concat([df_besoins_eau, df_stades_besoins], ignore_index=True)

        # --- POTENTIEL DE RÉCUPÉRATION SUR TOITURE ---
        batiments_potentiel = batiments_grands.to_crs(epsg=3857).copy()
        batiments_potentiel["surface_m2"] = batiments_potentiel.geometry.area
        batiments_potentiel["potentiel_m3"] = 0.7 * rain_mean_zone * batiments_potentiel["surface_m2"] / 1000
        batiments_potentiel = batiments_potentiel.to_crs(epsg=4326)
        batiments_potentiel["id"] = batiments_potentiel.index.astype(str)
        geojson_potentiel = json.loads(batiments_potentiel.to_json())
        batiments_potentiel = batiments_potentiel.to_crs(epsg=3857)
        batiments_potentiel["geometry"] = batiments_potentiel.geometry.centroid
        batiments_potentiel = batiments_potentiel.set_geometry("geometry").to_crs(epsg=4326)
        batiments_potentiel["lon"] = batiments_potentiel.geometry.x
        batiments_potentiel["lat"] = batiments_potentiel.geometry.y
        df_potentiel_points = batiments_potentiel[["lat", "lon", "potentiel_m3"]].copy()

        # --- BÂTIMENTS > 1000 m² ---
        tags_buildings = {"building": True}
        buildings = ox.features_from_polygon(polygon, tags_buildings)
        buildings = buildings[buildings.geometry.type.isin(["Polygon", "MultiPolygon"])]
        buildings_m = buildings.to_crs(epsg=3857)
        buildings_m["surface_m2"] = buildings_m.geometry.area
        batiments_grands = buildings_m[buildings_m["surface_m2"] > 1000].copy()
        batiments_grands = batiments_grands.to_crs(epsg=4326).reset_index(drop=True)
        batiments_grands["id"] = batiments_grands.index.astype(str)
        geojson_batiments = json.loads(batiments_grands.to_json())

        # --- ZONES INDUSTRIELLES ---
        tags_industriel = {"landuse": "industrial"}
        zones_industrielles = ox.features_from_polygon(polygon, tags_industriel)
        zones_industrielles = zones_industrielles[zones_industrielles.geometry.type.isin(["Polygon", "MultiPolygon"])]
        zones_industrielles = zones_industrielles.copy().reset_index(drop=True)
        zones_industrielles["id"] = zones_industrielles.index.astype(str)
        geojson_industriel = json.loads(zones_industrielles.to_json())

        # --- ZONES URBAINES ---
        tags_urbain = {"landuse": "residential"}
        zones_urbaines = ox.features_from_polygon(polygon, tags_urbain)
        zones_urbaines = zones_urbaines[zones_urbaines.geometry.type.isin(["Polygon", "MultiPolygon"])]
        zones_urbaines = zones_urbaines.copy().reset_index(drop=True)
        zones_urbaines["id"] = zones_urbaines.index.astype(str)
        geojson_urbain = json.loads(zones_urbaines.to_json())

        # --- COUCHES PLOTLY ---
        contour_osm = go.Scattermapbox(
            lat=list(cesena_polyline.coords.xy[1]),
            lon=list(cesena_polyline.coords.xy[0]),
            mode="lines",
            line=dict(width=2, color="red"),
            name="Administrative boundary",
            showlegend=True
        )
        batiments_trace = go.Choroplethmapbox(
            geojson=geojson_batiments,
            locations=batiments_grands["id"],
            featureidkey="properties.id",
            z=[1]*len(batiments_grands),
            showscale=False,
            marker_line_width=0.5,
            marker_line_color="black",
            marker_opacity=0.9,
            colorscale=[[0, "#444"], [1, "#444"]],
            name="Buildings > 1000 m²",
            showlegend=True
        )
        contours_verts = go.Choroplethmapbox(
            geojson=geojson_espaces_verts,
            locations=gdf_polys["id"],
            featureidkey="properties.id",
            z=[1]*len(gdf_polys),
            showscale=False,
            marker_line_width=1,
            marker_line_color="green",
            marker_opacity=0.5,
            colorscale=[[0, "rgba(0,255,0,0.3)"], [1, "rgba(0,255,0,0.3)"]],
            name="Green spaces",
            showlegend=True
        )
        stades_trace = go.Choroplethmapbox(
            geojson=geojson_stades,
            locations=stades_herbe["id"],
            featureidkey="properties.id",
            z=[1]*len(stades_herbe),
            showscale=False,
            marker_opacity=0.7,
            marker_line_color="darkgreen",
            marker_line_width=1,
            colorscale=[[0, "rgba(0,128,0,0.5)"], [1, "rgba(0,128,0,0.5)"]],
            name="Grass fields",
            showlegend=True
        )
        points_besoins = go.Scattermapbox(
            lat=df_besoins_fusionnes["lat"],
            lon=df_besoins_fusionnes["lon"],
            mode="markers",
            marker=go.scattermapbox.Marker(
                size=df_besoins_fusionnes["besoin_m3_an"],
                sizemode="area",
                sizeref=2.*df_besoins_fusionnes["besoin_m3_an"].max()/(30**2),
                color='red',
                opacity=0.7
            ),
            text=[f"Besoin : {int(val):,} m³/an" for val in df_besoins_fusionnes["besoin_m3_an"]],
            hoverinfo="text",
            name="Water demand for irrigation and sports fields",
            showlegend=True
        )
        points_potentiel = go.Scattermapbox(
            lat=df_potentiel_points["lat"],
            lon=df_potentiel_points["lon"],
            mode="markers",
            marker=go.scattermapbox.Marker(
                size=df_potentiel_points["potentiel_m3"],
                sizemode="area",
                sizeref=2.*df_potentiel_points["potentiel_m3"].max()/(30**2),
                color='blue',
                opacity=0.7
            ),
            text=[f"Récupérable : {int(val):,} m³/an" for val in df_potentiel_points["potentiel_m3"]],
            hoverinfo="text",
            name="Rooftop rainwater harvesting potential",
            showlegend=True
        )
        industriel_trace = go.Choroplethmapbox(
            geojson=geojson_industriel,
            locations=zones_industrielles["id"],
            featureidkey="properties.id",
            z=[1]*len(zones_industrielles),
            showscale=False,
            marker_line_width=1,
            marker_line_color="gray",
            marker_opacity=0.4,
            colorscale=[[0, "orange"], [1, "orange"]],
            name="Industrial area",
            showlegend=True
        )
        urbain_trace = go.Choroplethmapbox(
            geojson=geojson_urbain,
            locations=zones_urbaines["id"],
            featureidkey="properties.id",
            z=[1]*len(zones_urbaines),
            showscale=False,
            marker_opacity=0.4,
            marker_line_color="darkviolet",
            marker_line_width=1,
            colorscale=[[0, "violet"], [1, "violet"]],
            name="Urban areas",
            showlegend=True
        )
        heatmap = go.Densitymapbox(
            lat=df_besoins_fusionnes["lat"],
            lon=df_besoins_fusionnes["lon"],
            z=df_besoins_fusionnes["besoin_m3_an"],
            radius=30,
            colorscale="YlOrRd",
            zmin=0,
            zmax=df_besoins_fusionnes["besoin_m3_an"].quantile(0.95),
            showscale=False,
            name="Carte de chaleur des besoins en eau",
            showlegend=True
        )

        # --- FIGURE PLOTLY ---
        fig = go.Figure([
            grid_trace,
            batiments_trace,
            contours_verts,
            stades_trace,
            points_besoins,
            points_potentiel,
            industriel_trace,
            contour_osm,
            route_trace
        ])
        fig.update_layout(
            mapbox=dict(
                style="open-street-map",
                zoom=13,
                center=dict(lat=gdf_points["lat"].mean(), lon=gdf_points["lon"].mean())
            ),
            title="Rainwater harvesting and reuse potential of " + city_name,
            margin=dict(l=0, r=0, t=40, b=0),
            showlegend=True,
            legend_title="Elements"
        )
        #st.plotly_chart(fig, use_container_width=True, key=f"main_map_{city_name}_{wikidata}")

        st.session_state["fig"] = fig
        st.session_state["grid_plot"] = grid_plot
        st.session_state["routes_plot"] = routes_plot
        st.session_state["batiments_grands"] = batiments_grands
        st.session_state["gdf_polys"] = gdf_polys
        st.session_state["stades_herbe"] = stades_herbe
        st.session_state["zones_industrielles"] = zones_industrielles

    except Exception as e:
        st.error(f"❌ Une erreur est survenue : {e}")

if "fig" in st.session_state:
    st.plotly_chart(
        st.session_state["fig"],
        use_container_width=True,
        config={"responsive": True}
    )

    # Ne plus passer par un bouton intermédiaire
    export_layers = {
        "grille": st.session_state["grid_plot"],
        "routes": st.session_state["routes_plot"],
        "batiments_grands": st.session_state["batiments_grands"],
        "espaces_verts": st.session_state["gdf_polys"],
        "stades_herbe": st.session_state["stades_herbe"],
        "zones_industrielles": st.session_state["zones_industrielles"]
    }

    layer_labels = {
        "grille": "Grid cells",
        "routes": "Roads",
        "batiments_grands": "Buildings > 1000 m²",
        "espaces_verts": "Green spaces",
        "stades_herbe": "Grass fields",
        "zones_industrielles": "Industrial zones"
    }
    # Formulaire toujours visible
    with st.form("form_export_shp"):
        selected_layers = [
            name for name in export_layers
            if st.checkbox(layer_labels[name], value=True, key=f"chk_{name}")
        ]
        export_clicked = st.form_submit_button("Export")

    if export_clicked and selected_layers:
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, "couches_exportées.zip")
        try:
            with zipfile.ZipFile(zip_path, "w") as zipf:
                for name in selected_layers:
                    gdf = export_layers[name]
                    layer_dir = os.path.join(temp_dir, name)
                    os.makedirs(layer_dir, exist_ok=True)
                    shp_path = os.path.join(layer_dir, f"{name}.shp")
                    gdf.to_file(shp_path, driver="ESRI Shapefile")

                    for ext in [".shp", ".shx", ".dbf", ".prj"]:
                        file_path = os.path.join(layer_dir, f"{name}{ext}")
                        if os.path.exists(file_path):
                            arcname = f"{name}/{name}{ext}"
                            zipf.write(file_path, arcname=arcname)

            with open(zip_path, "rb") as f:
                zip_data = f.read()

            st.download_button(
                label="📦 Télécharger les couches sélectionnées (.zip)",
                data=zip_data,
                file_name="export_shapefiles.zip",
                mime="application/zip"
            )
        finally:
            shutil.rmtree(temp_dir)
