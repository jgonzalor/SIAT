# Sentinel IAT

**Sistema de Investigación y Análisis Territorial** desarrollado en Streamlit.

## Funciones principales

- Organizaciones, brigadas y operativos.
- Captura de recorridos, áreas, puntos, indicios y hallazgos.
- Evidencias preservadas con hash SHA-256.
- Bitácora de auditoría.
- Análisis territorial entre dos periodos con Sentinel-2.
- **Motor Multitemporal** para comparar el mismo periodo estacional entre varios años.
- Índices NDVI, NDMI, NBR y BSI.
- Búsqueda del intervalo probable del cambio.
- Clasificación temporal: sin cambio relevante, repentina, persistente, progresiva, revertida o no concluyente.
- Exportaciones Excel, JSON, CSV, GeoJSON y KMZ.

## Ejecución

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Uso del análisis histórico

1. Abra **Análisis histórico**.
2. Dibuje un rectángulo pequeño sobre el mapa.
3. Seleccione años y un mismo intervalo mensual para todos los años.
4. Ejecute el análisis.
5. Revise la serie anual, el intervalo probable del cambio y los mapas multiespectrales.

## Limitaciones técnicas

El módulo histórico usa Sentinel-2 L2A, disponible para análisis consistente aproximadamente desde 2016. Los resultados son indicadores territoriales para priorización y revisión humana; no confirman fosas, delitos ni hechos periciales. La selección de una escena por año reduce costos de cómputo, pero una versión institucional debería incorporar mosaicos compuestos, máscaras de nubes y PostgreSQL/PostGIS.
