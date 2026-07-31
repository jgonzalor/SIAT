# Sentinel IAT · Serie Histórica y Fusión Multifuente v6

Aplicación Streamlit enfocada exclusivamente en el análisis territorial multitemporal y la consulta automática de fuentes satelitales disponibles para el polígono de estudio.

## Qué hace

1. Permite dibujar el polígono exacto del área de interés.
2. Construye una serie histórica anual con Sentinel-2 L2A.
3. Elimina nubes, sombras y nieve mediante la clasificación SCL.
4. Calcula NDVI, NDMI, NBR y BSI.
5. Detecta cambios interanuales y acumulados.
6. Genera polígonos de áreas prioritarias.
7. Consulta automáticamente fuentes complementarias:
   - Sentinel-2 L2A.
   - Landsat Collection 2 Level-2.
   - Sentinel-1 RTC.
   - Capas de imagen de alta resolución y ortoimagen publicadas por INEGI mediante WMS.
   - PlanetScope y SkySat cuando se configura una API key institucional de Planet.
8. Muestra previsualizaciones INEGI cuando la capa devuelve cobertura útil en el área.
9. Evalúa qué tipo de inspección visual permite la resolución localizada.
10. Exporta polígonos y metadatos a KMZ, Excel y paquete ZIP.

## Principio técnico

Sentinel-2 continúa siendo la fuente analítica principal. Las fuentes adicionales sirven para:

- extender el histórico con Landsat;
- corroborar cambios bajo nubosidad mediante Sentinel-1 SAR;
- revisar estructuras, caminos o vehículos únicamente cuando existe una imagen de resolución suficiente;
- documentar qué fuentes fueron consultadas y cuáles realmente cubren el área.

El sistema no afirma detectar personas, vehículos o refugios cuando la resolución no los permite. Una fuente de 10 o 30 metros se considera adecuada para cambios territoriales, no para objetos individuales.

## Fuentes implementadas

### Microsoft Planetary Computer STAC

- `sentinel-2-l2a`
- `landsat-c2-l2`
- `sentinel-1-rtc`

### INEGI WMS

- Servicio oficial: `https://gaia.inegi.org.mx/NLB/tunnel/wms/wms61?`
- El motor consulta GetCapabilities, localiza capas GeoEye, WorldView, RapidEye y ortoimagen, y prueba cuáles devuelven imagen útil en el AOI.

### Planet opcional

El conector de búsqueda de catálogo está preparado para Planet Data API. No genera pedidos, activaciones ni cargos.

En Streamlit Secrets:

```toml
PLANET_API_KEY = "SU_API_KEY_INSTITUCIONAL"
```

Sin esta clave, el sistema funciona con todas las fuentes abiertas.

## Ejecución

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Límites

- Área máxima histórica: 25 km².
- Hasta 16 años por análisis.
- Las fechas y cobertura de INEGI varían por producto.
- Las imágenes comerciales dependen del contrato y licencia del usuario.
- Los resultados priorizan áreas para revisión y no constituyen por sí solos una conclusión pericial, causal o delictiva.
