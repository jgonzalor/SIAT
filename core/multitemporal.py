from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
import numpy as np

from core.satellite import _asset_href, _read_bbox, search_best_scene


INDEX_LABELS = {
    "NDVI": "Vegetación",
    "NDMI": "Humedad",
    "NBR": "Alteración / quema",
    "BSI": "Suelo desnudo",
}


@dataclass
class TemporalObservation:
    year: int
    period_start: str
    period_end: str
    scene_id: str | None
    scene_datetime: str | None
    cloud_cover: float | None
    ndvi: float | None
    ndmi: float | None
    nbr: float | None
    bsi: float | None
    valid_pct: float | None
    status: str
    error: str | None = None


def validate_bbox(bbox: list[float], max_area_deg2: float = 0.03) -> None:
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError("El área territorial no es válida.")
    if abs(bbox[2] - bbox[0]) * abs(bbox[3] - bbox[1]) > max_area_deg2:
        raise ValueError("El área es demasiado grande. Reduzca el rectángulo a aproximadamente 25–35 km².")


def _safe_ratio(a: np.ma.MaskedArray, b: np.ma.MaskedArray) -> np.ma.MaskedArray:
    den = a + b
    mask = np.ma.getmaskarray(a) | np.ma.getmaskarray(b) | (np.abs(den) < 1e-8)
    return np.ma.clip(np.ma.array((a - b) / den, mask=mask), -1, 1)


def compute_indices(item: Any, bbox: list[float], out_size: int = 384) -> dict[str, np.ma.MaskedArray]:
    blue = _read_bbox(_asset_href(item, ("B02", "blue")), bbox, out_size)
    green = _read_bbox(_asset_href(item, ("B03", "green")), bbox, out_size)
    red = _read_bbox(_asset_href(item, ("B04", "red")), bbox, out_size)
    nir = _read_bbox(_asset_href(item, ("B08", "nir")), bbox, out_size)
    swir1 = _read_bbox(_asset_href(item, ("B11", "swir16")), bbox, out_size)
    swir2 = _read_bbox(_asset_href(item, ("B12", "swir22")), bbox, out_size)

    common = (
        np.ma.getmaskarray(blue)
        | np.ma.getmaskarray(green)
        | np.ma.getmaskarray(red)
        | np.ma.getmaskarray(nir)
        | np.ma.getmaskarray(swir1)
        | np.ma.getmaskarray(swir2)
    )
    arrays = [np.ma.array(x, mask=common) for x in (blue, green, red, nir, swir1, swir2)]
    blue, green, red, nir, swir1, swir2 = arrays

    ndvi = _safe_ratio(nir, red)
    ndmi = _safe_ratio(nir, swir1)
    nbr = _safe_ratio(nir, swir2)
    bsi_den = swir1 + red + nir + blue
    bsi_mask = common | (np.abs(bsi_den) < 1e-8)
    bsi = np.ma.clip(np.ma.array(((swir1 + red) - (nir + blue)) / bsi_den, mask=bsi_mask), -1, 1)
    return {"NDVI": ndvi, "NDMI": ndmi, "NBR": nbr, "BSI": bsi}


def _mean(arr: np.ma.MaskedArray) -> float | None:
    values = arr.compressed()
    return float(values.mean()) if values.size else None


def _valid_pct(arr: np.ma.MaskedArray) -> float:
    return float((~np.ma.getmaskarray(arr)).mean() * 100)


def _period_for_year(year: int, month_start: int, month_end: int) -> tuple[date, date]:
    if not 1 <= month_start <= 12 or not 1 <= month_end <= 12:
        raise ValueError("Los meses deben encontrarse entre 1 y 12.")
    if month_start > month_end:
        raise ValueError("El periodo estacional no puede cruzar de diciembre a enero en esta versión.")
    start = date(year, month_start, 1)
    if month_end == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month_end + 1, 1).fromordinal(date(year, month_end + 1, 1).toordinal() - 1)
    return start, end


def collect_yearly_series(
    bbox: list[float],
    start_year: int,
    end_year: int,
    month_start: int,
    month_end: int,
    max_cloud: int = 30,
    out_size: int = 384,
) -> tuple[list[TemporalObservation], dict[int, dict[str, np.ma.MaskedArray]]]:
    validate_bbox(bbox)
    if start_year > end_year:
        raise ValueError("El año inicial debe ser menor o igual al final.")
    if end_year - start_year > 15:
        raise ValueError("Para una sesión web, limite la consulta a 16 años por ejecución.")

    observations: list[TemporalObservation] = []
    rasters: dict[int, dict[str, np.ma.MaskedArray]] = {}
    for year in range(start_year, end_year + 1):
        start, end = _period_for_year(year, month_start, month_end)
        try:
            item = search_best_scene(bbox, start, end, max_cloud)
            indices = compute_indices(item, bbox, out_size)
            rasters[year] = indices
            observations.append(
                TemporalObservation(
                    year=year,
                    period_start=start.isoformat(),
                    period_end=end.isoformat(),
                    scene_id=item.id,
                    scene_datetime=item.datetime.isoformat() if item.datetime else None,
                    cloud_cover=float(item.properties.get("eo:cloud_cover", 0)),
                    ndvi=_mean(indices["NDVI"]),
                    ndmi=_mean(indices["NDMI"]),
                    nbr=_mean(indices["NBR"]),
                    bsi=_mean(indices["BSI"]),
                    valid_pct=_valid_pct(indices["NDVI"]),
                    status="Disponible",
                )
            )
        except Exception as exc:
            observations.append(
                TemporalObservation(
                    year=year,
                    period_start=start.isoformat(),
                    period_end=end.isoformat(),
                    scene_id=None,
                    scene_datetime=None,
                    cloud_cover=None,
                    ndvi=None,
                    ndmi=None,
                    nbr=None,
                    bsi=None,
                    valid_pct=None,
                    status="Sin datos",
                    error=str(exc),
                )
            )
    return observations, rasters


def _robust_z(values: list[float | None]) -> list[float | None]:
    valid = np.array([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if valid.size < 3:
        return [0.0 if v is not None else None for v in values]
    med = float(np.median(valid))
    mad = float(np.median(np.abs(valid - med)))
    scale = 1.4826 * mad
    if scale < 1e-7:
        std = float(valid.std())
        scale = std if std > 1e-7 else 1.0
    return [None if v is None else float((v - med) / scale) for v in values]


def detect_temporal_changes(observations: list[TemporalObservation]) -> dict[str, Any]:
    available = [o for o in observations if o.status == "Disponible" and o.ndvi is not None]
    if len(available) < 2:
        return {
            "classification": "No concluyente",
            "last_stable": None,
            "first_change": None,
            "probable_window": None,
            "peak_year": None,
            "peak_score": 0.0,
            "scores": [],
            "explanation": "No existen al menos dos observaciones comparables.",
        }

    z_ndvi = _robust_z([o.ndvi for o in available])
    z_ndmi = _robust_z([o.ndmi for o in available])
    z_nbr = _robust_z([o.nbr for o in available])
    z_bsi = _robust_z([o.bsi for o in available])

    scores: list[dict[str, Any]] = []
    for i, obs in enumerate(available):
        if i == 0:
            scores.append({"year": obs.year, "score": 0.0, "delta_ndvi": None, "delta_bsi": None})
            continue
        prev = available[i - 1]
        d_ndvi = (obs.ndvi or 0) - (prev.ndvi or 0)
        d_ndmi = (obs.ndmi or 0) - (prev.ndmi or 0)
        d_nbr = (obs.nbr or 0) - (prev.nbr or 0)
        d_bsi = (obs.bsi or 0) - (prev.bsi or 0)
        annual = min(100.0, (
            abs(d_ndvi) * 150
            + abs(d_ndmi) * 100
            + abs(d_nbr) * 100
            + max(0.0, d_bsi) * 130
            + min(20.0, abs(z_ndvi[i] or 0) * 5)
            + min(15.0, abs(z_bsi[i] or 0) * 4)
        ))
        scores.append({
            "year": obs.year,
            "score": round(float(annual), 1),
            "delta_ndvi": round(float(d_ndvi), 4),
            "delta_ndmi": round(float(d_ndmi), 4),
            "delta_nbr": round(float(d_nbr), 4),
            "delta_bsi": round(float(d_bsi), 4),
        })

    peak = max(scores, key=lambda x: x["score"])
    threshold = max(25.0, float(np.mean([x["score"] for x in scores])) + float(np.std([x["score"] for x in scores])))
    flagged = [x for x in scores if x["score"] >= threshold]
    first = flagged[0] if flagged else None

    classification = "Sin cambio relevante"
    explanation = "La serie no muestra un salto multiespectral suficientemente fuerte."
    if first:
        idx = next(i for i, x in enumerate(scores) if x["year"] == first["year"])
        later = [x["score"] for x in scores[idx + 1:]]
        if peak["score"] >= 70:
            classification = "Repentina"
        else:
            classification = "Cambio relevante"
        if later and sum(v >= 20 for v in later) >= max(1, len(later) // 2):
            classification = "Persistente"
        elif later and later[-1] < 15:
            classification = "Revertida"
        elif len(flagged) >= 3 and flagged[-1]["year"] - flagged[0]["year"] >= 2:
            classification = "Progresiva"
        explanation = f"El mayor salto compuesto se observó en {peak['year']} con puntuación {peak['score']}/100."

    first_year = first["year"] if first else None
    prev_year = None
    if first_year is not None:
        previous = [o.year for o in available if o.year < first_year]
        prev_year = max(previous) if previous else None

    return {
        "classification": classification,
        "last_stable": prev_year,
        "first_change": first_year,
        "probable_window": f"{prev_year}–{first_year}" if prev_year else None,
        "peak_year": peak["year"],
        "peak_score": peak["score"],
        "threshold": round(threshold, 1),
        "scores": scores,
        "explanation": explanation,
    }


def _safe_name(name: str) -> str:
    clean = "".join(c for c in name if c.isalnum() or c in "-_ ").strip().replace(" ", "_")
    return clean[:70] or "analisis_historico"


def create_outputs(
    name: str,
    observations: list[TemporalObservation],
    rasters: dict[int, dict[str, np.ma.MaskedArray]],
    detection: dict[str, Any],
    out_dir: str = "data/analisis_historicos",
) -> dict[str, str]:
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(name)
    series_png = folder / f"{stem}_serie.png"
    change_png = folder / f"{stem}_cambio.png"
    json_path = folder / f"{stem}_resultado.json"
    csv_path = folder / f"{stem}_serie.csv"

    available = [o for o in observations if o.status == "Disponible"]
    years = [o.year for o in available]
    if years:
        fig, ax = plt.subplots(figsize=(10, 5.5))
        for field, label in [("ndvi", "NDVI"), ("ndmi", "NDMI"), ("nbr", "NBR"), ("bsi", "BSI")]:
            ax.plot(years, [getattr(o, field) for o in available], marker="o", label=label)
        if detection.get("first_change"):
            ax.axvline(detection["first_change"], linestyle="--", linewidth=1.5, label="Primer cambio priorizado")
        ax.set_title("Evolución multitemporal de índices territoriales")
        ax.set_xlabel("Año")
        ax.set_ylabel("Valor medio del índice")
        ax.grid(True, alpha=0.25)
        ax.legend(ncol=3)
        fig.tight_layout()
        fig.savefig(series_png, dpi=170, bbox_inches="tight")
        plt.close(fig)

    first_year = detection.get("last_stable")
    second_year = detection.get("first_change") or detection.get("peak_year")
    if first_year in rasters and second_year in rasters:
        fig, axes = plt.subplots(2, 2, figsize=(10, 8))
        for ax, index in zip(axes.ravel(), ("NDVI", "NDMI", "NBR", "BSI")):
            diff = np.ma.array(rasters[second_year][index] - rasters[first_year][index])
            im = ax.imshow(diff, vmin=-0.5, vmax=0.5, cmap="RdBu_r")
            ax.set_title(f"Δ {index}: {second_year} − {first_year}")
            ax.set_axis_off()
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
        fig.suptitle("Mapa multiespectral del intervalo probable de cambio")
        fig.tight_layout()
        fig.savefig(change_png, dpi=170, bbox_inches="tight")
        plt.close(fig)

    rows = [asdict(o) for o in observations]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["year"])
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps({"observations": rows, "detection": detection}, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "series_png": str(series_png) if series_png.exists() else "",
        "change_png": str(change_png) if change_png.exists() else "",
        "json": str(json_path),
        "csv": str(csv_path),
    }


def run_historical_analysis(
    name: str,
    bbox: list[float],
    start_year: int,
    end_year: int,
    month_start: int,
    month_end: int,
    max_cloud: int = 30,
) -> dict[str, Any]:
    observations, rasters = collect_yearly_series(
        bbox=bbox,
        start_year=start_year,
        end_year=end_year,
        month_start=month_start,
        month_end=month_end,
        max_cloud=max_cloud,
    )
    detection = detect_temporal_changes(observations)
    outputs = create_outputs(name, observations, rasters, detection)
    return {
        "name": name,
        "bbox": bbox,
        "start_year": start_year,
        "end_year": end_year,
        "month_start": month_start,
        "month_end": month_end,
        "max_cloud": max_cloud,
        "observations": [asdict(o) for o in observations],
        "detection": detection,
        "outputs": outputs,
        "source": "Microsoft Planetary Computer / Sentinel-2 L2A",
        "method_note": "Una escena de menor nubosidad por año y periodo estacional. Los resultados son analíticos y requieren revisión humana.",
    }


def result_excel_bytes(result: dict[str, Any]) -> bytes:
    import pandas as pd

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(result["observations"]).to_excel(writer, sheet_name="Serie anual", index=False)
        pd.DataFrame(result["detection"].get("scores", [])).to_excel(writer, sheet_name="Cambios anuales", index=False)
        summary = {k: v for k, v in result["detection"].items() if k != "scores"}
        pd.DataFrame([summary]).to_excel(writer, sheet_name="Conclusión", index=False)
    return buffer.getvalue()
