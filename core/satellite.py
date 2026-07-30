from __future__ import annotations
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
import numpy as np
import matplotlib.pyplot as plt
import planetary_computer as pc
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from pystac_client import Client

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

def search_best_scene(bbox: list[float], start: date, end: date, max_cloud: int = 30):
    catalog = Client.open(STAC_URL, modifier=pc.sign_inplace)
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=bbox,
        datetime=f"{start.isoformat()}/{end.isoformat()}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        sortby=[{"field": "properties.eo:cloud_cover", "direction": "asc"}],
        limit=50,
    )
    items = list(search.items())
    if not items:
        raise RuntimeError("No se encontraron escenas con los filtros seleccionados.")
    return items[0]

def _asset_href(item, candidates: tuple[str, ...]) -> str:
    for name in candidates:
        if name in item.assets:
            return item.assets[name].href
    raise KeyError(f"No se encontró ninguna banda: {candidates}")

def _read_bbox(href: str, bbox: list[float], out_size: int = 512):
    with rasterio.open(href) as src:
        left, bottom, right, top = rasterio.warp.transform_bounds(
            "EPSG:4326", src.crs, *bbox, densify_pts=21
        )
        win = from_bounds(left, bottom, right, top, transform=src.transform)
        if win.width <= 0 or win.height <= 0:
            raise RuntimeError("El área no intersecta la escena.")
        arr = src.read(
            1,
            window=win,
            out_shape=(out_size, out_size),
            resampling=Resampling.bilinear,
            masked=True
        ).astype("float32")
        return arr

def compute_scene_layers(item, bbox: list[float], out_size: int = 512):
    blue = _read_bbox(_asset_href(item, ("B02", "blue")), bbox, out_size)
    green = _read_bbox(_asset_href(item, ("B03", "green")), bbox, out_size)
    red = _read_bbox(_asset_href(item, ("B04", "red")), bbox, out_size)
    nir = _read_bbox(_asset_href(item, ("B08", "nir")), bbox, out_size)
    common = (
        np.ma.getmaskarray(blue)
        | np.ma.getmaskarray(green)
        | np.ma.getmaskarray(red)
        | np.ma.getmaskarray(nir)
    )
    blue, green, red, nir = [np.ma.array(x, mask=common) for x in (blue, green, red, nir)]
    den = nir + red
    ndvi = np.ma.masked_where((np.abs(den) < 1e-8) | common, (nir - red) / den)
    ndvi = np.ma.clip(ndvi, -1, 1)

    rgb = np.ma.dstack((red, green, blue))
    valid = rgb.compressed()
    if valid.size:
        low, high = np.percentile(valid, (2, 98))
        scale = max(float(high - low), 1e-6)
        rgb = np.ma.clip((rgb - low) / scale, 0, 1)
    return {"rgb": rgb, "ndvi": ndvi}


def compute_ndvi(item, bbox: list[float], out_size: int = 512):
    return compute_scene_layers(item, bbox, out_size)["ndvi"]

def compare_periods(name: str, bbox: list[float], start_a: date, end_a: date,
                    start_b: date, end_b: date, max_cloud: int = 30,
                    out_dir: str = "data/analisis") -> dict[str, Any]:
    width_deg = abs(bbox[2] - bbox[0])
    height_deg = abs(bbox[3] - bbox[1])
    if width_deg * height_deg > 0.03:
        raise ValueError("El área es demasiado grande. Reduzca el rectángulo a aproximadamente 25–35 km².")
    if start_a > end_a or start_b > end_b:
        raise ValueError("Las fechas iniciales deben ser anteriores a las fechas finales.")

    item_a = search_best_scene(bbox, start_a, end_a, max_cloud)
    item_b = search_best_scene(bbox, start_b, end_b, max_cloud)
    layers_a = compute_scene_layers(item_a, bbox)
    layers_b = compute_scene_layers(item_b, bbox)
    ndvi_a, ndvi_b = layers_a["ndvi"], layers_b["ndvi"]
    common_mask = np.ma.getmaskarray(ndvi_a) | np.ma.getmaskarray(ndvi_b)
    diff = np.ma.array(ndvi_b - ndvi_a, mask=common_mask)
    anomaly_mask = np.ma.array(np.abs(diff) >= 0.20, mask=common_mask)

    mean_a = float(ndvi_a.mean())
    mean_b = float(ndvi_b.mean())
    change = float(diff.mean())
    valid = diff.compressed()
    pct_significant = float(np.mean(np.abs(valid) >= 0.20) * 100) if valid.size else 0.0
    decrease_pct = float(np.mean(valid <= -0.20) * 100) if valid.size else 0.0
    increase_pct = float(np.mean(valid >= 0.20) * 100) if valid.size else 0.0

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_")[:50] or "analisis"
    stem = Path(out_dir) / f"{safe_name}_{item_a.id[:12]}_{item_b.id[:12]}"
    overview_png = Path(str(stem) + "_comparacion.png")
    before_png = Path(str(stem) + "_antes.png")
    after_png = Path(str(stem) + "_despues.png")
    anomaly_png = Path(str(stem) + "_anomalias.png")

    def save_image(array, path, cmap=None, vmin=None, vmax=None, title=""):
        fig, ax = plt.subplots(figsize=(7, 6))
        ax.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax)
        ax.set_title(title)
        ax.set_axis_off()
        fig.tight_layout()
        fig.savefig(path, dpi=170, bbox_inches="tight")
        plt.close(fig)

    save_image(layers_a["rgb"], before_png, title=f"Antes · {item_a.datetime.date() if item_a.datetime else start_a}")
    save_image(layers_b["rgb"], after_png, title=f"Después · {item_b.datetime.date() if item_b.datetime else start_b}")
    save_image(anomaly_mask, anomaly_png, cmap="Reds", vmin=0, vmax=1, title="Zonas con cambio fuerte (|ΔNDVI| ≥ 0.20)")

    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    axes[0, 0].imshow(layers_a["rgb"]); axes[0, 0].set_title("ANTES · imagen satelital")
    axes[0, 1].imshow(layers_b["rgb"]); axes[0, 1].set_title("DESPUÉS · imagen satelital")
    im = axes[1, 0].imshow(diff, vmin=-0.6, vmax=0.6, cmap="RdYlGn")
    axes[1, 0].set_title("CAMBIO · ΔNDVI")
    axes[1, 1].imshow(anomaly_mask, cmap="Reds", vmin=0, vmax=1)
    axes[1, 1].set_title("ANOMALÍAS PRIORIZADAS")
    for ax in axes.ravel(): ax.set_axis_off()
    fig.colorbar(im, ax=axes[1, 0], fraction=0.046, pad=0.04, label="Δ NDVI")
    fig.suptitle("Comparación territorial del área marcada", fontsize=15)
    fig.tight_layout()
    fig.savefig(overview_png, dpi=170, bbox_inches="tight")
    plt.close(fig)

    if pct_significant < 5:
        interpretation = "No se observa una modificación superficial fuerte en una proporción relevante del área."
        priority = "Baja"
    elif pct_significant < 15:
        interpretation = "Se identifican cambios localizados que requieren revisión visual y contraste con información de campo."
        priority = "Media"
    elif pct_significant < 30:
        interpretation = "Se observa una modificación territorial relevante y concentrada dentro del área marcada."
        priority = "Alta"
    else:
        interpretation = "Se detecta una modificación extensa del terreno; debe revisarse la estacionalidad, actividad agrícola y antecedentes del sitio."
        priority = "Muy alta"

    return {
        "nombre": name,
        "bbox": bbox,
        "fecha_a": f"{start_a}/{end_a}",
        "fecha_b": f"{start_b}/{end_b}",
        "fecha_escena_a": item_a.datetime.isoformat() if item_a.datetime else None,
        "fecha_escena_b": item_b.datetime.isoformat() if item_b.datetime else None,
        "escena_a": item_a.id,
        "escena_b": item_b.id,
        "nube_a": float(item_a.properties.get("eo:cloud_cover", 0)),
        "nube_b": float(item_b.properties.get("eo:cloud_cover", 0)),
        "media_ndvi_a": mean_a,
        "media_ndvi_b": mean_b,
        "cambio_medio": change,
        "porcentaje_cambio": pct_significant,
        "porcentaje_disminucion": decrease_pct,
        "porcentaje_aumento": increase_pct,
        "prioridad_automatica": priority,
        "interpretacion": interpretation,
        "raster_png": str(overview_png),
        "before_png": str(before_png),
        "after_png": str(after_png),
        "anomaly_png": str(anomaly_png),
        "metadata_json": json.dumps({
            "item_a_datetime": item_a.datetime.isoformat() if item_a.datetime else None,
            "item_b_datetime": item_b.datetime.isoformat() if item_b.datetime else None,
            "source": STAC_URL,
            "threshold_abs_ndvi": 0.20,
            "warning": "Una anomalía territorial no confirma por sí sola un hallazgo ni un hecho delictivo."
        }, ensure_ascii=False)
    }

