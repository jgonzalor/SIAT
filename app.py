from __future__ import annotations

import json
from datetime import date, timedelta

import folium
import pandas as pd
import streamlit as st
from folium.plugins import Draw, Fullscreen, MeasureControl
from streamlit_folium import st_folium

from core.engine import (
    ARCHIVE_START_YEAR,
    analyze_change,
    analyze_historical_series,
    comparison_gallery_zip,
    historical_gallery_zip,
    historical_report_excel,
    report_excel,
)

st.set_page_config(
    page_title="Sentinel IAT · Anomalías territoriales",
    page_icon="🛰️",
    layout="wide",
)

st.title("🛰️ Sentinel IAT · Motor de Anomalías Territoriales")
st.caption(
    "Análisis visual, multiespectral e histórico de imágenes Sentinel-2 sobre un área marcada"
)
st.warning(
    "El sistema prioriza cambios territoriales para revisión. No confirma fosas, delitos ni hallazgos periciales."
)

with st.expander("¿Qué analiza realmente?", expanded=True):
    st.markdown(
        """
        El motor consulta imágenes **Sentinel-2 L2A**, recorta exactamente el área marcada,
        elimina nubes y sombras mediante la clasificación SCL, genera vistas en color natural
        y calcula NDVI, NDMI, NBR y BSI. Puede trabajar de dos formas:

        1. **Comparación A/B:** contrasta dos periodos concretos.
        2. **Serie histórica:** construye un mosaico comparable por cada año y localiza la
           transición anual con mayor cambio.

        Las imágenes mostradas en la galería son vistas RGB derivadas de las bandas originales
        B04, B03 y B02 que sí participaron en el análisis.
        """
    )

st.subheader("1. Marque el área de estudio")
mapa = folium.Map(location=[24.8, -107.4], zoom_start=7, tiles=None, control_scale=True)
folium.TileLayer("OpenStreetMap", name="Mapa").add_to(mapa)
folium.TileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attr="Esri",
    name="Satélite de referencia",
).add_to(mapa)
Draw(
    export=False,
    draw_options={
        "rectangle": True,
        "polygon": True,
        "polyline": False,
        "circle": False,
        "circlemarker": False,
        "marker": False,
    },
    edit_options={"edit": True, "remove": True},
).add_to(mapa)
Fullscreen().add_to(mapa)
MeasureControl().add_to(mapa)
folium.LayerControl(collapsed=False).add_to(mapa)
map_state = st_folium(mapa, height=520, width="stretch", key="mapa_anomalias")


def bbox_from_drawing(state):
    drawing = state.get("last_active_drawing") if state else None
    if not drawing or not drawing.get("geometry"):
        return None
    geometry = drawing["geometry"]
    coordinates = geometry.get("coordinates", [])
    points = coordinates[0] if geometry.get("type") == "Polygon" and coordinates else []
    if not points:
        return None
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    return [min(lons), min(lats), max(lons), max(lats)]


bbox = bbox_from_drawing(map_state)
if bbox:
    st.success(
        f"Área marcada correctamente. Límites: {bbox[1]:.5f}, {bbox[0]:.5f} → "
        f"{bbox[3]:.5f}, {bbox[2]:.5f}"
    )
else:
    st.info("Utilice la herramienta de rectángulo o polígono para marcar el área.")

comparison_tab, historical_tab = st.tabs(
    ["🔎 Comparación de dos periodos", "🗓️ Serie histórica anual"]
)


def show_scene_gallery(scene_previews, key_prefix: str, year_filter=None):
    previews = scene_previews
    if year_filter not in (None, "Todos"):
        previews = [row for row in previews if row.get("year") == int(year_filter)]
    if not previews:
        st.info("No hay vistas de escenas disponibles para este filtro.")
        return
    st.caption(
        f"Se muestran {len(previews)} escenas utilizadas. Cada vista corresponde al recorte exacto del área marcada."
    )
    columns = st.columns(3)
    for index, scene in enumerate(previews):
        dt = (scene.get("datetime") or "Sin fecha")[:10]
        period = scene.get("periodo") or str(scene.get("year", ""))
        cloud = scene.get("cloud_cover", 0)
        valid = scene.get("valid_coverage_pct", 0)
        caption = (
            f"{period} · {dt}\n"
            f"Nubes catálogo: {cloud:.1f}% · Cobertura útil: {valid:.1f}%\n"
            f"{scene.get('scene_id', '')}"
        )
        columns[index % 3].image(scene["preview_png"], caption=caption, width="stretch")


with comparison_tab:
    st.subheader("2A. Seleccione dos periodos comparables")
    left, right = st.columns(2)
    with left:
        st.markdown("**Periodo anterior (ANTES)**")
        start_a = st.date_input(
            "Inicio anterior", date.today() - timedelta(days=365 * 3 + 60), key="ab_start_a"
        )
        end_a = st.date_input(
            "Fin anterior", date.today() - timedelta(days=365 * 3), key="ab_end_a"
        )
    with right:
        st.markdown("**Periodo reciente (DESPUÉS)**")
        start_b = st.date_input(
            "Inicio reciente", date.today() - timedelta(days=60), key="ab_start_b"
        )
        end_b = st.date_input("Fin reciente", date.today(), key="ab_end_b")

    c1, c2, c3 = st.columns(3)
    max_cloud = c1.slider("Nubosidad máxima por escena", 5, 80, 35, 5, key="ab_cloud")
    scenes = c2.slider("Escenas máximas por periodo", 2, 12, 8, key="ab_scenes")
    threshold = c3.slider(
        "Sensibilidad de anomalía", 0.10, 0.50, 0.24, 0.01, key="ab_threshold"
    )

    run = st.button(
        "🔎 Analizar cambios entre los dos periodos",
        type="primary",
        width="stretch",
        disabled=bbox is None,
        key="run_ab",
    )
    if run:
        try:
            with st.status("Procesando imágenes Sentinel-2…", expanded=True) as status:
                st.write("Buscando escenas candidatas y seleccionando las de menor nubosidad…")
                result = analyze_change(
                    bbox,
                    start_a,
                    end_a,
                    start_b,
                    end_b,
                    max_cloud,
                    scenes,
                    threshold=threshold,
                )
                st.session_state["analysis_result"] = result
                status.update(label="Análisis terminado", state="complete")
        except Exception as exc:
            st.error(f"No fue posible completar el análisis: {exc}")

    result = st.session_state.get("analysis_result")
    if result:
        st.divider()
        st.subheader("Resultados de la comparación A/B")
        a, b, c, d = st.columns(4)
        a.metric("Área analizada", f"{result['area_km2']:.3f} km²")
        b.metric("Área priorizada", f"{result['changed_pct']:.1f}%")
        c.metric("Superficie priorizada", f"{result['changed_km2']:.3f} km²")
        d.metric("Prioridad", result["priority"])
        st.info(result["interpretation"])

        result_summary, result_gallery, result_technical = st.tabs(
            ["Resultado visual", "Galería de escenas utilizadas", "Metadatos y exportación"]
        )
        with result_summary:
            before, after = st.columns(2)
            before.image(result["before_png"], caption="ANTES · mosaico de varias escenas", width="stretch")
            after.image(result["after_png"], caption="DESPUÉS · mosaico de varias escenas", width="stretch")
            score_col, anomaly_col = st.columns(2)
            score_col.image(result["score_png"], caption="Puntuación combinada de cambio", width="stretch")
            anomaly_col.image(result["anomaly_png"], caption="Zonas priorizadas", width="stretch")

            st.markdown("### Polígonos detectados")
            result_map = folium.Map(
                location=[
                    (result["bbox"][1] + result["bbox"][3]) / 2,
                    (result["bbox"][0] + result["bbox"][2]) / 2,
                ],
                zoom_start=15,
                tiles=None,
            )
            folium.TileLayer("OpenStreetMap", name="Mapa").add_to(result_map)
            folium.TileLayer(
                "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                attr="Esri",
                name="Satélite",
            ).add_to(result_map)
            if result["geojson"]["features"]:
                folium.GeoJson(
                    result["geojson"],
                    name="Áreas priorizadas",
                    style_function=lambda _: {
                        "color": "#ff2b2b",
                        "weight": 3,
                        "fillColor": "#ff2b2b",
                        "fillOpacity": 0.30,
                    },
                ).add_to(result_map)
            folium.LayerControl(collapsed=False).add_to(result_map)
            st_folium(result_map, height=520, width="stretch", key="resultado_mapa")

        with result_gallery:
            st.markdown("### Imágenes que participaron en el análisis")
            st.caption(
                "No son capturas de Google Earth: son vistas en color natural generadas desde las bandas Sentinel-2 originales."
            )
            show_scene_gallery(result["scene_previews"], "ab_gallery")

        with result_technical:
            st.markdown("### Escenas utilizadas")
            st.dataframe(result["scenes"], width="stretch", hide_index=True)
            with st.expander("Escenas candidatas encontradas en el catálogo"):
                st.dataframe(result["candidates"], width="stretch", hide_index=True)
            if result["warnings"]:
                st.warning("Algunas escenas candidatas fueron descartadas.")
                st.code("\n".join(result["warnings"][:30]))

            st.markdown("### Exportar")
            e1, e2, e3, e4 = st.columns(4)
            e1.download_button(
                "Informe Excel",
                report_excel(result),
                "sentinel_iat_resultado.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
            e2.download_button(
                "Paquete visual ZIP",
                comparison_gallery_zip(result),
                "sentinel_iat_evidencia_visual.zip",
                "application/zip",
                width="stretch",
            )
            e3.download_button(
                "Anomalías GeoJSON",
                json.dumps(result["geojson"], ensure_ascii=False, indent=2),
                "sentinel_iat_anomalias.geojson",
                "application/geo+json",
                width="stretch",
            )
            e4.download_button(
                "Metadatos JSON",
                json.dumps(result["metadata"], ensure_ascii=False, indent=2),
                "sentinel_iat_metadatos.json",
                "application/json",
                width="stretch",
            )

with historical_tab:
    st.subheader("2B. Construya la serie histórica completa")
    st.markdown(
        "Elija una **misma ventana estacional** para todos los años. Comparar junio–julio de cada año, "
        "por ejemplo, reduce falsos cambios causados únicamente por estaciones distintas."
    )

    h1, h2, h3, h4 = st.columns(4)
    start_year = h1.number_input(
        "Año inicial", min_value=ARCHIVE_START_YEAR, max_value=date.today().year, value=ARCHIVE_START_YEAR, step=1
    )
    end_year = h2.number_input(
        "Año final", min_value=ARCHIVE_START_YEAR, max_value=date.today().year, value=date.today().year, step=1
    )
    reference_start = h3.date_input(
        "Inicio de ventana anual", date(2024, 6, 1), key="hist_window_start"
    )
    reference_end = h4.date_input(
        "Fin de ventana anual", date(2024, 7, 31), key="hist_window_end"
    )

    h5, h6, h7 = st.columns(3)
    historical_cloud = h5.slider(
        "Nubosidad máxima histórica", 5, 80, 35, 5, key="hist_cloud"
    )
    scenes_per_year = h6.slider(
        "Escenas máximas por año", 1, 6, 3, 1, key="hist_scenes"
    )
    historical_threshold = h7.slider(
        "Sensibilidad histórica", 0.10, 0.50, 0.24, 0.01, key="hist_threshold"
    )

    years_requested = int(end_year) - int(start_year) + 1
    st.caption(
        f"Solicitud: {years_requested} años × hasta {scenes_per_year} escenas = "
        f"máximo aproximado de {years_requested * scenes_per_year} escenas procesadas."
    )

    run_history = st.button(
        "🗓️ Analizar toda la serie histórica",
        type="primary",
        width="stretch",
        disabled=bbox is None,
        key="run_history",
    )
    if run_history:
        progress = st.progress(0, text="Preparando análisis histórico…")

        def update_progress(value: float, message: str):
            progress.progress(min(max(value, 0.0), 1.0), text=message)

        try:
            historical_result = analyze_historical_series(
                bbox=bbox,
                start_year=int(start_year),
                end_year=int(end_year),
                window_start=reference_start,
                window_end=reference_end,
                max_cloud=historical_cloud,
                scenes_per_year=scenes_per_year,
                threshold=historical_threshold,
                progress_callback=update_progress,
            )
            st.session_state["historical_result"] = historical_result
            progress.progress(1.0, text="Serie histórica terminada")
        except Exception as exc:
            progress.empty()
            st.error(f"No fue posible completar la serie histórica: {exc}")

    historical_result = st.session_state.get("historical_result")
    if historical_result:
        st.divider()
        st.subheader("Resultados históricos")
        strongest = historical_result["strongest_transition"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Años con mosaico", len(historical_result["valid_years"]))
        m2.metric("Escenas utilizadas", len(historical_result["scene_previews"]))
        m3.metric("Mayor transición", strongest["transition"])
        m4.metric("Cambio máximo", f"{strongest['changed_pct']:.1f}%")
        st.info(historical_result["interpretation"])

        history_summary, annual_gallery_tab, scene_gallery_tab, history_export = st.tabs(
            [
                "Evolución y mayor cambio",
                "Mosaicos de todos los años",
                "Todas las escenas utilizadas",
                "Metadatos y exportación",
            ]
        )

        with history_summary:
            st.markdown("### Evolución de índices por año")
            annual_chart = historical_result["annual"].set_index("year")
            st.line_chart(
                annual_chart[["mean_ndvi", "mean_ndmi", "mean_nbr", "mean_bsi"]],
                width="stretch",
            )
            st.markdown("### Proporción de cambio entre años consecutivos disponibles")
            transition_chart = historical_result["transitions"].set_index("transition")
            st.bar_chart(transition_chart[["changed_pct"]], width="stretch")

            st.markdown(f"### Transición prioritaria: {strongest['transition']}")
            sb, sa = st.columns(2)
            sb.image(
                historical_result["strongest_assets"]["before_png"],
                caption=f"Antes · {strongest['from_year']}",
                width="stretch",
            )
            sa.image(
                historical_result["strongest_assets"]["after_png"],
                caption=f"Después · {strongest['to_year']}",
                width="stretch",
            )
            ss, sm = st.columns(2)
            ss.image(
                historical_result["strongest_assets"]["score_png"],
                caption="Puntuación multiespectral de la transición",
                width="stretch",
            )
            sm.image(
                historical_result["strongest_assets"]["anomaly_png"],
                caption="Áreas priorizadas de la transición",
                width="stretch",
            )

        with annual_gallery_tab:
            st.markdown("### Línea visual anual")
            st.caption(
                "Cada tarjeta es el mosaico mediano de las escenas válidas del mismo periodo estacional de ese año."
            )
            annual_columns = st.columns(3)
            for index, annual in enumerate(historical_result["annual_gallery"]):
                cloud = annual["mean_cloud_cover"]
                cloud_text = f"{cloud:.1f}%" if cloud is not None else "N/D"
                caption = (
                    f"{annual['year']} · {annual['used_scenes']} escenas\n"
                    f"Nubes promedio: {cloud_text} · NDVI: {annual['mean_ndvi']:.3f} · BSI: {annual['mean_bsi']:.3f}"
                )
                annual_columns[index % 3].image(
                    annual["composite_png"], caption=caption, width="stretch"
                )

        with scene_gallery_tab:
            years_options = ["Todos"] + [str(year) for year in historical_result["valid_years"]]
            selected_year = st.selectbox(
                "Filtrar imágenes por año", years_options, key="scene_year_filter"
            )
            show_scene_gallery(
                historical_result["scene_previews"],
                "historical_gallery",
                year_filter=selected_year,
            )

        with history_export:
            st.markdown("### Serie anual")
            st.dataframe(historical_result["annual"], width="stretch", hide_index=True)
            st.markdown("### Transiciones interanuales")
            st.dataframe(historical_result["transitions"], width="stretch", hide_index=True)
            with st.expander("Escenas candidatas encontradas"):
                st.dataframe(historical_result["candidates"], width="stretch", hide_index=True)
            if historical_result["missing_years"]:
                st.warning(
                    "No fue posible construir mosaicos para: "
                    + ", ".join(map(str, historical_result["missing_years"]))
                )
            if historical_result["warnings"]:
                with st.expander("Advertencias técnicas"):
                    st.code("\n".join(historical_result["warnings"][:100]))

            x1, x2, x3, x4 = st.columns(4)
            x1.download_button(
                "Informe histórico Excel",
                historical_report_excel(historical_result),
                "sentinel_iat_serie_historica.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
            x2.download_button(
                "Paquete histórico ZIP",
                historical_gallery_zip(historical_result),
                "sentinel_iat_evidencia_historica.zip",
                "application/zip",
                width="stretch",
            )
            x3.download_button(
                "Resumen visual PNG",
                historical_result["historical_overview_png"],
                "sentinel_iat_resumen_historico.png",
                "image/png",
                width="stretch",
            )
            x4.download_button(
                "Metadatos JSON",
                json.dumps(historical_result["metadata"], ensure_ascii=False, indent=2),
                "sentinel_iat_historico_metadatos.json",
                "application/json",
                width="stretch",
            )

st.divider()
st.caption(
    "Fuente de consulta: Microsoft Planetary Computer STAC · colección sentinel-2-l2a. "
    "Las escenas son datos Copernicus Sentinel-2 procesados a reflectancia de superficie."
)
