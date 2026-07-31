from __future__ import annotations

import base64
from datetime import date
from typing import Any

import folium
import streamlit as st
from folium.plugins import Draw, Fullscreen, MeasureControl
from streamlit_folium import st_folium

from core.engine import (
    ARCHIVE_START_YEAR,
    analyze_historical_series,
    historical_gallery_zip,
    historical_kmz,
    historical_report_excel,
)

st.set_page_config(
    page_title="Sentinel IAT · Serie histórica anual",
    page_icon="🛰️",
    layout="wide",
)

st.title("🛰️ Sentinel IAT · Serie Histórica Anual")
st.caption(
    "Análisis multitemporal de imágenes Sentinel-2 para localizar, visualizar y exportar cambios territoriales"
)
st.warning(
    "El sistema identifica áreas prioritarias para revisión. No confirma fosas, delitos ni hallazgos periciales."
)

with st.expander("¿Cómo trabaja este módulo?", expanded=True):
    st.markdown(
        """
        1. Dibuje el **polígono exacto** que desea analizar.
        2. Seleccione los años y una misma ventana estacional para toda la serie.
        3. El motor consulta imágenes **Sentinel-2 L2A** en Microsoft Planetary Computer.
        4. Elimina nubes, sombras y nieve mediante la capa SCL.
        5. Construye un mosaico anual y calcula NDVI, NDMI, NBR y BSI.
        6. Compara los años consecutivos y localiza la transición de mayor cambio.
        7. Genera polígonos acumulados e interanuales para revisión y exportación a KMZ.

        El análisis queda restringido al interior real del polígono dibujado, no al rectángulo que lo contiene.
        """
    )


def _geometry_from_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    drawing = state.get("last_active_drawing")
    if drawing and drawing.get("geometry"):
        return drawing["geometry"]
    drawings = state.get("all_drawings") or []
    if drawings:
        geometry = drawings[-1].get("geometry")
        if geometry:
            return geometry
    return None


def _bbox_from_geometry(geometry: dict[str, Any] | None) -> list[float] | None:
    if not geometry:
        return None

    points: list[tuple[float, float]] = []

    def visit(value: Any) -> None:
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            points.append((float(value[0]), float(value[1])))
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(geometry.get("coordinates", []))
    if not points:
        return None
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    return [min(lons), min(lats), max(lons), max(lats)]


def _png_data_uri(png_bytes: bytes) -> str:
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _add_base_layers(m: folium.Map, preferred: str = "Esri World Imagery") -> None:
    folium.TileLayer(
        "OpenStreetMap",
        name="OpenStreetMap",
        show=preferred == "OpenStreetMap",
    ).add_to(m)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles © Esri and contributors",
        name="Esri World Imagery",
        show=preferred == "Esri World Imagery",
    ).add_to(m)


def _add_image_overlay(
    m: folium.Map,
    png_bytes: bytes,
    bbox: list[float],
    name: str,
    show: bool,
    opacity: float = 0.88,
) -> None:
    west, south, east, north = bbox
    folium.raster_layers.ImageOverlay(
        image=_png_data_uri(png_bytes),
        bounds=[[south, west], [north, east]],
        name=name,
        opacity=opacity,
        interactive=True,
        cross_origin=False,
        zindex=3,
        show=show,
    ).add_to(m)


def _show_scene_gallery(scene_previews: list[dict[str, Any]], year_filter: str) -> None:
    previews = scene_previews
    if year_filter != "Todos":
        previews = [row for row in previews if row.get("year") == int(year_filter)]
    if not previews:
        st.info("No hay imágenes utilizadas para el filtro seleccionado.")
        return
    columns = st.columns(3)
    for index, scene in enumerate(previews):
        capture_date = (scene.get("datetime") or "Sin fecha")[:10]
        cloud = float(scene.get("cloud_cover", 0) or 0)
        valid = float(scene.get("valid_coverage_pct", 0) or 0)
        caption = (
            f"{scene.get('year', '')} · {capture_date}\n"
            f"Nubes: {cloud:.1f}% · Cobertura útil: {valid:.1f}%\n"
            f"{scene.get('scene_id', '')}"
        )
        columns[index % 3].image(
            scene["preview_png"], caption=caption, width="stretch"
        )


historical_result = st.session_state.get("historical_result")

st.subheader("1. Dibuje el área exacta de estudio")
map_reference = st.radio(
    "Mapa de referencia para marcar el polígono",
    ["Esri World Imagery", "OpenStreetMap"],
    horizontal=True,
    help=(
        "Estas capas sirven únicamente para ubicar el área. El análisis utiliza imágenes "
        "Sentinel-2 fechadas y procesadas por el motor."
    ),
)
show_last_analysis = False
if historical_result:
    show_last_analysis = st.checkbox(
        f"Mostrar mosaico Sentinel-2 del último análisis ({historical_result['valid_years'][-1]})",
        value=False,
    )

mapa = folium.Map(
    location=[24.8, -107.4],
    zoom_start=7,
    tiles=None,
    control_scale=True,
)
_add_base_layers(mapa, preferred=map_reference)

if historical_result and show_last_analysis:
    _add_image_overlay(
        mapa,
        historical_result["annual_gallery"][-1]["composite_png"],
        historical_result["bbox"],
        f"Sentinel-2 {historical_result['valid_years'][-1]} · último análisis",
        show=True,
    )

saved_geometry = st.session_state.get("aoi_geometry")
if saved_geometry:
    folium.GeoJson(
        {"type": "Feature", "properties": {}, "geometry": saved_geometry},
        name="Polígono de estudio guardado",
        style_function=lambda _: {
            "color": "#1683ff",
            "weight": 3,
            "fillColor": "#1683ff",
            "fillOpacity": 0.10,
        },
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

map_state = st_folium(
    mapa,
    height=540,
    width="stretch",
    key="mapa_historico_anual",
)

new_geometry = _geometry_from_state(map_state)
if new_geometry:
    st.session_state["aoi_geometry"] = new_geometry

left_status, right_status = st.columns([4, 1])
with right_status:
    if st.button("Borrar área", width="stretch"):
        st.session_state.pop("aoi_geometry", None)
        st.session_state.pop("historical_result", None)
        st.rerun()

aoi_geometry = st.session_state.get("aoi_geometry")
bbox = _bbox_from_geometry(aoi_geometry)
with left_status:
    if bbox:
        st.success(
            "Polígono registrado. Límites de consulta: "
            f"{bbox[1]:.5f}, {bbox[0]:.5f} → {bbox[3]:.5f}, {bbox[2]:.5f}"
        )
    else:
        st.info("Utilice la herramienta de polígono o rectángulo para delimitar el área.")

st.subheader("2. Configure la serie histórica anual")
st.markdown(
    "Seleccione la misma temporada para cada año. Comparar, por ejemplo, junio–julio en toda "
    "la serie reduce falsos cambios producidos por lluvia, sequía o ciclos agrícolas."
)

c1, c2, c3, c4 = st.columns(4)
start_year = c1.number_input(
    "Año inicial",
    min_value=ARCHIVE_START_YEAR,
    max_value=date.today().year,
    value=ARCHIVE_START_YEAR,
    step=1,
)
end_year = c2.number_input(
    "Año final",
    min_value=ARCHIVE_START_YEAR,
    max_value=date.today().year,
    value=date.today().year,
    step=1,
)
window_start = c3.date_input(
    "Inicio de ventana anual",
    date(2024, 6, 1),
    key="hist_window_start",
)
window_end = c4.date_input(
    "Fin de ventana anual",
    date(2024, 7, 31),
    key="hist_window_end",
)

c5, c6, c7 = st.columns(3)
max_cloud = c5.slider(
    "Nubosidad máxima por escena", 5, 80, 35, 5, key="hist_cloud"
)
scenes_per_year = c6.slider(
    "Escenas máximas por año", 1, 6, 3, 1, key="hist_scenes"
)
threshold = c7.slider(
    "Sensibilidad de anomalía", 0.10, 0.50, 0.24, 0.01, key="hist_threshold"
)

years_requested = int(end_year) - int(start_year) + 1
st.caption(
    f"Procesamiento estimado: {years_requested} años × hasta {scenes_per_year} escenas = "
    f"máximo aproximado de {max(years_requested, 0) * scenes_per_year} escenas."
)

run_history = st.button(
    "🗓️ Analizar la serie histórica anual",
    type="primary",
    width="stretch",
    disabled=bbox is None or aoi_geometry is None,
)

if run_history:
    progress = st.progress(0, text="Preparando análisis histórico…")

    def update_progress(value: float, message: str) -> None:
        progress.progress(min(max(value, 0.0), 1.0), text=message)

    try:
        result = analyze_historical_series(
            bbox=bbox,
            aoi_geometry=aoi_geometry,
            start_year=int(start_year),
            end_year=int(end_year),
            window_start=window_start,
            window_end=window_end,
            max_cloud=max_cloud,
            scenes_per_year=scenes_per_year,
            threshold=threshold,
            progress_callback=update_progress,
        )
        st.session_state["historical_result"] = result
        historical_result = result
        progress.progress(1.0, text="Serie histórica terminada")
    except Exception as exc:
        progress.empty()
        st.error(f"No fue posible completar la serie histórica: {exc}")

historical_result = st.session_state.get("historical_result")
if historical_result:
    st.divider()
    st.subheader("3. Resultado de la serie histórica")
    strongest = historical_result["strongest_transition"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Área exacta", f"{historical_result['area_km2']:.3f} km²")
    m2.metric("Años válidos", len(historical_result["valid_years"]))
    m3.metric("Escenas utilizadas", len(historical_result["scene_previews"]))
    m4.metric("Mayor transición", strongest["transition"])
    m5.metric("Cambio máximo", f"{strongest['changed_pct']:.1f}%")
    st.info(historical_result["interpretation"])

    summary_tab, annual_tab, scenes_tab, technical_tab = st.tabs(
        [
            "Evolución y mayor cambio",
            "Mosaicos de todos los años",
            "Imágenes utilizadas",
            "Tablas técnicas",
        ]
    )

    with summary_tab:
        st.markdown("### Evolución de los índices espectrales")
        annual_chart = historical_result["annual"].set_index("year")
        st.line_chart(
            annual_chart[["mean_ndvi", "mean_ndmi", "mean_nbr", "mean_bsi"]],
            width="stretch",
        )
        st.markdown("### Cambio detectado entre años consecutivos")
        transition_chart = historical_result["transitions"].set_index("transition")
        st.bar_chart(transition_chart[["changed_pct"]], width="stretch")

        st.markdown(f"### Transición prioritaria: {strongest['transition']}")
        before, after = st.columns(2)
        before.image(
            historical_result["strongest_assets"]["before_png"],
            caption=f"Antes · {strongest['from_year']}",
            width="stretch",
        )
        after.image(
            historical_result["strongest_assets"]["after_png"],
            caption=f"Después · {strongest['to_year']}",
            width="stretch",
        )
        score_col, anomaly_col = st.columns(2)
        score_col.image(
            historical_result["strongest_assets"]["score_png"],
            caption="Puntuación multiespectral",
            width="stretch",
        )
        anomaly_col.image(
            historical_result["strongest_assets"]["anomaly_png"],
            caption="Áreas priorizadas",
            width="stretch",
        )

    with annual_tab:
        st.markdown("### Línea visual anual")
        st.caption(
            "Cada imagen es un mosaico mediano construido con las escenas válidas de la misma ventana estacional."
        )
        annual_columns = st.columns(3)
        for index, annual in enumerate(historical_result["annual_gallery"]):
            cloud = annual["mean_cloud_cover"]
            cloud_text = f"{cloud:.1f}%" if cloud is not None else "N/D"
            caption = (
                f"{annual['year']} · {annual['used_scenes']} escenas\n"
                f"Nubes promedio: {cloud_text} · NDVI: {annual['mean_ndvi']:.3f} · "
                f"BSI: {annual['mean_bsi']:.3f}"
            )
            annual_columns[index % 3].image(
                annual["composite_png"], caption=caption, width="stretch"
            )

    with scenes_tab:
        year_options = ["Todos"] + [
            str(year) for year in historical_result["valid_years"]
        ]
        selected_year = st.selectbox(
            "Filtrar imágenes por año", year_options, key="scene_year_filter"
        )
        _show_scene_gallery(historical_result["scene_previews"], selected_year)

    with technical_tab:
        st.markdown("### Serie anual")
        st.dataframe(historical_result["annual"], width="stretch", hide_index=True)
        st.markdown("### Transiciones interanuales")
        st.dataframe(
            historical_result["transitions"], width="stretch", hide_index=True
        )
        with st.expander("Escenas candidatas encontradas"):
            st.dataframe(
                historical_result["candidates"], width="stretch", hide_index=True
            )
        if historical_result["missing_years"]:
            st.warning(
                "No fue posible construir mosaicos para: "
                + ", ".join(map(str, historical_result["missing_years"]))
            )
        if historical_result["warnings"]:
            with st.expander("Advertencias técnicas"):
                st.code("\n".join(historical_result["warnings"][:100]))

    st.divider()
    st.subheader("4. Mapa final de polígonos y exportación KMZ")
    st.markdown(
        "El mapa combina el polígono exacto de consulta, las anomalías acumuladas y las "
        "anomalías de cada transición anual. Use el control de capas para activar o desactivar cada año."
    )

    first_year = historical_result["valid_years"][0]
    last_year = historical_result["valid_years"][-1]
    final_reference = st.selectbox(
        "Vista inicial del mapa final",
        [
            f"Sentinel-2 {last_year}",
            f"Sentinel-2 {first_year}",
            "Esri World Imagery",
            "OpenStreetMap",
        ],
    )

    bbox_result = historical_result["bbox"]
    center = [
        (bbox_result[1] + bbox_result[3]) / 2,
        (bbox_result[0] + bbox_result[2]) / 2,
    ]
    final_map = folium.Map(location=center, zoom_start=15, tiles=None, control_scale=True)
    preferred_base = (
        "OpenStreetMap" if final_reference == "OpenStreetMap" else "Esri World Imagery"
    )
    _add_base_layers(final_map, preferred=preferred_base)

    first_overlay_show = final_reference == f"Sentinel-2 {first_year}"
    last_overlay_show = final_reference == f"Sentinel-2 {last_year}"
    _add_image_overlay(
        final_map,
        historical_result["annual_gallery"][0]["composite_png"],
        bbox_result,
        f"Sentinel-2 {first_year} · mosaico utilizado",
        show=first_overlay_show,
    )
    _add_image_overlay(
        final_map,
        historical_result["annual_gallery"][-1]["composite_png"],
        bbox_result,
        f"Sentinel-2 {last_year} · mosaico utilizado",
        show=last_overlay_show,
    )

    folium.GeoJson(
        {
            "type": "Feature",
            "properties": {"nombre": "Área exacta analizada"},
            "geometry": historical_result.get("aoi_geometry"),
        },
        name="Área exacta analizada",
        style_function=lambda _: {
            "color": "#1683ff",
            "weight": 4,
            "fillColor": "#1683ff",
            "fillOpacity": 0.08,
        },
        tooltip=folium.GeoJsonTooltip(fields=["nombre"], aliases=["Capa:"]),
        show=True,
    ).add_to(final_map)

    accumulated = historical_result["accumulated_geojson"]
    if accumulated.get("features"):
        folium.GeoJson(
            accumulated,
            name=f"Anomalías acumuladas {first_year}-{last_year}",
            style_function=lambda _: {
                "color": "#ff2020",
                "weight": 3,
                "fillColor": "#ff2020",
                "fillOpacity": 0.32,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["clasificacion", "area_m2", "area_ha"],
                aliases=["Clasificación:", "Área m²:", "Área ha:"],
                localize=True,
            ),
            show=True,
        ).add_to(final_map)

    strongest_label = historical_result["strongest_transition"]["transition"]
    for label, geojson in historical_result.get("transition_geojsons", {}).items():
        if not geojson.get("features"):
            continue
        is_strongest = label == strongest_label
        folium.GeoJson(
            geojson,
            name=("★ Mayor cambio · " if is_strongest else "Cambio · ") + label,
            style_function=(
                (lambda _: {
                    "color": "#ffd000",
                    "weight": 4,
                    "fillColor": "#ffd000",
                    "fillOpacity": 0.28,
                })
                if is_strongest
                else (lambda _: {
                    "color": "#ff8c00",
                    "weight": 2,
                    "fillColor": "#ff8c00",
                    "fillOpacity": 0.18,
                })
            ),
            tooltip=folium.GeoJsonTooltip(
                fields=["transition", "area_m2", "area_ha"],
                aliases=["Transición:", "Área m²:", "Área ha:"],
                localize=True,
            ),
            show=is_strongest,
        ).add_to(final_map)

    folium.LayerControl(collapsed=False).add_to(final_map)
    Fullscreen().add_to(final_map)
    MeasureControl().add_to(final_map)
    final_map.fit_bounds(
        [[bbox_result[1], bbox_result[0]], [bbox_result[3], bbox_result[2]]]
    )
    st_folium(final_map, height=620, width="stretch", key="mapa_final_historico")

    st.markdown("### Exportar resultado")
    e1, e2, e3 = st.columns(3)
    e1.download_button(
        "🌎 Exportar polígonos a KMZ",
        historical_kmz(historical_result),
        f"sentinel_iat_serie_historica_{first_year}_{last_year}.kmz",
        "application/vnd.google-earth.kmz",
        type="primary",
        width="stretch",
        help=(
            "Incluye el área exacta, anomalías acumuladas y carpetas por transición anual para Google Earth."
        ),
    )
    e2.download_button(
        "📊 Informe histórico Excel",
        historical_report_excel(historical_result),
        f"sentinel_iat_serie_historica_{first_year}_{last_year}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )
    e3.download_button(
        "📦 Paquete completo ZIP",
        historical_gallery_zip(historical_result),
        f"sentinel_iat_evidencia_historica_{first_year}_{last_year}.zip",
        "application/zip",
        width="stretch",
    )

st.divider()
st.caption(
    "Fuente analítica: Microsoft Planetary Computer STAC · colección sentinel-2-l2a · "
    "datos Copernicus Sentinel-2 de reflectancia de superficie. Esri y OpenStreetMap se usan únicamente como referencia cartográfica."
)
