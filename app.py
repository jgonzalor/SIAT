from __future__ import annotations

import json
from datetime import date, timedelta

import folium
import streamlit as st
from folium.plugins import Draw, Fullscreen, MeasureControl
from streamlit_folium import st_folium

from core.engine import analyze_change, report_excel

st.set_page_config(page_title="Sentinel IAT · Anomalías", page_icon="🛰️", layout="wide")

st.title("🛰️ Sentinel IAT · Motor de Detección de Anomalías")
st.caption("Comparación multitemporal de imágenes Sentinel-2 en un área marcada")
st.warning("El sistema prioriza cambios territoriales para revisión. No confirma fosas, delitos ni hallazgos periciales.")

with st.expander("¿Cómo funciona?", expanded=True):
    st.markdown("**1. Marque un área → 2. Elija dos periodos comparables → 3. Ejecute el análisis → 4. Revise las zonas priorizadas.**")
    st.caption("El motor construye mosaicos con varias escenas, filtra nubes mediante la capa SCL y combina NDVI, NDMI, NBR y BSI.")

st.subheader("1. Marque el área de estudio")
mapa = folium.Map(location=[24.8, -107.4], zoom_start=7, tiles=None, control_scale=True)
folium.TileLayer("OpenStreetMap", name="Mapa").add_to(mapa)
folium.TileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attr="Esri", name="Satélite"
).add_to(mapa)
Draw(
    export=False,
    draw_options={"rectangle": True, "polygon": True, "polyline": False, "circle": False, "circlemarker": False, "marker": False},
    edit_options={"edit": True, "remove": True},
).add_to(mapa)
Fullscreen().add_to(mapa); MeasureControl().add_to(mapa); folium.LayerControl(collapsed=False).add_to(mapa)
map_state = st_folium(mapa, height=520, width="stretch", key="mapa_anomalias")

def bbox_from_drawing(state):
    drawing = state.get("last_active_drawing") if state else None
    if not drawing or not drawing.get("geometry"):
        return None
    geom = drawing["geometry"]
    coords = geom.get("coordinates", [])
    points = coords[0] if geom.get("type") == "Polygon" and coords else []
    if not points:
        return None
    lons = [p[0] for p in points]; lats = [p[1] for p in points]
    return [min(lons), min(lats), max(lons), max(lats)]

bbox = bbox_from_drawing(map_state)
if bbox:
    st.success(f"Área marcada correctamente. Límites: {bbox[1]:.5f}, {bbox[0]:.5f} → {bbox[3]:.5f}, {bbox[2]:.5f}")
else:
    st.info("Utilice la herramienta de rectángulo o polígono del mapa para marcar el área.")

st.subheader("2. Seleccione periodos comparables")
left, right = st.columns(2)
with left:
    st.markdown("**Periodo anterior (ANTES)**")
    start_a = st.date_input("Inicio anterior", date.today() - timedelta(days=365*3+60))
    end_a = st.date_input("Fin anterior", date.today() - timedelta(days=365*3))
with right:
    st.markdown("**Periodo reciente (DESPUÉS)**")
    start_b = st.date_input("Inicio reciente", date.today() - timedelta(days=60))
    end_b = st.date_input("Fin reciente", date.today())

c1, c2, c3 = st.columns(3)
max_cloud = c1.slider("Nubosidad máxima por escena", 5, 80, 35, 5)
scenes = c2.slider("Escenas máximas por periodo", 2, 12, 8)
threshold = c3.slider("Sensibilidad de anomalía", 0.10, 0.50, 0.24, 0.01)

st.subheader("3. Ejecute la comparación")
run = st.button("🔎 Analizar cambios en el área marcada", type="primary", width="stretch", disabled=bbox is None)

if run:
    try:
        with st.status("Procesando imágenes Sentinel-2…", expanded=True) as status:
            st.write("Buscando escenas y filtrando nubosidad…")
            result = analyze_change(bbox, start_a, end_a, start_b, end_b, max_cloud, scenes, threshold=threshold)
            st.session_state["analysis_result"] = result
            status.update(label="Análisis terminado", state="complete")
    except Exception as exc:
        st.error(f"No fue posible completar el análisis: {exc}")

result = st.session_state.get("analysis_result")
if result:
    st.divider(); st.subheader("4. Resultados")
    a,b,c,d = st.columns(4)
    a.metric("Área analizada", f"{result['area_km2']:.3f} km²")
    b.metric("Área priorizada", f"{result['changed_pct']:.1f}%")
    c.metric("Superficie priorizada", f"{result['changed_km2']:.3f} km²")
    d.metric("Prioridad", result["priority"])

    st.info(result["interpretation"])
    before, after = st.columns(2)
    before.image(result["before_png"], caption="ANTES · mosaico de varias escenas", width="stretch")
    after.image(result["after_png"], caption="DESPUÉS · mosaico de varias escenas", width="stretch")
    score_col, anomaly_col = st.columns(2)
    score_col.image(result["score_png"], caption="Puntuación combinada de cambio", width="stretch")
    anomaly_col.image(result["anomaly_png"], caption="Zonas priorizadas", width="stretch")

    st.markdown("### Polígonos detectados")
    result_map = folium.Map(location=[(result['bbox'][1]+result['bbox'][3])/2, (result['bbox'][0]+result['bbox'][2])/2], zoom_start=15, tiles=None)
    folium.TileLayer("OpenStreetMap", name="Mapa").add_to(result_map)
    folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", attr="Esri", name="Satélite").add_to(result_map)
    if result["geojson"]["features"]:
        folium.GeoJson(result["geojson"], name="Áreas priorizadas", style_function=lambda _: {"color":"#ff2b2b", "weight":3, "fillColor":"#ff2b2b", "fillOpacity":0.30}).add_to(result_map)
    folium.LayerControl(collapsed=False).add_to(result_map)
    st_folium(result_map, height=520, width="stretch", key="resultado_mapa")

    with st.expander("Escenas utilizadas y diagnóstico técnico"):
        st.dataframe(result["scenes"], width="stretch", hide_index=True)
        if result["warnings"]:
            st.warning("Algunas escenas fueron descartadas durante la construcción del mosaico.")
            st.code("\n".join(result["warnings"][:20]))

    st.markdown("### Exportar")
    e1,e2,e3,e4 = st.columns(4)
    e1.download_button("Descargar informe Excel", report_excel(result), "sentinel_iat_resultado.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    e2.download_button("Descargar anomalías GeoJSON", json.dumps(result["geojson"], ensure_ascii=False, indent=2), "sentinel_iat_anomalias.geojson", "application/geo+json", width="stretch")
    e3.download_button("Descargar comparativa PNG", result["overview_png"], "sentinel_iat_comparativa.png", "image/png", width="stretch")
    e4.download_button("Descargar metadatos JSON", json.dumps(result["metadata"], ensure_ascii=False, indent=2), "sentinel_iat_metadatos.json", "application/json", width="stretch")
