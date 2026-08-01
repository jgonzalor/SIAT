from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import date
from io import BytesIO
from itertools import islice
from typing import Any

import numpy as np
import pandas as pd
import planetary_computer as pc
import requests
from PIL import Image
from pystac_client import Client

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
INEGI_WMS_URL = "https://gaia.inegi.org.mx/NLB/tunnel/wms/wms61?"
PLANET_QUICK_SEARCH_URL = "https://api.planet.com/data/v1/quick-search"


@dataclass(frozen=True)
class SourceProfile:
    provider: str
    dataset: str
    collection: str
    source_type: str
    nominal_resolution_m: float | None
    access: str
    archive: str
    recommended_use: str


OPEN_PROFILES: tuple[SourceProfile, ...] = (
    SourceProfile(
        provider="Copernicus / Microsoft Planetary Computer",
        dataset="Sentinel-2 L2A",
        collection="sentinel-2-l2a",
        source_type="Óptica multiespectral",
        nominal_resolution_m=10.0,
        access="Abierto",
        archive="2016-presente",
        recommended_use="Vegetación, humedad, suelo desnudo y cambios territoriales",
    ),
    SourceProfile(
        provider="USGS / Microsoft Planetary Computer",
        dataset="Landsat Collection 2 L2",
        collection="landsat-c2-l2",
        source_type="Óptica multiespectral y térmica",
        nominal_resolution_m=30.0,
        access="Abierto",
        archive="1982-presente",
        recommended_use="Contexto histórico de largo plazo y cambios amplios",
    ),
    SourceProfile(
        provider="Copernicus / Microsoft Planetary Computer",
        dataset="Sentinel-1 RTC",
        collection="sentinel-1-rtc",
        source_type="Radar SAR",
        nominal_resolution_m=10.0,
        access="Abierto con posibles requisitos de cuenta para activos",
        archive="2014-presente",
        recommended_use="Cambios estructurales, humedad y observación con nubes",
    ),
)


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    text = str(value)
    return text or None


def _catalog() -> Client:
    return Client.open(PC_STAC_URL, modifier=pc.sign_inplace)


def detectability_for_resolution(resolution_m: float | None) -> str:
    if resolution_m is None:
        return "Resolución no declarada; requiere inspección visual"
    if resolution_m <= 0.5:
        return (
            "Vehículos y estructuras pequeñas pueden ser visibles; personas individuales "
            "no son una conclusión confiable"
        )
    if resolution_m <= 1.5:
        return "Techos, caminos, refugios y concentraciones de vehículos pueden ser visibles"
    if resolution_m <= 3.5:
        return "Claros, caminos y campamentos como conjunto; no objetos individuales confiables"
    if resolution_m <= 10:
        return "Cambios territoriales, caminos amplios y desmontes; no vehículos ni personas"
    return "Cambios regionales amplios; no objetos pequeños"


def _stac_search(
    profile: SourceProfile,
    bbox: list[float],
    start: date,
    end: date,
    max_cloud: int,
    limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    kwargs: dict[str, Any] = {
        "collections": [profile.collection],
        "bbox": bbox,
        "datetime": f"{start.isoformat()}/{end.isoformat()}",
        "limit": min(max(limit, 1), 100),
    }
    if profile.collection in {"sentinel-2-l2a", "landsat-c2-l2"}:
        kwargs["query"] = {"eo:cloud_cover": {"lte": max_cloud}}
        kwargs["sortby"] = [
            {"field": "properties.eo:cloud_cover", "direction": "asc"}
        ]
    else:
        kwargs["sortby"] = [
            {"field": "properties.datetime", "direction": "desc"}
        ]

    search = _catalog().search(**kwargs)
    items = list(islice(search.items(), limit))
    scenes: list[dict[str, Any]] = []
    dates: list[str] = []
    clouds: list[float] = []
    for item in items:
        dt = _safe_iso(item.datetime or item.properties.get("datetime"))
        if dt:
            dates.append(dt)
        cloud = item.properties.get("eo:cloud_cover")
        if cloud is not None:
            try:
                clouds.append(float(cloud))
            except (TypeError, ValueError):
                pass
        scenes.append(
            {
                "provider": profile.provider,
                "dataset": profile.dataset,
                "collection": profile.collection,
                "scene_id": item.id,
                "datetime": dt,
                "cloud_cover": float(cloud) if cloud is not None else None,
                "platform": item.properties.get("platform")
                or item.properties.get("constellation"),
                "product_type": item.properties.get("product:type")
                or item.properties.get("sar:product_type"),
                "resolution_m": profile.nominal_resolution_m,
                "access": profile.access,
                "geometry": item.geometry,
                "preview_png": None,
                "layer_name": None,
            }
        )

    summary = {
        **asdict(profile),
        "status": "Disponible" if items else "Sin escenas en el periodo",
        "scenes_found": len(items),
        "first_date": min(dates)[:10] if dates else None,
        "latest_date": max(dates)[:10] if dates else None,
        "min_cloud_pct": min(clouds) if clouds else None,
        "detectability": detectability_for_resolution(profile.nominal_resolution_m),
        "notes": "Consulta automática STAC completada",
    }
    return summary, scenes


def _year_from_text(text: str) -> int | None:
    matches = re.findall(r"(?:19|20)\d{2}", text)
    if not matches:
        return None
    return max(int(value) for value in matches)


def _infer_inegi_resolution(title: str) -> float | None:
    lower = title.lower()
    if "worldview" in lower or "geoeye" in lower:
        return 0.5
    if "rapideye" in lower:
        return 5.0
    if "orto" in lower:
        return 1.0
    if "landsat" in lower:
        return 30.0
    return None


def _parse_inegi_layers(xml_bytes: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    layers: list[dict[str, Any]] = []
    keywords = ("geoeye", "worldview", "rapideye", "ortoimagen", "ortofoto")
    for layer in root.findall(".//{*}Layer"):
        name_node = layer.find("{*}Name")
        title_node = layer.find("{*}Title")
        if name_node is None or not (name_node.text or "").strip():
            continue
        name = (name_node.text or "").strip()
        title = ((title_node.text if title_node is not None else None) or name).strip()
        haystack = f"{name} {title}".lower()
        if not any(keyword in haystack for keyword in keywords):
            continue
        layers.append(
            {
                "name": name,
                "title": title,
                "year": _year_from_text(title),
                "resolution_m": _infer_inegi_resolution(title),
            }
        )
    unique: dict[str, dict[str, Any]] = {}
    for layer in layers:
        unique[layer["name"]] = layer
    return list(unique.values())


def _wms_preview(
    layer_name: str,
    bbox: list[float],
    width: int = 420,
    height: int = 420,
    timeout: int = 15,
) -> bytes | None:
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": layer_name,
        "STYLES": "",
        "SRS": "EPSG:4326",
        "BBOX": ",".join(f"{value:.8f}" for value in bbox),
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "image/png",
        "TRANSPARENT": "TRUE",
    }
    response = requests.get(INEGI_WMS_URL, params=params, timeout=timeout)
    response.raise_for_status()
    if "image" not in response.headers.get("content-type", "").lower():
        return None
    image = Image.open(BytesIO(response.content)).convert("RGBA")
    array = np.asarray(image)
    alpha = array[..., 3]
    visible = alpha > 0
    if np.count_nonzero(visible) < max(50, int(0.002 * visible.size)):
        return None
    rgb = array[..., :3][visible]
    if rgb.size == 0 or float(np.std(rgb)) < 1.2:
        return None
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def scan_inegi_wms(
    bbox: list[float],
    start_year: int,
    end_year: int,
    max_layers: int = 8,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    response = requests.get(
        INEGI_WMS_URL,
        params={"SERVICE": "WMS", "REQUEST": "GetCapabilities", "VERSION": "1.1.1"},
        timeout=20,
    )
    response.raise_for_status()
    layers = _parse_inegi_layers(response.content)
    # Priorizar años solicitados y las capas más recientes.
    layers.sort(key=lambda row: (row["year"] is not None, row["year"] or 0), reverse=True)
    relevant = [
        row
        for row in layers
        if row["year"] is None or (start_year <= row["year"] <= end_year)
    ]
    if not relevant:
        relevant = layers
    tested = relevant[:max_layers]
    scenes: list[dict[str, Any]] = []
    available_layers: list[dict[str, Any]] = []
    preview_results: dict[str, bytes | None] = {}
    with ThreadPoolExecutor(max_workers=min(4, max(len(tested), 1))) as executor:
        futures = {
            executor.submit(_wms_preview, layer["name"], bbox): layer
            for layer in tested
        }
        for future in as_completed(futures):
            layer = futures[future]
            try:
                preview_results[layer["name"]] = future.result()
            except Exception:
                preview_results[layer["name"]] = None

    for layer in tested:
        preview = preview_results.get(layer["name"])
        if not preview:
            continue
        available_layers.append(layer)
        scenes.append(
            {
                "provider": "INEGI",
                "dataset": layer["title"],
                "collection": "INEGI WMS",
                "scene_id": layer["name"],
                "datetime": f"{layer['year']}-01-01" if layer["year"] else None,
                "cloud_cover": None,
                "platform": "INEGI / proveedor de origen",
                "product_type": "WMS de imagen de alta resolución",
                "resolution_m": layer["resolution_m"],
                "access": "Abierto para visualización WMS",
                "geometry": None,
                "preview_png": preview,
                "layer_name": layer["name"],
                "layer_title": layer["title"],
                "year": layer["year"],
            }
        )

    resolutions = [
        float(row["resolution_m"])
        for row in available_layers
        if row.get("resolution_m") is not None
    ]
    best_resolution = min(resolutions) if resolutions else None
    years = [row["year"] for row in available_layers if row.get("year")]
    summary = {
        "provider": "INEGI",
        "dataset": "Coberturas de imagen satelital / ortoimagen WMS",
        "collection": "INEGI WMS wms61",
        "source_type": "Óptica de alta resolución y ortoimagen",
        "nominal_resolution_m": best_resolution,
        "access": "Abierto para visualización WMS",
        "archive": "Cobertura variable por producto y año",
        "recommended_use": "Inspección visual de techos, caminos y estructuras cuando exista cobertura",
        "status": "Disponible en el AOI" if available_layers else "Sin cobertura verificada en las capas probadas",
        "scenes_found": len(available_layers),
        "first_date": f"{min(years)}-01-01" if years else None,
        "latest_date": f"{max(years)}-12-31" if years else None,
        "min_cloud_pct": None,
        "detectability": detectability_for_resolution(best_resolution),
        "notes": f"Se probaron {len(tested)} capas WMS y {len(available_layers)} devolvieron imagen útil",
    }
    return summary, scenes, available_layers


def _planet_geometry_filter(aoi_geometry: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "GeometryFilter",
        "field_name": "geometry",
        "config": aoi_geometry,
    }


def _planet_date_filter(start: date, end: date) -> dict[str, Any]:
    return {
        "type": "DateRangeFilter",
        "field_name": "acquired",
        "config": {
            "gte": f"{start.isoformat()}T00:00:00Z",
            "lte": f"{end.isoformat()}T23:59:59Z",
        },
    }


def _planet_cloud_filter(max_cloud: int) -> dict[str, Any]:
    return {
        "type": "RangeFilter",
        "field_name": "cloud_cover",
        "config": {"lte": max_cloud / 100.0},
    }


def scan_planet_catalog(
    api_key: str,
    aoi_geometry: dict[str, Any],
    start: date,
    end: date,
    max_cloud: int,
    limit: int = 25,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    scenes: list[dict[str, Any]] = []
    searches = [
        ("PlanetScope PSScene", "PSScene", 3.0),
        ("SkySat Collect", "SkySatCollect", 0.5),
    ]
    for title, item_type, resolution in searches:
        payload = {
            "item_types": [item_type],
            "filter": {
                "type": "AndFilter",
                "config": [
                    _planet_geometry_filter(aoi_geometry),
                    _planet_date_filter(start, end),
                    _planet_cloud_filter(max_cloud),
                ],
            },
        }
        response = requests.post(
            PLANET_QUICK_SEARCH_URL,
            auth=(api_key, ""),
            json=payload,
            timeout=45,
        )
        if response.status_code >= 400:
            summaries.append(
                {
                    "provider": "Planet",
                    "dataset": title,
                    "collection": item_type,
                    "source_type": "Óptica comercial",
                    "nominal_resolution_m": resolution,
                    "access": "Comercial con API key y licencia",
                    "archive": "Según contrato",
                    "recommended_use": "Verificación visual de alta resolución",
                    "status": f"Error de catálogo HTTP {response.status_code}",
                    "scenes_found": 0,
                    "first_date": None,
                    "latest_date": None,
                    "min_cloud_pct": None,
                    "detectability": detectability_for_resolution(resolution),
                    "notes": response.text[:180],
                }
            )
            continue
        features = (response.json() or {}).get("features", [])[:limit]
        dates: list[str] = []
        clouds: list[float] = []
        for feature in features:
            properties = feature.get("properties") or {}
            dt = properties.get("acquired") or properties.get("published")
            if dt:
                dates.append(str(dt))
            cloud = properties.get("cloud_cover")
            if cloud is not None:
                try:
                    clouds.append(float(cloud) * 100.0)
                except (TypeError, ValueError):
                    pass
            scenes.append(
                {
                    "provider": "Planet",
                    "dataset": title,
                    "collection": item_type,
                    "scene_id": feature.get("id"),
                    "datetime": dt,
                    "cloud_cover": (float(cloud) * 100.0) if cloud is not None else None,
                    "platform": properties.get("satellite_id") or properties.get("instrument"),
                    "product_type": item_type,
                    "resolution_m": resolution,
                    "access": "Comercial; requiere activación/orden para descargar",
                    "geometry": feature.get("geometry"),
                    "preview_png": None,
                    "layer_name": None,
                }
            )
        summaries.append(
            {
                "provider": "Planet",
                "dataset": title,
                "collection": item_type,
                "source_type": "Óptica comercial",
                "nominal_resolution_m": resolution,
                "access": "Comercial con API key y licencia",
                "archive": "Según contrato",
                "recommended_use": "Verificación visual de alta resolución",
                "status": "Disponible en catálogo" if features else "Sin escenas en el periodo",
                "scenes_found": len(features),
                "first_date": min(dates)[:10] if dates else None,
                "latest_date": max(dates)[:10] if dates else None,
                "min_cloud_pct": min(clouds) if clouds else None,
                "detectability": detectability_for_resolution(resolution),
                "notes": "La descarga depende de la licencia y de la activación/orden del activo",
            }
        )
    return summaries, scenes


def _best_source_summary(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    available = [
        row
        for row in summaries
        if int(row.get("scenes_found") or 0) > 0
        and row.get("nominal_resolution_m") is not None
    ]
    if not available:
        return {
            "best_provider": None,
            "best_dataset": None,
            "best_resolution_m": None,
            "object_inspection_ready": False,
            "assessment": "No se confirmó una fuente de mayor resolución para el área y periodo.",
        }
    best = min(available, key=lambda row: float(row["nominal_resolution_m"]))
    resolution = float(best["nominal_resolution_m"])
    return {
        "best_provider": best["provider"],
        "best_dataset": best["dataset"],
        "best_resolution_m": resolution,
        "latest_date": best.get("latest_date"),
        "access": best.get("access"),
        "object_inspection_ready": resolution <= 1.5,
        "assessment": detectability_for_resolution(resolution),
    }


def scan_multisource_catalog(
    bbox: list[float],
    aoi_geometry: dict[str, Any],
    start: date,
    end: date,
    max_cloud: int = 40,
    stac_limit: int = 25,
    planet_api_key: str | None = None,
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    scenes: list[dict[str, Any]] = []
    warnings: list[str] = []
    inegi_layers: list[dict[str, Any]] = []

    for profile in OPEN_PROFILES:
        try:
            summary, source_scenes = _stac_search(
                profile, bbox, start, end, max_cloud=max_cloud, limit=stac_limit
            )
        except Exception as exc:
            summary = {
                **asdict(profile),
                "status": "No disponible en esta ejecución",
                "scenes_found": 0,
                "first_date": None,
                "latest_date": None,
                "min_cloud_pct": None,
                "detectability": detectability_for_resolution(profile.nominal_resolution_m),
                "notes": str(exc),
            }
            source_scenes = []
            warnings.append(f"{profile.dataset}: {exc}")
        summaries.append(summary)
        scenes.extend(source_scenes)

    try:
        inegi_summary, inegi_scenes, inegi_layers = scan_inegi_wms(
            bbox, start.year, end.year
        )
        summaries.append(inegi_summary)
        scenes.extend(inegi_scenes)
    except Exception as exc:
        summaries.append(
            {
                "provider": "INEGI",
                "dataset": "Coberturas de imagen satelital / ortoimagen WMS",
                "collection": "INEGI WMS wms61",
                "source_type": "Óptica de alta resolución y ortoimagen",
                "nominal_resolution_m": None,
                "access": "Abierto para visualización WMS",
                "archive": "Cobertura variable por producto y año",
                "recommended_use": "Inspección visual cuando exista cobertura",
                "status": "No disponible en esta ejecución",
                "scenes_found": 0,
                "first_date": None,
                "latest_date": None,
                "min_cloud_pct": None,
                "detectability": detectability_for_resolution(None),
                "notes": str(exc),
            }
        )
        warnings.append(f"INEGI WMS: {exc}")

    key = planet_api_key or os.getenv("PLANET_API_KEY")
    if key:
        try:
            planet_summaries, planet_scenes = scan_planet_catalog(
                key, aoi_geometry, start, end, max_cloud=max_cloud
            )
            summaries.extend(planet_summaries)
            scenes.extend(planet_scenes)
        except Exception as exc:
            warnings.append(f"Planet: {exc}")
            summaries.append(
                {
                    "provider": "Planet",
                    "dataset": "PlanetScope / SkySat",
                    "collection": "Planet Data API",
                    "source_type": "Óptica comercial",
                    "nominal_resolution_m": 0.5,
                    "access": "Comercial con API key y licencia",
                    "archive": "Según contrato",
                    "recommended_use": "Verificación visual de alta resolución",
                    "status": "Error de conexión o credencial",
                    "scenes_found": 0,
                    "first_date": None,
                    "latest_date": None,
                    "min_cloud_pct": None,
                    "detectability": detectability_for_resolution(0.5),
                    "notes": str(exc),
                }
            )
    else:
        summaries.append(
            {
                "provider": "Planet",
                "dataset": "PlanetScope / SkySat",
                "collection": "Planet Data API",
                "source_type": "Óptica comercial",
                "nominal_resolution_m": 0.5,
                "access": "Comercial con API key y licencia",
                "archive": "Según contrato",
                "recommended_use": "Verificación visual de alta resolución",
                "status": "Conector preparado; falta PLANET_API_KEY",
                "scenes_found": 0,
                "first_date": None,
                "latest_date": None,
                "min_cloud_pct": None,
                "detectability": detectability_for_resolution(0.5),
                "notes": "No se realizan pedidos ni cargos automáticamente",
            }
        )

    summary_df = pd.DataFrame(summaries)
    scene_rows = [
        {key: value for key, value in row.items() if key not in {"preview_png", "geometry"}}
        for row in scenes
    ]
    return {
        "summary": summary_df,
        "scenes": scenes,
        "scene_table": pd.DataFrame(scene_rows),
        "inegi_layers": inegi_layers,
        "warnings": warnings,
        "best_source": _best_source_summary(summaries),
        "scan_start": start.isoformat(),
        "scan_end": end.isoformat(),
    }
