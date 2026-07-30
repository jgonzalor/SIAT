from __future__ import annotations
import csv
import io
import json
import math
import xml.etree.ElementTree as ET
from typing import Any
from shapely.geometry import shape, mapping
from shapely.ops import transform
from pyproj import Transformer

def geojson_metrics(geometry: dict[str, Any]) -> tuple[float, float]:
    geom = shape(geometry)
    if geom.is_empty:
        return 0.0, 0.0
    lon, lat = geom.centroid.x, geom.centroid.y
    zone = max(1, min(60, int((lon + 180) / 6) + 1))
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    projected = transform(transformer.transform, geom)
    return float(projected.length), float(projected.area)

def parse_geojson(raw: bytes) -> list[dict]:
    obj = json.loads(raw.decode("utf-8-sig"))
    if obj.get("type") == "FeatureCollection":
        return obj.get("features", [])
    if obj.get("type") == "Feature":
        return [obj]
    return [{"type": "Feature", "properties": {}, "geometry": obj}]

def parse_gpx(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    points = []
    for elem in root.iter():
        if elem.tag.endswith("trkpt") or elem.tag.endswith("rtept"):
            points.append([float(elem.attrib["lon"]), float(elem.attrib["lat"])])
    if len(points) < 2:
        raise ValueError("El GPX no contiene una ruta con al menos dos puntos.")
    return [{
        "type": "Feature",
        "properties": {"nombre": "Recorrido GPX"},
        "geometry": {"type": "LineString", "coordinates": points}
    }]

def parse_csv_points(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.lower().strip(): f for f in (reader.fieldnames or [])}
    lat_key = next((fields[k] for k in ("lat", "latitude", "latitud") if k in fields), None)
    lon_key = next((fields[k] for k in ("lon", "lng", "longitude", "longitud") if k in fields), None)
    if not lat_key or not lon_key:
        raise ValueError("El CSV requiere columnas latitud/longitud o lat/lon.")
    features = []
    for row in reader:
        lat, lon = float(row[lat_key]), float(row[lon_key])
        props = {k: v for k, v in row.items() if k not in (lat_key, lon_key)}
        features.append({"type":"Feature","properties":props,
                         "geometry":{"type":"Point","coordinates":[lon, lat]}})
    return features

def feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}
