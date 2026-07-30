from __future__ import annotations
import io
import json
import pandas as pd
import simplekml

def to_csv_bytes(rows: list[dict]) -> bytes:
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")

def to_geojson_bytes(features: list[dict]) -> bytes:
    return json.dumps({"type":"FeatureCollection","features":features},
                      ensure_ascii=False, indent=2).encode("utf-8")

def to_kmz_bytes(features: list[dict], name: str = "Sentinel Tierra Sinaloa") -> bytes:
    kml = simplekml.Kml(name=name)
    folder = kml.newfolder(name=name)
    for ft in features:
        geom = ft.get("geometry") or {}
        props = ft.get("properties") or {}
        title = str(props.get("nombre") or props.get("folio") or "Elemento")
        desc = "<br>".join(f"<b>{k}</b>: {v}" for k, v in props.items() if v not in (None, ""))
        typ = geom.get("type")
        coords = geom.get("coordinates")
        if typ == "Point":
            p = folder.newpoint(name=title, coords=[tuple(coords)])
            p.description = desc
        elif typ == "LineString":
            ls = folder.newlinestring(name=title, coords=[tuple(c) for c in coords])
            ls.description = desc
            ls.style.linestyle.width = 4
        elif typ == "Polygon":
            pol = folder.newpolygon(name=title, outerboundaryis=[tuple(c) for c in coords[0]])
            pol.description = desc
            pol.style.polystyle.fill = 0
            pol.style.linestyle.width = 3
    bio = io.BytesIO()
    kml.savekmz(bio)
    return bio.getvalue()
