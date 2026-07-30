import json,io,re
from pathlib import Path
from hashlib import sha256
from datetime import datetime
import pandas as pd
import simplekml
from shapely.geometry import shape
from shapely.ops import transform
from pyproj import Transformer

def metrics(g):
 s=shape(g);lon,lat=s.centroid.x,s.centroid.y;zone=max(1,min(60,int((lon+180)/6)+1));epsg=32600+zone if lat>=0 else 32700+zone;p=transform(Transformer.from_crs('EPSG:4326',f'EPSG:{epsg}',always_xy=True).transform,s);return float(p.length),float(p.area)
def save_evidence(upload,folio):
 data=upload.getvalue();h=sha256(data).hexdigest();folder=Path('data/evidencias')/re.sub(r'[^A-Za-z0-9_-]','_',folio);folder.mkdir(parents=True,exist_ok=True);name=re.sub(r'[^A-Za-z0-9_.-]','_',upload.name);path=folder/f'{datetime.now():%Y%m%d_%H%M%S}_{h[:10]}_{name}';path.write_bytes(data);return str(path),h,len(data)
def excel_bytes(sheets):
 b=io.BytesIO()
 with pd.ExcelWriter(b,engine='openpyxl') as x:
  for n,r in sheets.items():pd.DataFrame(r).to_excel(x,sheet_name=n[:31],index=False)
 return b.getvalue()
def geojson_bytes(fs):return json.dumps({'type':'FeatureCollection','features':fs},ensure_ascii=False,indent=2).encode()
def kmz_bytes(fs,name):
 k=simplekml.Kml(name=name);f=k.newfolder(name=name)
 for ft in fs:
  g,p=ft['geometry'],ft.get('properties',{});title=p.get('nombre','Elemento');t,c=g['type'],g['coordinates']
  if t=='Point':o=f.newpoint(name=title,coords=[tuple(c)])
  elif t=='LineString':o=f.newlinestring(name=title,coords=[tuple(x) for x in c])
  elif t=='Polygon':o=f.newpolygon(name=title,outerboundaryis=[tuple(x) for x in c[0]])
  else:continue
  o.description='<br>'.join(f'<b>{a}</b>: {b}</br>' for a,b in p.items() if b not in (None,''))
 b=io.BytesIO();k.savekmz(b);return b.getvalue()
def score(change_pct,change,persist,access,multi,field):
 f={'cambio_superficial':min(25,max(0,change_pct/4)),'magnitud_ndvi':min(20,abs(change)*100),'persistencia':15 if persist else 0,'nuevo_acceso':10 if access else 0,'coincidencia_multifuente':20 if multi else 0,'reporte_campo':10 if field else 0};s=round(min(100,sum(f.values())),1);return s,'Alta' if s>=70 else 'Media' if s>=40 else 'Baja',f
