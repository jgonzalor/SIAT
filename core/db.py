import sqlite3
from contextlib import contextmanager
from pathlib import Path
DB=Path('data/sentinel_iat.db')
SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS organizaciones(id INTEGER PRIMARY KEY,nombre TEXT UNIQUE NOT NULL,tipo TEXT,contacto TEXT,telefono TEXT,correo TEXT,observaciones TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS brigadas(id INTEGER PRIMARY KEY,nombre TEXT UNIQUE NOT NULL,organizacion_id INTEGER,responsable TEXT,telefono TEXT,integrantes INTEGER DEFAULT 0,especialidad TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(organizacion_id) REFERENCES organizaciones(id));
CREATE TABLE IF NOT EXISTS operativos(id INTEGER PRIMARY KEY,folio TEXT UNIQUE NOT NULL,nombre TEXT NOT NULL,tipo TEXT,organizacion_id INTEGER,brigada_id INTEGER,municipio TEXT,localidad TEXT,fecha_inicio TEXT,fecha_fin TEXT,responsable TEXT,participantes INTEGER DEFAULT 0,estatus TEXT,nivel_confidencialidad TEXT,objetivo TEXT,observaciones TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(organizacion_id) REFERENCES organizaciones(id),FOREIGN KEY(brigada_id) REFERENCES brigadas(id));
CREATE TABLE IF NOT EXISTS geometries(id INTEGER PRIMARY KEY,operativo_id INTEGER,tipo TEXT,nombre TEXT,geojson TEXT,fuente TEXT,fecha TEXT,longitud_m REAL DEFAULT 0,area_m2 REAL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(operativo_id) REFERENCES operativos(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS puntos(id INTEGER PRIMARY KEY,operativo_id INTEGER,categoria TEXT,nombre TEXT,lat REAL,lon REAL,fecha_hora TEXT,descripcion TEXT,prioridad TEXT,estado_validacion TEXT DEFAULT 'Registrado',responsable_validacion TEXT,fecha_validacion TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(operativo_id) REFERENCES operativos(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS evidencias(id INTEGER PRIMARY KEY,operativo_id INTEGER,punto_id INTEGER,tipo TEXT,nombre_original TEXT,ruta TEXT,sha256 TEXT,tamanio INTEGER,descripcion TEXT,autor TEXT,fecha_captura TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(operativo_id) REFERENCES operativos(id) ON DELETE CASCADE,FOREIGN KEY(punto_id) REFERENCES puntos(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS capas(id INTEGER PRIMARY KEY,nombre TEXT UNIQUE,categoria TEXT,fuente TEXT,descripcion TEXT,geojson TEXT,visible INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS analisis(id INTEGER PRIMARY KEY,nombre TEXT,operativo_id INTEGER,bbox TEXT,fecha_a TEXT,fecha_b TEXT,escena_a TEXT,escena_b TEXT,nube_a REAL,nube_b REAL,media_ndvi_a REAL,media_ndvi_b REAL,cambio_medio REAL,porcentaje_cambio REAL,iat_score REAL,iat_nivel TEXT,factores_json TEXT,raster_png TEXT,metadata_json TEXT,estado TEXT DEFAULT 'Pendiente de revisión',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(operativo_id) REFERENCES operativos(id));
CREATE TABLE IF NOT EXISTS analisis_historicos(id INTEGER PRIMARY KEY,nombre TEXT,operativo_id INTEGER,bbox TEXT,anio_inicio INTEGER,anio_fin INTEGER,mes_inicio INTEGER,mes_fin INTEGER,nubosidad_max REAL,clasificacion TEXT,ultimo_estable INTEGER,primer_cambio INTEGER,ventana_probable TEXT,anio_pico INTEGER,puntuacion_pico REAL,observaciones_json TEXT,resultados_json TEXT,serie_png TEXT,cambio_png TEXT,estado TEXT DEFAULT 'Pendiente de revisión',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(operativo_id) REFERENCES operativos(id));
CREATE TABLE IF NOT EXISTS bitacora(id INTEGER PRIMARY KEY,operativo_id INTEGER,usuario TEXT,accion TEXT,entidad TEXT,entidad_id INTEGER,detalle TEXT,fecha_hora TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(operativo_id) REFERENCES operativos(id) ON DELETE CASCADE);
'''
def init_db():
 DB.parent.mkdir(parents=True,exist_ok=True)
 with sqlite3.connect(DB) as c:c.executescript(SCHEMA)
@contextmanager
def connect():
 init_db();c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
 try:yield c;c.commit()
 except:c.rollback();raise
 finally:c.close()
def query(sql,p=()):
 with connect() as c:return [dict(r) for r in c.execute(sql,p).fetchall()]
def execute(sql,p=()):
 with connect() as c:
  x=c.execute(sql,p);return int(x.lastrowid)
def scalar(sql,p=()):
 r=query(sql,p);return next(iter(r[0].values())) if r else 0
def audit(action,entity='',entity_id=None,detail='',operativo_id=None,user='Sistema'):
 execute('INSERT INTO bitacora(operativo_id,usuario,accion,entidad,entidad_id,detalle) VALUES(?,?,?,?,?,?)',(operativo_id,user,action,entity,entity_id,detail))
