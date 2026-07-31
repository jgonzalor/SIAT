# Sentinel IAT · Motor de Detección de Anomalías v2

Versión enfocada únicamente en comparación multitemporal de imágenes Sentinel-2.

## Funciones
- Área marcada mediante rectángulo o polígono.
- Mosaico mediano de varias escenas por periodo.
- Máscara de nubes/sombras/nieve usando SCL.
- Índices NDVI, NDMI, NBR y BSI.
- Puntuación multiespectral de cambio.
- Polígonos GeoJSON de áreas priorizadas.
- Exportación Excel, GeoJSON, PNG y JSON.

## Ejecución
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Alcance
Detecta y prioriza cambios territoriales. No confirma fosas, delitos ni hallazgos.
