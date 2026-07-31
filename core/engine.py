from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import planetary_computer as pc
import rasterio
from PIL import Image
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"

@dataclass
class SceneInfo:
    scene_id: str
    datetime: str | None
    cloud_cover: float


def _catalog() -> Client:
    return Client.open(STAC_URL, modifier=pc.sign_inplace)


def search_scenes(bbox: list[float], start: date, end: date, max_cloud: int, limit: int = 12):
    search = _catalog().search(
        collections=[COLLECTION],
        bbox=bbox,
        datetime=f"{start.isoformat()}/{end.isoformat()}",
        query={"eo:cloud_cover": {"lte": max_cloud}},
        sortby=[{"field": "properties.eo:cloud_cover", "direction": "asc"}],
        limit=max(limit * 3, 30),
    )
    items = list(search.items())
    if not items:
        raise RuntimeError("No se encontraron imágenes Sentinel-2 para el periodo y nubosidad seleccionados.")
    return items[:limit]


def _asset(item, *names: str) -> str:
    for name in names:
        if name in item.assets:
            return item.assets[name].href
    raise KeyError(f"No se localizó la banda requerida: {names}")


def _read_asset(href: str, bbox: list[float], out_size: int, categorical: bool = False) -> np.ma.MaskedArray:
    with rasterio.open(href) as src:
        left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
        window = from_bounds(left, bottom, right, top, src.transform)
        if window.width <= 0 or window.height <= 0:
            raise RuntimeError("El área marcada no intersecta la escena satelital.")
        return src.read(
            1,
            window=window,
            out_shape=(out_size, out_size),
            resampling=Resampling.nearest if categorical else Resampling.bilinear,
            masked=True,
        ).astype("float32")


def _scene_arrays(item, bbox: list[float], out_size: int) -> dict[str, np.ma.MaskedArray]:
    blue = _read_asset(_asset(item, "B02", "blue"), bbox, out_size)
    green = _read_asset(_asset(item, "B03", "green"), bbox, out_size)
    red = _read_asset(_asset(item, "B04", "red"), bbox, out_size)
    nir = _read_asset(_asset(item, "B08", "nir"), bbox, out_size)
    swir1 = _read_asset(_asset(item, "B11", "swir16"), bbox, out_size)
    swir2 = _read_asset(_asset(item, "B12", "swir22"), bbox, out_size)
    scl = _read_asset(_asset(item, "SCL", "scl"), bbox, out_size, categorical=True)

    invalid_scl = np.isin(np.asarray(scl.filled(0), dtype=np.int16), [0, 1, 3, 8, 9, 10, 11])
    common = invalid_scl
    for arr in (blue, green, red, nir, swir1, swir2):
        common |= np.ma.getmaskarray(arr)
    return {k: np.ma.array(v, mask=common) for k, v in {
        "blue": blue, "green": green, "red": red, "nir": nir, "swir1": swir1, "swir2": swir2
    }.items()}


def _median_stack(arrays: list[np.ma.MaskedArray]) -> np.ma.MaskedArray:
    if not arrays:
        raise RuntimeError("No hay imágenes válidas para construir el mosaico.")
    stack = np.ma.stack(arrays, axis=0)
    return np.ma.median(stack, axis=0)


def _index(a: np.ma.MaskedArray, b: np.ma.MaskedArray) -> np.ma.MaskedArray:
    den = a + b
    return np.ma.clip(np.ma.masked_where(np.abs(den) < 1e-8, (a - b) / den), -1, 1)


def build_composite(items, bbox: list[float], out_size: int = 512) -> dict[str, Any]:
    stacks: dict[str, list[np.ma.MaskedArray]] = {k: [] for k in ("blue", "green", "red", "nir", "swir1", "swir2")}
    used: list[SceneInfo] = []
    errors: list[str] = []
    for item in items:
        try:
            data = _scene_arrays(item, bbox, out_size)
            valid = 100 * (1 - np.mean(np.ma.getmaskarray(data["red"])))
            if valid < 25:
                errors.append(f"{item.id}: cobertura válida insuficiente ({valid:.1f}%).")
                continue
            for key in stacks:
                stacks[key].append(data[key])
            used.append(SceneInfo(item.id, item.datetime.isoformat() if item.datetime else None, float(item.properties.get("eo:cloud_cover", 0))))
        except Exception as exc:
            errors.append(f"{item.id}: {exc}")
    if not used:
        raise RuntimeError("No fue posible construir un mosaico válido. Amplíe el periodo o aumente la nubosidad permitida.")

    c = {key: _median_stack(values) for key, values in stacks.items()}
    ndvi = _index(c["nir"], c["red"])
    ndmi = _index(c["nir"], c["swir1"])
    nbr = _index(c["nir"], c["swir2"])
    bsi_num = (c["swir1"] + c["red"]) - (c["nir"] + c["blue"])
    bsi_den = (c["swir1"] + c["red"]) + (c["nir"] + c["blue"])
    bsi = np.ma.clip(np.ma.masked_where(np.abs(bsi_den) < 1e-8, bsi_num / bsi_den), -1, 1)
    rgb = np.ma.dstack((c["red"], c["green"], c["blue"]))
    valid = rgb.compressed()
    if valid.size:
        lo, hi = np.percentile(valid, [2, 98])
        rgb = np.ma.clip((rgb - lo) / max(float(hi - lo), 1e-6), 0, 1)
    return {"rgb": rgb, "ndvi": ndvi, "ndmi": ndmi, "nbr": nbr, "bsi": bsi, "scenes": [asdict(s) for s in used], "warnings": errors}


def _png_bytes(array, title: str, cmap=None, vmin=None, vmax=None) -> bytes:
    fig, ax = plt.subplots(figsize=(7, 6))
    ax.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_title(title)
    ax.set_axis_off()
    fig.tight_layout()
    bio = BytesIO()
    fig.savefig(bio, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    return bio.getvalue()


def _area_km2(bbox: list[float]) -> float:
    lon_mid = (bbox[0] + bbox[2]) / 2
    lat_mid = (bbox[1] + bbox[3]) / 2
    width_km = abs(bbox[2] - bbox[0]) * 111.32 * np.cos(np.radians(lat_mid))
    height_km = abs(bbox[3] - bbox[1]) * 110.57
    return float(width_km * height_km)


def analyze_change(
    bbox: list[float], start_a: date, end_a: date, start_b: date, end_b: date,
    max_cloud: int = 35, scenes_per_period: int = 8, out_size: int = 512,
    threshold: float = 0.24, min_patch_pixels: int = 20,
) -> dict[str, Any]:
    if start_a > end_a or start_b > end_b:
        raise ValueError("Las fechas de inicio deben ser anteriores a las fechas finales.")
    area_km2 = _area_km2(bbox)
    if area_km2 > 40:
        raise ValueError(f"El área marcada es de {area_km2:.1f} km². Redúzcala a 40 km² o menos.")

    comp_a = build_composite(search_scenes(bbox, start_a, end_a, max_cloud, scenes_per_period), bbox, out_size)
    comp_b = build_composite(search_scenes(bbox, start_b, end_b, max_cloud, scenes_per_period), bbox, out_size)

    mask = np.ma.getmaskarray(comp_a["ndvi"]) | np.ma.getmaskarray(comp_b["ndvi"])
    deltas = {name: np.ma.array(comp_b[name] - comp_a[name], mask=mask) for name in ("ndvi", "ndmi", "nbr", "bsi")}
    score = (
        np.abs(deltas["ndvi"]) * 0.40 +
        np.abs(deltas["ndmi"]) * 0.22 +
        np.abs(deltas["nbr"]) * 0.20 +
        np.maximum(deltas["bsi"], 0) * 0.18
    )
    score = np.ma.clip(score, 0, 1)
    anomaly = np.asarray(score.filled(0) >= threshold, dtype=np.uint8)

    transform = transform_from_bounds(*bbox, out_size, out_size)
    geoms = []
    for geom, value in shapes(anomaly, mask=anomaly.astype(bool), transform=transform):
        if value != 1:
            continue
        poly = shape(geom)
        if poly.area <= 0:
            continue
        approx_pixels = poly.area / (abs(transform.a * transform.e))
        if approx_pixels >= min_patch_pixels:
            geoms.append(poly)
    merged = unary_union(geoms) if geoms else None
    features = []
    if merged and not merged.is_empty:
        parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
        for idx, poly in enumerate(parts, 1):
            features.append({"type": "Feature", "properties": {"id": idx, "clasificacion": "Área prioritaria para revisión"}, "geometry": mapping(poly)})

    valid = score.compressed()
    pct = float(np.mean(valid >= threshold) * 100) if valid.size else 0.0
    area_changed = area_km2 * pct / 100
    mean_score = float(valid.mean()) if valid.size else 0.0
    max_score = float(valid.max()) if valid.size else 0.0
    if pct < 2: priority = "Baja"
    elif pct < 8: priority = "Media"
    elif pct < 20: priority = "Alta"
    else: priority = "Muy alta"

    interpretation = (
        f"Se prioriza {pct:.1f}% del área marcada ({area_changed:.3f} km²) por cambios multiespectrales. "
        "El resultado indica zonas que requieren revisión visual, contraste histórico y verificación de campo; no confirma por sí solo una fosa, delito o hallazgo."
    )

    overview = BytesIO()
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    axes[0,0].imshow(comp_a["rgb"]); axes[0,0].set_title("ANTES · mosaico limpio")
    axes[0,1].imshow(comp_b["rgb"]); axes[0,1].set_title("DESPUÉS · mosaico limpio")
    im = axes[1,0].imshow(score, cmap="magma", vmin=0, vmax=max(0.5, max_score)); axes[1,0].set_title("Puntuación multiespectral")
    axes[1,1].imshow(anomaly, cmap="Reds", vmin=0, vmax=1); axes[1,1].set_title("Áreas priorizadas")
    for ax in axes.ravel(): ax.set_axis_off()
    fig.colorbar(im, ax=axes[1,0], fraction=.046, pad=.04)
    fig.tight_layout(); fig.savefig(overview, format="png", dpi=160, bbox_inches="tight"); plt.close(fig)

    geojson = {"type": "FeatureCollection", "features": features}
    scenes = pd.DataFrame([{"periodo": "Antes", **x} for x in comp_a["scenes"]] + [{"periodo": "Después", **x} for x in comp_b["scenes"]])

    return {
        "bbox": bbox, "area_km2": area_km2, "changed_pct": pct, "changed_km2": area_changed,
        "priority": priority, "mean_score": mean_score, "max_score": max_score,
        "interpretation": interpretation, "geojson": geojson, "scenes": scenes,
        "before_png": _png_bytes(comp_a["rgb"], "ANTES · mosaico satelital"),
        "after_png": _png_bytes(comp_b["rgb"], "DESPUÉS · mosaico satelital"),
        "score_png": _png_bytes(score, "Puntuación de cambio", "magma", 0, max(0.5, max_score)),
        "anomaly_png": _png_bytes(anomaly, "Áreas priorizadas", "Reds", 0, 1),
        "overview_png": overview.getvalue(),
        "warnings": comp_a["warnings"] + comp_b["warnings"],
        "metadata": {
            "source": STAC_URL, "collection": COLLECTION, "threshold": threshold,
            "period_a": [start_a.isoformat(), end_a.isoformat()], "period_b": [start_b.isoformat(), end_b.isoformat()],
            "scenes_before": len(comp_a["scenes"]), "scenes_after": len(comp_b["scenes"]),
        },
    }


def report_excel(result: dict[str, Any]) -> bytes:
    output = BytesIO()
    resumen = pd.DataFrame([{
        "Área analizada km²": result["area_km2"], "Área priorizada km²": result["changed_km2"],
        "Área priorizada %": result["changed_pct"], "Prioridad": result["priority"],
        "Puntuación media": result["mean_score"], "Puntuación máxima": result["max_score"],
        "Interpretación": result["interpretation"],
    }])
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        resumen.to_excel(writer, index=False, sheet_name="Resumen")
        result["scenes"].to_excel(writer, index=False, sheet_name="Escenas")
        pd.DataFrame(result["geojson"]["features"]).to_excel(writer, index=False, sheet_name="Anomalias")
    return output.getvalue()
