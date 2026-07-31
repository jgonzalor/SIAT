# Sentinel IAT · Serie Histórica Anual v5

Aplicación Streamlit enfocada exclusivamente en el análisis multitemporal anual de imágenes Sentinel-2 L2A.

## Flujo

1. Dibujar un polígono o rectángulo de estudio.
2. Elegir el año inicial, año final y una ventana estacional común.
3. Consultar escenas Sentinel-2 en Microsoft Planetary Computer.
4. Descartar nubes, sombras y nieve con la capa SCL.
5. Construir mosaicos anuales y calcular NDVI, NDMI, NBR y BSI.
6. Comparar transiciones anuales y detectar polígonos de cambio.
7. Revisar el mapa final y exportar el resultado a KMZ.

## Mejoras de esta versión

- Se eliminó la comparación A/B y cualquier módulo ajeno a la serie histórica.
- El análisis respeta el interior exacto del polígono dibujado.
- Selector de mapa de referencia: Esri World Imagery u OpenStreetMap.
- El mapa final permite activar los mosaicos Sentinel-2 del primer y último año.
- Capas separadas para anomalías acumuladas y anomalías por transición anual.
- KMZ con área exacta, anomalías acumuladas y carpetas por transición anual.

## Ejecución

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Fuente analítica

- STAC: Microsoft Planetary Computer
- Colección: `sentinel-2-l2a`
- Datos: Copernicus Sentinel-2 L2A

Los resultados priorizan áreas para revisión y no constituyen por sí solos una conclusión pericial o causal.
