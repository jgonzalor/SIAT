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

def compute_ndvi(item, bbox: list[float], out_size: int = 512):
    red = _read_bbox(_asset_href(item, ("B04", "red")), bbox, out_size)
    nir = _read_bbox(_asset_href(item, ("B08", "nir")), bbox, out_size)
    den = nir + red
    ndvi = np.ma.masked_where((den == 0) | red.mask | nir.mask, (nir - red) / den)
    return np.ma.clip(ndvi, -1, 1)

def compare_periods(name: str, bbox: list[float], start_a: date, end_a: date,
                    start_b: date, end_b: date, max_cloud: int = 30,
                    out_dir: str = "data/analisis") -> dict[str, Any]:
    # Safety: prevents accidentally processing a huge area in a web session.
    width_deg = abs(bbox[2] - bbox[0])
    height_deg = abs(bbox[3] - bbox[1])
    if width_deg * height_deg > 0.03:
        raise ValueError("El polígono es demasiado grande para el MVP. Reduzca el área (aprox. hasta 25–35 km²).")

    item_a = search_best_scene(bbox, start_a, end_a, max_cloud)
    item_b = search_best_scene(bbox, start_b, end_b, max_cloud)
    ndvi_a = compute_ndvi(item_a, bbox)
    ndvi_b = compute_ndvi(item_b, bbox)
    common_mask = np.ma.getmaskarray(ndvi_a) | np.ma.getmaskarray(ndvi_b)
    diff = np.ma.array(ndvi_b - ndvi_a, mask=common_mask)

    mean_a = float(ndvi_a.mean())
    mean_b = float(ndvi_b.mean())
    change = float(diff.mean())
    valid = diff.compressed()
    pct_significant = float(np.mean(np.abs(valid) >= 0.20) * 100) if valid.size else 0.0

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_")[:50] or "analisis"
    png = Path(out_dir) / f"{safe_name}_{item_a.id[:12]}_{item_b.id[:12]}.png"

    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(diff, vmin=-0.6, vmax=0.6, cmap="RdYlGn")
    ax.set_title("Cambio NDVI: periodo B − periodo A")
    ax.set_axis_off()
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("Δ NDVI")
    fig.tight_layout()
    fig.savefig(png, dpi=160, bbox_inches="tight")
    plt.close(fig)

    return {
        "nombre": name,
        "bbox": bbox,
        "fecha_a": f"{start_a}/{end_a}",
        "fecha_b": f"{start_b}/{end_b}",
        "escena_a": item_a.id,
        "escena_b": item_b.id,
        "nube_a": float(item_a.properties.get("eo:cloud_cover", 0)),
        "nube_b": float(item_b.properties.get("eo:cloud_cover", 0)),
        "media_ndvi_a": mean_a,
        "media_ndvi_b": mean_b,
        "cambio_medio": change,
        "porcentaje_cambio": pct_significant,
        "raster_png": str(png),
        "metadata_json": json.dumps({
            "item_a_datetime": item_a.datetime.isoformat() if item_a.datetime else None,
            "item_b_datetime": item_b.datetime.isoformat() if item_b.datetime else None,
            "source": STAC_URL,
            "threshold_abs_ndvi": 0.20
        }, ensure_ascii=False)
    }
