from __future__ import annotations

import calendar
import json
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date
from io import BytesIO
from itertools import islice
from typing import Any, Callable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import planetary_computer as pc
import rasterio
from PIL import Image
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
from pyproj import Geod
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
ARCHIVE_START_YEAR = 2016
GEOD = Geod(ellps="WGS84")
KML_NS = "http://www.opengis.net/kml/2.2"
ET.register_namespace("", KML_NS)


@dataclass
class SceneInfo:
    scene_id: str
    datetime: str | None
    cloud_cover: float
    valid_coverage_pct: float
    platform: str | None
    mgrs_tile: str | None


def _catalog() -> Client:
    return Client.open(STAC_URL, modifier=pc.sign_inplace)


def _item_metadata(item: Any, selected: bool = False) -> dict[str, Any]:
    dt = item.datetime.isoformat() if item.datetime else item.properties.get("datetime")
    return {
        "scene_id": item.id,
        "datetime": dt,
        "cloud_cover": float(item.properties.get("eo:cloud_cover", 0) or 0),
        "platform": item.properties.get("platform") or item.properties.get("constellation"),
        "mgrs_tile": item.properties.get("s2:mgrs_tile"),
        "selected": bool(selected),
    }


def search_scenes(
    bbox: list[float],
    start: date,
    end: date,
    max_cloud: int,
    limit: int = 12,
    candidate_cap: int = 40,
) -> dict[str, Any]:
    """Busca escenas y separa candidatas de escenas seleccionadas.

    Se conserva un máximo razonable de candidatas para evitar consultas y tablas
    desproporcionadas. Las escenas seleccionadas son las de menor nubosidad.
    """
    search = _catalog().search(
        collections=[COLLECTION],
        bbox=bbox,
        datetime=f"{start.isoformat()}/{end.isoformat()}",
        query={"eo:cloud_cover": {"lte": max_cloud}},
        sortby=[{"field": "properties.eo:cloud_cover", "direction": "asc"}],
        limit=min(max(candidate_cap, limit), 100),
    )
    items = list(islice(search.items(), candidate_cap))
    if not items:
        raise RuntimeError(
            "No se encontraron imágenes Sentinel-2 para el periodo y la nubosidad seleccionados."
        )
    selected_items = items[:limit]
    selected_ids = {item.id for item in selected_items}
    candidates = [_item_metadata(item, item.id in selected_ids) for item in items]
    return {
        "selected_items": selected_items,
        "candidates": candidates,
        "candidate_count": len(items),
        "candidate_cap": candidate_cap,
    }


def _asset(item: Any, *names: str) -> str:
    for name in names:
        if name in item.assets:
            return item.assets[name].href
    raise KeyError(f"No se localizó la banda requerida: {names}")


def _read_asset(
    href: str,
    bbox: list[float],
    out_shape: tuple[int, int],
    categorical: bool = False,
) -> np.ma.MaskedArray:
    out_height, out_width = out_shape
    with rasterio.open(href) as src:
        left, bottom, right, top = transform_bounds(
            "EPSG:4326", src.crs, *bbox, densify_pts=21
        )
        window = from_bounds(left, bottom, right, top, src.transform)
        if window.width <= 0 or window.height <= 0:
            raise RuntimeError("El área marcada no intersecta la escena satelital.")
        return src.read(
            1,
            window=window,
            out_shape=(out_height, out_width),
            resampling=Resampling.nearest if categorical else Resampling.bilinear,
            masked=True,
        ).astype("float32")


def _output_shape(bbox: list[float], max_size: int) -> tuple[int, int]:
    """Conserva aproximadamente la relación espacial del área seleccionada."""
    lat_mid = (bbox[1] + bbox[3]) / 2
    width_km = max(abs(bbox[2] - bbox[0]) * 111.32 * np.cos(np.radians(lat_mid)), 0.001)
    height_km = max(abs(bbox[3] - bbox[1]) * 110.57, 0.001)
    ratio = width_km / height_km
    if ratio >= 1:
        width = max_size
        height = max(96, int(round(max_size / ratio)))
    else:
        height = max_size
        width = max(96, int(round(max_size * ratio)))
    return height, width


def _aoi_outside_mask(
    bbox: list[float],
    out_shape: tuple[int, int],
    aoi_geometry: dict[str, Any] | None,
) -> np.ndarray:
    """Devuelve True fuera del polígono de estudio y False en su interior."""
    if not aoi_geometry:
        return np.zeros(out_shape, dtype=bool)
    height, width = out_shape
    transform = transform_from_bounds(*bbox, width, height)
    inside = geometry_mask(
        [aoi_geometry],
        out_shape=out_shape,
        transform=transform,
        invert=True,
        all_touched=False,
    )
    if not np.any(inside):
        raise RuntimeError("El polígono marcado no contiene píxeles analizables.")
    return ~inside


def _scene_arrays(
    item: Any, bbox: list[float], out_shape: tuple[int, int]
) -> dict[str, np.ma.MaskedArray]:
    blue = _read_asset(_asset(item, "B02", "blue"), bbox, out_shape)
    green = _read_asset(_asset(item, "B03", "green"), bbox, out_shape)
    red = _read_asset(_asset(item, "B04", "red"), bbox, out_shape)
    nir = _read_asset(_asset(item, "B08", "nir"), bbox, out_shape)
    swir1 = _read_asset(_asset(item, "B11", "swir16"), bbox, out_shape)
    swir2 = _read_asset(_asset(item, "B12", "swir22"), bbox, out_shape)
    scl = _read_asset(_asset(item, "SCL", "scl"), bbox, out_shape, categorical=True)

    # SCL: 0/1 sin datos, 3 sombra, 8/9/10 nubes, 11 nieve/hielo.
    invalid_scl = np.isin(
        np.asarray(scl.filled(0), dtype=np.int16), [0, 1, 3, 8, 9, 10, 11]
    )
    common = invalid_scl.copy()
    for arr in (blue, green, red, nir, swir1, swir2):
        common |= np.ma.getmaskarray(arr)
    return {
        key: np.ma.array(value, mask=common)
        for key, value in {
            "blue": blue,
            "green": green,
            "red": red,
            "nir": nir,
            "swir1": swir1,
            "swir2": swir2,
        }.items()
    }


def _median_stack(arrays: list[np.ma.MaskedArray]) -> np.ma.MaskedArray:
    if not arrays:
        raise RuntimeError("No hay imágenes válidas para construir el mosaico.")
    return np.ma.median(np.ma.stack(arrays, axis=0), axis=0)


def _index(a: np.ma.MaskedArray, b: np.ma.MaskedArray) -> np.ma.MaskedArray:
    den = a + b
    return np.ma.clip(
        np.ma.masked_where(np.abs(den) < 1e-8, (a - b) / den), -1, 1
    )


def _stretch_rgb(rgb: np.ma.MaskedArray) -> np.ma.MaskedArray:
    valid = rgb.compressed()
    if not valid.size:
        return np.ma.clip(rgb, 0, 1)
    lo, hi = np.percentile(valid, [2, 98])
    return np.ma.clip((rgb - lo) / max(float(hi - lo), 1e-6), 0, 1)


def _rgb_png(rgb: np.ma.MaskedArray) -> bytes:
    data = np.asarray(np.ma.filled(rgb, 0), dtype=np.float32)
    data = np.clip(data, 0, 1)
    rgb_u8 = (data * 255).astype(np.uint8)
    mask = np.ma.getmaskarray(rgb)
    if mask.ndim == 3:
        outside = np.all(mask, axis=2)
    else:
        outside = mask
    alpha = np.where(outside, 0, 255).astype(np.uint8)
    rgba = np.dstack((rgb_u8, alpha))
    image = Image.fromarray(rgba, mode="RGBA")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def build_composite(
    items: list[Any],
    bbox: list[float],
    out_size: int = 512,
    include_scene_previews: bool = True,
    aoi_geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out_shape = _output_shape(bbox, out_size)
    outside_aoi = _aoi_outside_mask(bbox, out_shape, aoi_geometry)
    inside_pixels = max(int(np.count_nonzero(~outside_aoi)), 1)
    stacks: dict[str, list[np.ma.MaskedArray]] = {
        key: [] for key in ("blue", "green", "red", "nir", "swir1", "swir2")
    }
    used: list[SceneInfo] = []
    previews: list[dict[str, Any]] = []
    errors: list[str] = []

    for item in items:
        try:
            data = _scene_arrays(item, bbox, out_shape)
            if np.any(outside_aoi):
                data = {
                    key: np.ma.array(value, mask=np.ma.getmaskarray(value) | outside_aoi)
                    for key, value in data.items()
                }
            valid_inside = np.count_nonzero(
                (~np.ma.getmaskarray(data["red"])) & (~outside_aoi)
            )
            valid = 100 * valid_inside / inside_pixels
            if valid < 25:
                errors.append(
                    f"{item.id}: cobertura válida insuficiente ({valid:.1f}%)."
                )
                continue
            for key in stacks:
                stacks[key].append(data[key])

            info = SceneInfo(
                scene_id=item.id,
                datetime=item.datetime.isoformat() if item.datetime else None,
                cloud_cover=float(item.properties.get("eo:cloud_cover", 0) or 0),
                valid_coverage_pct=float(valid),
                platform=item.properties.get("platform")
                or item.properties.get("constellation"),
                mgrs_tile=item.properties.get("s2:mgrs_tile"),
            )
            used.append(info)

            if include_scene_previews:
                scene_rgb = _stretch_rgb(
                    np.ma.dstack((data["red"], data["green"], data["blue"]))
                )
                previews.append({**asdict(info), "preview_png": _rgb_png(scene_rgb)})
        except Exception as exc:
            errors.append(f"{item.id}: {exc}")

    if not used:
        raise RuntimeError(
            "No fue posible construir un mosaico válido. Amplíe el periodo o aumente la nubosidad permitida."
        )

    composite = {key: _median_stack(values) for key, values in stacks.items()}
    ndvi = _index(composite["nir"], composite["red"])
    ndmi = _index(composite["nir"], composite["swir1"])
    nbr = _index(composite["nir"], composite["swir2"])
    bsi_num = (composite["swir1"] + composite["red"]) - (
        composite["nir"] + composite["blue"]
    )
    bsi_den = (composite["swir1"] + composite["red"]) + (
        composite["nir"] + composite["blue"]
    )
    bsi = np.ma.clip(
        np.ma.masked_where(np.abs(bsi_den) < 1e-8, bsi_num / bsi_den), -1, 1
    )
    rgb = _stretch_rgb(
        np.ma.dstack((composite["red"], composite["green"], composite["blue"]))
    )

    return {
        "rgb": rgb,
        "ndvi": ndvi,
        "ndmi": ndmi,
        "nbr": nbr,
        "bsi": bsi,
        "scenes": [asdict(scene) for scene in used],
        "scene_previews": previews,
        "warnings": errors,
        "out_shape": out_shape,
    }


def _png_bytes(
    array: Any,
    title: str,
    cmap: str | None = None,
    vmin: float | None = None,
    vmax: float | None = None,
) -> bytes:
    fig, ax = plt.subplots(figsize=(7, 6))
    ax.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_title(title)
    ax.set_axis_off()
    fig.tight_layout()
    bio = BytesIO()
    fig.savefig(bio, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    return bio.getvalue()


def _area_km2(
    bbox: list[float], aoi_geometry: dict[str, Any] | None = None
) -> float:
    if aoi_geometry:
        area_m2, _ = GEOD.geometry_area_perimeter(shape(aoi_geometry))
        return abs(float(area_m2)) / 1_000_000.0
    lat_mid = (bbox[1] + bbox[3]) / 2
    width_km = abs(bbox[2] - bbox[0]) * 111.32 * np.cos(np.radians(lat_mid))
    height_km = abs(bbox[3] - bbox[1]) * 110.57
    return float(width_km * height_km)


def _change_score(
    comp_a: dict[str, Any], comp_b: dict[str, Any]
) -> tuple[np.ma.MaskedArray, dict[str, np.ma.MaskedArray]]:
    mask = np.ma.getmaskarray(comp_a["ndvi"]) | np.ma.getmaskarray(comp_b["ndvi"])
    deltas = {
        name: np.ma.array(comp_b[name] - comp_a[name], mask=mask)
        for name in ("ndvi", "ndmi", "nbr", "bsi")
    }
    score = (
        np.abs(deltas["ndvi"]) * 0.40
        + np.abs(deltas["ndmi"]) * 0.22
        + np.abs(deltas["nbr"]) * 0.20
        + np.maximum(deltas["bsi"], 0) * 0.18
    )
    return np.ma.clip(score, 0, 1), deltas


def _anomaly_geojson(
    anomaly: np.ndarray,
    bbox: list[float],
    min_patch_pixels: int,
) -> dict[str, Any]:
    height, width = anomaly.shape
    transform = transform_from_bounds(*bbox, width, height)
    geoms = []
    for geom, value in shapes(
        anomaly.astype(np.uint8), mask=anomaly.astype(bool), transform=transform
    ):
        if value != 1:
            continue
        poly = shape(geom)
        if poly.area <= 0:
            continue
        approx_pixels = poly.area / abs(transform.a * transform.e)
        if approx_pixels >= min_patch_pixels:
            geoms.append(poly)

    merged = unary_union(geoms) if geoms else None
    features = []
    if merged and not merged.is_empty:
        parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
        for idx, poly in enumerate(parts, 1):
            area_m2, _ = GEOD.geometry_area_perimeter(poly)
            area_m2 = abs(float(area_m2))
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "id": idx,
                        "clasificacion": "Área prioritaria para revisión",
                        "area_m2": area_m2,
                        "area_ha": area_m2 / 10000.0,
                    },
                    "geometry": mapping(poly),
                }
            )
    return {"type": "FeatureCollection", "features": features}



def _kml_tag(name: str) -> str:
    return f"{{{KML_NS}}}{name}"


def _kml_coordinates(ring: list[list[float]] | tuple[tuple[float, ...], ...]) -> str:
    coordinates = []
    for point in ring:
        if len(point) < 2:
            continue
        lon, lat = float(point[0]), float(point[1])
        altitude = float(point[2]) if len(point) > 2 else 0.0
        coordinates.append(f"{lon:.8f},{lat:.8f},{altitude:.2f}")
    return " ".join(coordinates)


def _append_kml_polygon(parent: ET.Element, coordinates: Any) -> None:
    polygon = ET.SubElement(parent, _kml_tag("Polygon"))
    ET.SubElement(polygon, _kml_tag("tessellate")).text = "1"
    ET.SubElement(polygon, _kml_tag("altitudeMode")).text = "clampToGround"

    if not coordinates:
        return
    outer = ET.SubElement(polygon, _kml_tag("outerBoundaryIs"))
    outer_ring = ET.SubElement(outer, _kml_tag("LinearRing"))
    ET.SubElement(outer_ring, _kml_tag("coordinates")).text = _kml_coordinates(
        coordinates[0]
    )

    for inner_coords in coordinates[1:]:
        inner = ET.SubElement(polygon, _kml_tag("innerBoundaryIs"))
        inner_ring = ET.SubElement(inner, _kml_tag("LinearRing"))
        ET.SubElement(inner_ring, _kml_tag("coordinates")).text = _kml_coordinates(
            inner_coords
        )


def _append_kml_geometry(parent: ET.Element, geometry: dict[str, Any]) -> None:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        _append_kml_polygon(parent, coordinates)
    elif geometry_type == "MultiPolygon":
        multi = ET.SubElement(parent, _kml_tag("MultiGeometry"))
        for polygon_coords in coordinates:
            _append_kml_polygon(multi, polygon_coords)
    else:
        raise ValueError(f"Geometría no compatible con KMZ: {geometry_type}")


def _append_extended_data(parent: ET.Element, values: dict[str, Any]) -> None:
    clean_values: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple, set)):
            serialized = json.dumps(value, ensure_ascii=False, default=str)
        else:
            serialized = str(value)
        clean_values[str(key)] = serialized
    if not clean_values:
        return
    extended = ET.SubElement(parent, _kml_tag("ExtendedData"))
    for key, value in clean_values.items():
        data = ET.SubElement(extended, _kml_tag("Data"), {"name": key})
        ET.SubElement(data, _kml_tag("value")).text = value


def geojson_to_kmz(
    geojson: dict[str, Any],
    bbox: list[float],
    document_name: str,
    description: str,
    metadata: dict[str, Any] | None = None,
    aoi_geometry: dict[str, Any] | None = None,
) -> bytes:
    """Convierte las áreas priorizadas a un KMZ autocontenido para Google Earth."""
    root = ET.Element(_kml_tag("kml"))
    document = ET.SubElement(root, _kml_tag("Document"))
    ET.SubElement(document, _kml_tag("name")).text = document_name
    ET.SubElement(document, _kml_tag("open")).text = "1"
    ET.SubElement(document, _kml_tag("visibility")).text = "1"
    ET.SubElement(document, _kml_tag("description")).text = description
    _append_extended_data(document, metadata or {})

    anomaly_style = ET.SubElement(document, _kml_tag("Style"), {"id": "anomalyStyle"})
    anomaly_line = ET.SubElement(anomaly_style, _kml_tag("LineStyle"))
    ET.SubElement(anomaly_line, _kml_tag("color")).text = "ff0000ff"
    ET.SubElement(anomaly_line, _kml_tag("width")).text = "3"
    anomaly_poly = ET.SubElement(anomaly_style, _kml_tag("PolyStyle"))
    ET.SubElement(anomaly_poly, _kml_tag("color")).text = "660000ff"
    ET.SubElement(anomaly_poly, _kml_tag("fill")).text = "1"
    ET.SubElement(anomaly_poly, _kml_tag("outline")).text = "1"

    aoi_style = ET.SubElement(document, _kml_tag("Style"), {"id": "aoiStyle"})
    aoi_line = ET.SubElement(aoi_style, _kml_tag("LineStyle"))
    ET.SubElement(aoi_line, _kml_tag("color")).text = "ffff0000"
    ET.SubElement(aoi_line, _kml_tag("width")).text = "2"
    aoi_poly = ET.SubElement(aoi_style, _kml_tag("PolyStyle"))
    ET.SubElement(aoi_poly, _kml_tag("color")).text = "180000ff"
    ET.SubElement(aoi_poly, _kml_tag("fill")).text = "1"
    ET.SubElement(aoi_poly, _kml_tag("outline")).text = "1"

    area_folder = ET.SubElement(document, _kml_tag("Folder"))
    ET.SubElement(area_folder, _kml_tag("name")).text = "Área analizada"
    ET.SubElement(area_folder, _kml_tag("open")).text = "0"
    area_placemark = ET.SubElement(area_folder, _kml_tag("Placemark"))
    ET.SubElement(area_placemark, _kml_tag("name")).text = "Polígono de consulta"
    ET.SubElement(area_placemark, _kml_tag("styleUrl")).text = "#aoiStyle"
    if aoi_geometry:
        _append_kml_geometry(area_placemark, aoi_geometry)
    else:
        west, south, east, north = map(float, bbox)
        bbox_ring = [
            [west, south, 0],
            [east, south, 0],
            [east, north, 0],
            [west, north, 0],
            [west, south, 0],
        ]
        _append_kml_polygon(area_placemark, [bbox_ring])

    anomaly_folder = ET.SubElement(document, _kml_tag("Folder"))
    ET.SubElement(anomaly_folder, _kml_tag("name")).text = "Áreas priorizadas"
    ET.SubElement(anomaly_folder, _kml_tag("open")).text = "1"
    ET.SubElement(anomaly_folder, _kml_tag("visibility")).text = "1"

    features = geojson.get("features", [])
    if not features:
        empty_placemark = ET.SubElement(anomaly_folder, _kml_tag("Placemark"))
        ET.SubElement(empty_placemark, _kml_tag("name")).text = (
            "Sin polígonos por encima del umbral"
        )
        ET.SubElement(empty_placemark, _kml_tag("description")).text = (
            "El análisis no generó áreas priorizadas con la sensibilidad seleccionada."
        )
    else:
        for index, feature in enumerate(features, 1):
            properties = dict(feature.get("properties") or {})
            placemark = ET.SubElement(anomaly_folder, _kml_tag("Placemark"))
            placemark_name = f"Anomalía {properties.get('id', index)}"
            area_ha = properties.get("area_ha")
            if isinstance(area_ha, (int, float)):
                placemark_name += f" · {area_ha:.3f} ha"
            ET.SubElement(placemark, _kml_tag("name")).text = placemark_name
            ET.SubElement(placemark, _kml_tag("visibility")).text = "1"
            ET.SubElement(placemark, _kml_tag("styleUrl")).text = "#anomalyStyle"
            ET.SubElement(placemark, _kml_tag("description")).text = str(
                properties.get("clasificacion", "Área prioritaria para revisión")
            )
            _append_extended_data(placemark, properties)
            _append_kml_geometry(placemark, feature.get("geometry") or {})

    kml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml_bytes)
    return output.getvalue()


def comparison_kmz(result: dict[str, Any]) -> bytes:
    metadata = {
        **_clean_metadata(result.get("metadata", {})),
        "area_analizada_km2": round(float(result["area_km2"]), 6),
        "area_priorizada_km2": round(float(result["changed_km2"]), 6),
        "area_priorizada_pct": round(float(result["changed_pct"]), 4),
        "prioridad": result["priority"],
    }
    return geojson_to_kmz(
        result["geojson"],
        result["bbox"],
        "Sentinel IAT · Anomalías territoriales",
        result["interpretation"],
        metadata,
        aoi_geometry=result.get("aoi_geometry"),
    )


def historical_kmz(result: dict[str, Any]) -> bytes:
    """Exporta el AOI exacto, anomalías acumuladas y transiciones anuales a KMZ."""
    strongest = result["strongest_transition"]
    years = result.get("valid_years", [])
    year_label = f"{years[0]}-{years[-1]}" if years else "serie histórica"
    metadata = {
        **_clean_metadata(result.get("metadata", {})),
        "area_analizada_km2": round(float(result["area_km2"]), 6),
        "transicion_mayor": strongest["transition"],
        "cambio_maximo_pct": round(float(strongest["changed_pct"]), 4),
        "cambio_acumulado_pct": round(float(result["accumulated_changed_pct"]), 4),
    }

    root = ET.Element(_kml_tag("kml"))
    document = ET.SubElement(root, _kml_tag("Document"))
    ET.SubElement(document, _kml_tag("name")).text = (
        f"Sentinel IAT · Serie histórica {year_label}"
    )
    ET.SubElement(document, _kml_tag("open")).text = "1"
    ET.SubElement(document, _kml_tag("visibility")).text = "1"
    ET.SubElement(document, _kml_tag("description")).text = result["interpretation"]
    _append_extended_data(document, metadata)

    def add_style(style_id: str, line_color: str, fill_color: str, width: str = "3"):
        style = ET.SubElement(document, _kml_tag("Style"), {"id": style_id})
        line = ET.SubElement(style, _kml_tag("LineStyle"))
        ET.SubElement(line, _kml_tag("color")).text = line_color
        ET.SubElement(line, _kml_tag("width")).text = width
        poly = ET.SubElement(style, _kml_tag("PolyStyle"))
        ET.SubElement(poly, _kml_tag("color")).text = fill_color
        ET.SubElement(poly, _kml_tag("fill")).text = "1"
        ET.SubElement(poly, _kml_tag("outline")).text = "1"

    # KML usa AABBGGRR.
    add_style("aoiStyle", "ffff0000", "220000ff", "2")
    add_style("accumulatedStyle", "ff0000ff", "660000ff", "3")
    add_style("transitionStyle", "ff00a5ff", "5500a5ff", "2")
    add_style("strongestStyle", "ff00ffff", "6600ffff", "4")

    area_folder = ET.SubElement(document, _kml_tag("Folder"))
    ET.SubElement(area_folder, _kml_tag("name")).text = "01 · Área exacta analizada"
    ET.SubElement(area_folder, _kml_tag("open")).text = "0"
    area_placemark = ET.SubElement(area_folder, _kml_tag("Placemark"))
    ET.SubElement(area_placemark, _kml_tag("name")).text = "Polígono de consulta"
    ET.SubElement(area_placemark, _kml_tag("visibility")).text = "1"
    ET.SubElement(area_placemark, _kml_tag("styleUrl")).text = "#aoiStyle"
    aoi_geometry = result.get("aoi_geometry")
    if aoi_geometry:
        _append_kml_geometry(area_placemark, aoi_geometry)
    else:
        west, south, east, north = map(float, result["bbox"])
        _append_kml_polygon(
            area_placemark,
            [[[west, south, 0], [east, south, 0], [east, north, 0],
              [west, north, 0], [west, south, 0]]],
        )

    def append_feature_folder(
        parent: ET.Element,
        folder_name: str,
        geojson: dict[str, Any],
        style_id: str,
        visible: bool,
        transition: str | None = None,
    ) -> None:
        folder = ET.SubElement(parent, _kml_tag("Folder"))
        ET.SubElement(folder, _kml_tag("name")).text = folder_name
        ET.SubElement(folder, _kml_tag("open")).text = "0"
        ET.SubElement(folder, _kml_tag("visibility")).text = "1" if visible else "0"
        features = geojson.get("features", [])
        if not features:
            placemark = ET.SubElement(folder, _kml_tag("Placemark"))
            ET.SubElement(placemark, _kml_tag("name")).text = "Sin polígonos sobre el umbral"
            ET.SubElement(placemark, _kml_tag("visibility")).text = "0"
            return
        for index, feature in enumerate(features, 1):
            properties = dict(feature.get("properties") or {})
            if transition:
                properties.setdefault("transition", transition)
            placemark = ET.SubElement(folder, _kml_tag("Placemark"))
            area_ha = properties.get("area_ha")
            name = f"Anomalía {index}"
            if transition:
                name = f"{transition} · {name}"
            if isinstance(area_ha, (int, float)):
                name += f" · {area_ha:.3f} ha"
            ET.SubElement(placemark, _kml_tag("name")).text = name
            ET.SubElement(placemark, _kml_tag("visibility")).text = (
                "1" if visible else "0"
            )
            ET.SubElement(placemark, _kml_tag("styleUrl")).text = f"#{style_id}"
            ET.SubElement(placemark, _kml_tag("description")).text = str(
                properties.get("clasificacion", "Área prioritaria para revisión")
            )
            _append_extended_data(placemark, properties)
            _append_kml_geometry(placemark, feature.get("geometry") or {})

    append_feature_folder(
        document,
        f"02 · Anomalías acumuladas {year_label}",
        result["accumulated_geojson"],
        "accumulatedStyle",
        True,
    )

    transitions_folder = ET.SubElement(document, _kml_tag("Folder"))
    ET.SubElement(transitions_folder, _kml_tag("name")).text = "03 · Anomalías por transición anual"
    ET.SubElement(transitions_folder, _kml_tag("open")).text = "0"
    ET.SubElement(transitions_folder, _kml_tag("visibility")).text = "1"
    strongest_label = strongest["transition"]
    for label, geojson in result.get("transition_geojsons", {}).items():
        is_strongest = label == strongest_label
        append_feature_folder(
            transitions_folder,
            ("★ Mayor cambio · " if is_strongest else "") + label,
            geojson,
            "strongestStyle" if is_strongest else "transitionStyle",
            is_strongest,
            transition=label,
        )

    kml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml_bytes)
    return output.getvalue()


def _score_summary(score: np.ma.MaskedArray, threshold: float) -> dict[str, float]:
    valid = score.compressed()
    return {
        "changed_pct": float(np.mean(valid >= threshold) * 100) if valid.size else 0.0,
        "mean_score": float(valid.mean()) if valid.size else 0.0,
        "max_score": float(valid.max()) if valid.size else 0.0,
    }


def _priority(changed_pct: float) -> str:
    if changed_pct < 2:
        return "Baja"
    if changed_pct < 8:
        return "Media"
    if changed_pct < 20:
        return "Alta"
    return "Muy alta"


def analyze_change(
    bbox: list[float],
    start_a: date,
    end_a: date,
    start_b: date,
    end_b: date,
    max_cloud: int = 35,
    scenes_per_period: int = 8,
    out_size: int = 512,
    threshold: float = 0.24,
    min_patch_pixels: int = 20,
    aoi_geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if start_a > end_a or start_b > end_b:
        raise ValueError("Las fechas de inicio deben ser anteriores a las fechas finales.")
    area_km2 = _area_km2(bbox, aoi_geometry)
    if area_km2 > 40:
        raise ValueError(
            f"El área marcada es de {area_km2:.1f} km². Redúzcala a 40 km² o menos."
        )

    search_a = search_scenes(bbox, start_a, end_a, max_cloud, scenes_per_period)
    search_b = search_scenes(bbox, start_b, end_b, max_cloud, scenes_per_period)
    comp_a = build_composite(
        search_a["selected_items"], bbox, out_size, aoi_geometry=aoi_geometry
    )
    comp_b = build_composite(
        search_b["selected_items"], bbox, out_size, aoi_geometry=aoi_geometry
    )

    score, _ = _change_score(comp_a, comp_b)
    anomaly = np.asarray(score.filled(0) >= threshold, dtype=np.uint8)
    geojson = _anomaly_geojson(anomaly, bbox, min_patch_pixels)
    summary = _score_summary(score, threshold)
    changed_pct = summary["changed_pct"]
    area_changed = area_km2 * changed_pct / 100
    priority = _priority(changed_pct)

    interpretation = (
        f"Se prioriza {changed_pct:.1f}% del área marcada ({area_changed:.3f} km²) "
        "por cambios multiespectrales. El resultado indica zonas que requieren revisión "
        "visual, contraste histórico y verificación de campo; no confirma por sí solo "
        "una fosa, delito o hallazgo."
    )

    overview = BytesIO()
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    axes[0, 0].imshow(comp_a["rgb"])
    axes[0, 0].set_title("ANTES · mosaico limpio")
    axes[0, 1].imshow(comp_b["rgb"])
    axes[0, 1].set_title("DESPUÉS · mosaico limpio")
    im = axes[1, 0].imshow(
        score,
        cmap="magma",
        vmin=0,
        vmax=max(0.5, summary["max_score"]),
    )
    axes[1, 0].set_title("Puntuación multiespectral")
    axes[1, 1].imshow(anomaly, cmap="Reds", vmin=0, vmax=1)
    axes[1, 1].set_title("Áreas priorizadas")
    for ax in axes.ravel():
        ax.set_axis_off()
    fig.colorbar(im, ax=axes[1, 0], fraction=0.046, pad=0.04)
    fig.tight_layout()
    fig.savefig(overview, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)

    scenes = pd.DataFrame(
        [{"periodo": "Antes", **row} for row in comp_a["scenes"]]
        + [{"periodo": "Después", **row} for row in comp_b["scenes"]]
    )
    candidates = pd.DataFrame(
        [{"periodo": "Antes", **row} for row in search_a["candidates"]]
        + [{"periodo": "Después", **row} for row in search_b["candidates"]]
    )
    scene_previews = [
        {"periodo": "Antes", **row} for row in comp_a["scene_previews"]
    ] + [{"periodo": "Después", **row} for row in comp_b["scene_previews"]]

    return {
        "bbox": bbox,
        "aoi_geometry": aoi_geometry,
        "area_km2": area_km2,
        "changed_pct": changed_pct,
        "changed_km2": area_changed,
        "priority": priority,
        "mean_score": summary["mean_score"],
        "max_score": summary["max_score"],
        "interpretation": interpretation,
        "geojson": geojson,
        "scenes": scenes,
        "candidates": candidates,
        "scene_previews": scene_previews,
        "before_png": _png_bytes(comp_a["rgb"], "ANTES · mosaico satelital"),
        "after_png": _png_bytes(comp_b["rgb"], "DESPUÉS · mosaico satelital"),
        "score_png": _png_bytes(
            score, "Puntuación de cambio", "magma", 0, max(0.5, summary["max_score"])
        ),
        "anomaly_png": _png_bytes(anomaly, "Áreas priorizadas", "Reds", 0, 1),
        "overview_png": overview.getvalue(),
        "warnings": comp_a["warnings"] + comp_b["warnings"],
        "metadata": {
            "source": STAC_URL,
            "collection": COLLECTION,
            "threshold": threshold,
            "period_a": [start_a.isoformat(), end_a.isoformat()],
            "period_b": [start_b.isoformat(), end_b.isoformat()],
            "candidate_scenes_before": search_a["candidate_count"],
            "candidate_scenes_after": search_b["candidate_count"],
            "scenes_before": len(comp_a["scenes"]),
            "scenes_after": len(comp_b["scenes"]),
        },
    }


def _safe_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _masked_mean(array: np.ma.MaskedArray) -> float | None:
    values = array.compressed()
    return float(values.mean()) if values.size else None


def _historical_overview(
    first: dict[str, Any],
    last: dict[str, Any],
    score: np.ma.MaskedArray,
    anomaly: np.ndarray,
    first_year: int,
    last_year: int,
) -> bytes:
    output = BytesIO()
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    axes[0, 0].imshow(first["rgb"])
    axes[0, 0].set_title(f"{first_year} · mosaico anual")
    axes[0, 1].imshow(last["rgb"])
    axes[0, 1].set_title(f"{last_year} · mosaico anual")
    im = axes[1, 0].imshow(score, cmap="magma", vmin=0, vmax=max(0.5, float(score.max())))
    axes[1, 0].set_title("Cambio acumulado primero–último")
    axes[1, 1].imshow(anomaly, cmap="Reds", vmin=0, vmax=1)
    axes[1, 1].set_title("Áreas priorizadas acumuladas")
    for ax in axes.ravel():
        ax.set_axis_off()
    fig.colorbar(im, ax=axes[1, 0], fraction=0.046, pad=0.04)
    fig.tight_layout()
    fig.savefig(output, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    return output.getvalue()


def analyze_historical_series(
    bbox: list[float],
    start_year: int,
    end_year: int,
    window_start: date,
    window_end: date,
    max_cloud: int = 35,
    aoi_geometry: dict[str, Any] | None = None,
    scenes_per_year: int = 3,
    out_size: int = 320,
    threshold: float = 0.24,
    min_patch_pixels: int = 12,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Construye una serie anual comparable y detecta transiciones interanuales."""
    if start_year < ARCHIVE_START_YEAR:
        raise ValueError(f"Sentinel-2 L2A se consulta desde {ARCHIVE_START_YEAR} en este sistema.")
    if end_year < start_year:
        raise ValueError("El año final no puede ser anterior al año inicial.")
    if end_year - start_year > 15:
        raise ValueError("Seleccione un máximo de 16 años por análisis.")
    if (window_end.month, window_end.day) < (window_start.month, window_start.day):
        raise ValueError("La ventana anual debe iniciar y terminar dentro del mismo año.")

    area_km2 = _area_km2(bbox, aoi_geometry)
    if area_km2 > 25:
        raise ValueError(
            f"El área marcada es de {area_km2:.1f} km². Para la serie histórica redúzcala a 25 km² o menos."
        )

    years = list(range(start_year, end_year + 1))
    composites: dict[int, dict[str, Any]] = {}
    annual_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    scene_previews: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, year in enumerate(years, 1):
        if progress_callback:
            progress_callback((index - 1) / len(years), f"Consultando y procesando {year}…")
        start = _safe_date(year, window_start.month, window_start.day)
        end = _safe_date(year, window_end.month, window_end.day)
        try:
            search_result = search_scenes(
                bbox,
                start,
                end,
                max_cloud,
                limit=scenes_per_year,
                candidate_cap=max(20, scenes_per_year * 5),
            )
            comp = build_composite(
                search_result["selected_items"],
                bbox,
                out_size=out_size,
                include_scene_previews=True,
                aoi_geometry=aoi_geometry,
            )
            composites[year] = comp
            cloud_values = [row["cloud_cover"] for row in comp["scenes"]]
            annual_rows.append(
                {
                    "year": year,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "candidate_scenes": search_result["candidate_count"],
                    "used_scenes": len(comp["scenes"]),
                    "mean_cloud_cover": float(np.mean(cloud_values)) if cloud_values else None,
                    "mean_ndvi": _masked_mean(comp["ndvi"]),
                    "mean_ndmi": _masked_mean(comp["ndmi"]),
                    "mean_nbr": _masked_mean(comp["nbr"]),
                    "mean_bsi": _masked_mean(comp["bsi"]),
                    "composite_png": _rgb_png(comp["rgb"]),
                }
            )
            candidate_rows.extend(
                [{"year": year, **row} for row in search_result["candidates"]]
            )
            scene_previews.extend(
                [{"year": year, **row} for row in comp["scene_previews"]]
            )
            warnings.extend([f"{year}: {message}" for message in comp["warnings"]])
        except Exception as exc:
            warnings.append(f"{year}: no fue posible construir el mosaico ({exc})")

    if progress_callback:
        progress_callback(1.0, "Finalizando serie histórica…")

    valid_years = sorted(composites)
    if len(valid_years) < 2:
        raise RuntimeError(
            "Se necesitan al menos dos años con mosaicos válidos. Amplíe la ventana anual o aumente la nubosidad permitida."
        )

    transition_rows: list[dict[str, Any]] = []
    transition_assets: dict[str, dict[str, Any]] = {}
    transition_geojsons: dict[str, dict[str, Any]] = {}
    previous_year = valid_years[0]
    for current_year in valid_years[1:]:
        score, _ = _change_score(composites[previous_year], composites[current_year])
        summary = _score_summary(score, threshold)
        anomaly = np.asarray(score.filled(0) >= threshold, dtype=np.uint8)
        label = f"{previous_year}-{current_year}"
        transition_rows.append(
            {
                "transition": label,
                "from_year": previous_year,
                "to_year": current_year,
                "year_gap": current_year - previous_year,
                "changed_pct": summary["changed_pct"],
                "mean_score": summary["mean_score"],
                "max_score": summary["max_score"],
            }
        )
        transition_assets[label] = {
            "before_png": _rgb_png(composites[previous_year]["rgb"]),
            "after_png": _rgb_png(composites[current_year]["rgb"]),
            "score_png": _png_bytes(
                score,
                f"Cambio {label}",
                "magma",
                0,
                max(0.5, summary["max_score"]),
            ),
            "anomaly_png": _png_bytes(
                anomaly, f"Áreas priorizadas {label}", "Reds", 0, 1
            ),
        }
        transition_geojsons[label] = _anomaly_geojson(
            anomaly, bbox, min_patch_pixels
        )
        for feature in transition_geojsons[label].get("features", []):
            feature.setdefault("properties", {})["transition"] = label
            feature["properties"]["from_year"] = previous_year
            feature["properties"]["to_year"] = current_year
        previous_year = current_year

    transitions = pd.DataFrame(transition_rows)
    strongest_row = max(transition_rows, key=lambda row: row["changed_pct"])
    strongest_label = strongest_row["transition"]

    first_year, last_year = valid_years[0], valid_years[-1]
    accumulated_score, _ = _change_score(composites[first_year], composites[last_year])
    accumulated_summary = _score_summary(accumulated_score, threshold)
    accumulated_anomaly = np.asarray(
        accumulated_score.filled(0) >= threshold, dtype=np.uint8
    )
    accumulated_geojson = _anomaly_geojson(
        accumulated_anomaly, bbox, min_patch_pixels
    )

    annual = pd.DataFrame(
        [{key: value for key, value in row.items() if key != "composite_png"} for row in annual_rows]
    ).sort_values("year")
    annual_gallery = [
        {"year": row["year"], "composite_png": row["composite_png"], "used_scenes": row["used_scenes"],
         "mean_cloud_cover": row["mean_cloud_cover"], "mean_ndvi": row["mean_ndvi"], "mean_bsi": row["mean_bsi"]}
        for row in sorted(annual_rows, key=lambda row: row["year"])
    ]

    total_scenes = len(scene_previews)
    interpretation = (
        f"Se construyeron mosaicos válidos para {len(valid_years)} años utilizando {total_scenes} escenas. "
        f"La transición con mayor proporción de cambio fue {strongest_label} "
        f"({strongest_row['changed_pct']:.1f}% del área válida). La comparación acumulada "
        f"{first_year}–{last_year} prioriza {accumulated_summary['changed_pct']:.1f}% del área. "
        "Estos resultados orientan la revisión visual y de campo; no determinan la causa del cambio."
    )

    return {
        "bbox": bbox,
        "aoi_geometry": aoi_geometry,
        "area_km2": area_km2,
        "valid_years": valid_years,
        "missing_years": [year for year in years if year not in composites],
        "annual": annual,
        "annual_gallery": annual_gallery,
        "transitions": transitions,
        "transition_geojsons": transition_geojsons,
        "scene_previews": scene_previews,
        "candidates": pd.DataFrame(candidate_rows),
        "warnings": warnings,
        "strongest_transition": strongest_row,
        "strongest_assets": transition_assets[strongest_label],
        "accumulated_changed_pct": accumulated_summary["changed_pct"],
        "accumulated_mean_score": accumulated_summary["mean_score"],
        "accumulated_max_score": accumulated_summary["max_score"],
        "accumulated_geojson": accumulated_geojson,
        "historical_overview_png": _historical_overview(
            composites[first_year],
            composites[last_year],
            accumulated_score,
            accumulated_anomaly,
            first_year,
            last_year,
        ),
        "interpretation": interpretation,
        "metadata": {
            "source": STAC_URL,
            "collection": COLLECTION,
            "archive_start_year": ARCHIVE_START_YEAR,
            "requested_years": [start_year, end_year],
            "valid_years": valid_years,
            "window_month_day": [
                f"{window_start.month:02d}-{window_start.day:02d}",
                f"{window_end.month:02d}-{window_end.day:02d}",
            ],
            "max_cloud": max_cloud,
            "scenes_per_year": scenes_per_year,
            "threshold": threshold,
            "total_used_scenes": total_scenes,
        },
    }


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(metadata, default=str))


def comparison_gallery_zip(result: dict[str, Any]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("comparativa_general.png", result["overview_png"])
        archive.writestr("antes_mosaico.png", result["before_png"])
        archive.writestr("despues_mosaico.png", result["after_png"])
        archive.writestr("puntuacion_cambio.png", result["score_png"])
        archive.writestr("anomalias.png", result["anomaly_png"])
        archive.writestr(
            "escenas_utilizadas.csv", result["scenes"].to_csv(index=False).encode("utf-8-sig")
        )
        archive.writestr(
            "escenas_candidatas.csv", result["candidates"].to_csv(index=False).encode("utf-8-sig")
        )
        archive.writestr(
            "anomalias_google_earth.kmz",
            comparison_kmz(result),
        )
        archive.writestr(
            "metadatos.json",
            json.dumps(_clean_metadata(result["metadata"]), ensure_ascii=False, indent=2),
        )
        for index, scene in enumerate(result["scene_previews"], 1):
            dt = (scene.get("datetime") or "sin_fecha")[:10]
            period = scene.get("periodo", "periodo").lower()
            archive.writestr(
                f"escenas/{index:03d}_{period}_{dt}_{scene['scene_id']}.png",
                scene["preview_png"],
            )
    return output.getvalue()


def historical_gallery_zip(result: dict[str, Any]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("resumen_historico.png", result["historical_overview_png"])
        archive.writestr(
            "serie_anual.csv", result["annual"].to_csv(index=False).encode("utf-8-sig")
        )
        archive.writestr(
            "transiciones.csv", result["transitions"].to_csv(index=False).encode("utf-8-sig")
        )
        archive.writestr(
            "escenas_candidatas.csv", result["candidates"].to_csv(index=False).encode("utf-8-sig")
        )
        archive.writestr(
            "anomalias_acumuladas_google_earth.kmz",
            historical_kmz(result),
        )
        archive.writestr(
            "metadatos.json",
            json.dumps(_clean_metadata(result["metadata"]), ensure_ascii=False, indent=2),
        )
        for annual in result["annual_gallery"]:
            archive.writestr(
                f"mosaicos_anuales/{annual['year']}_mosaico.png",
                annual["composite_png"],
            )
        for index, scene in enumerate(result["scene_previews"], 1):
            dt = (scene.get("datetime") or "sin_fecha")[:10]
            archive.writestr(
                f"escenas/{scene['year']}/{index:03d}_{dt}_{scene['scene_id']}.png",
                scene["preview_png"],
            )
        for name, data in result["strongest_assets"].items():
            archive.writestr(f"transicion_mayor/{name}.png", data)
    return output.getvalue()


def report_excel(result: dict[str, Any]) -> bytes:
    output = BytesIO()
    resumen = pd.DataFrame(
        [
            {
                "Área analizada km²": result["area_km2"],
                "Área priorizada km²": result["changed_km2"],
                "Área priorizada %": result["changed_pct"],
                "Prioridad": result["priority"],
                "Puntuación media": result["mean_score"],
                "Puntuación máxima": result["max_score"],
                "Interpretación": result["interpretation"],
            }
        ]
    )
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        resumen.to_excel(writer, index=False, sheet_name="Resumen")
        result["scenes"].to_excel(writer, index=False, sheet_name="Escenas utilizadas")
        result["candidates"].to_excel(writer, index=False, sheet_name="Escenas candidatas")
        pd.DataFrame(result["geojson"]["features"]).to_excel(
            writer, index=False, sheet_name="Anomalias"
        )
    return output.getvalue()


def historical_report_excel(result: dict[str, Any]) -> bytes:
    output = BytesIO()
    strongest = result["strongest_transition"]
    summary = pd.DataFrame(
        [
            {
                "Área analizada km²": result["area_km2"],
                "Años válidos": len(result["valid_years"]),
                "Años sin mosaico": ", ".join(map(str, result["missing_years"])) or "Ninguno",
                "Transición de mayor cambio": strongest["transition"],
                "Cambio máximo %": strongest["changed_pct"],
                "Cambio acumulado %": result["accumulated_changed_pct"],
                "Interpretación": result["interpretation"],
            }
        ]
    )
    scene_rows = [
        {key: value for key, value in scene.items() if key != "preview_png"}
        for scene in result["scene_previews"]
    ]
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, index=False, sheet_name="Resumen")
        result["annual"].to_excel(writer, index=False, sheet_name="Serie anual")
        result["transitions"].to_excel(writer, index=False, sheet_name="Transiciones")
        pd.DataFrame(scene_rows).to_excel(writer, index=False, sheet_name="Escenas utilizadas")
        result["candidates"].to_excel(writer, index=False, sheet_name="Escenas candidatas")
        pd.DataFrame(result["accumulated_geojson"]["features"]).to_excel(
            writer, index=False, sheet_name="Anomalias acumuladas"
        )
    return output.getvalue()
