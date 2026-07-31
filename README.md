# Sentinel IAT · Motor histórico de anomalías territoriales

Aplicación Streamlit enfocada exclusivamente en análisis satelital sobre un área marcada.

## Funciones

- Comparación A/B con mosaicos multiescena.
- Galería de todas las escenas utilizadas en cada comparación.
- Serie histórica anual desde 2016 hasta el año actual.
- Mosaico visual por año con la misma ventana estacional.
- NDVI, NDMI, NBR y BSI por año.
- Detección de la transición interanual con mayor cambio.
- Exportación Excel, GeoJSON, JSON, PNG y paquetes ZIP con imágenes y metadatos.

## Ejecución

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Fuente

Microsoft Planetary Computer STAC, colección `sentinel-2-l2a`.

## Advertencia

El sistema prioriza cambios territoriales para revisión. No identifica causas ni confirma fosas, delitos o hallazgos periciales.
