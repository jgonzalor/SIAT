from __future__ import annotations
import json,zipfile
from pathlib import Path
from datetime import date,datetime,timedelta
import streamlit as st
import pandas as pd
import folium
from folium.plugins import Draw,Fullscreen,MeasureControl,MousePosition
from streamlit_folium import st_folium
from core.db import init_db,query,execute,scalar,audit,DB
from core.geo import parse_geojson,parse_gpx,parse_csv_points
from core.satellite import compare_periods
from core.multitemporal import run_historical_analysis,result_excel_bytes
from core.utils import metrics,save_evidence,excel_bytes,geojson_bytes,kmz_bytes,score

st.set_page_config(page_title='Sentinel IAT',page_icon='🧭',layout='wide')
init_db()
def map_base(zoom=7):
 m=folium.Map(location=[24.8,-107.4],zoom_start=zoom,tiles=None,control_scale=True);folium.TileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',attr='© OpenStreetMap',name='Mapa').add_to(m);folium.TileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',attr='Esri',name='Satélite').add_to(m);Fullscreen().add_to(m);MeasureControl().add_to(m);MousePosition().add_to(m);return m
def features(op=None):
 w='' if op is None else ' WHERE operativo_id=?';p=() if op is None else (op,);out=[]
 for r in query('SELECT * FROM geometries'+w,p):
  try:out.append({'type':'Feature','geometry':json.loads(r['geojson']),'properties':{'nombre':r['nombre'],'tipo':r['tipo'],'fecha':r['fecha'],'fuente':r['fuente']}})
  except:pass
 for r in query('SELECT * FROM puntos'+w,p):out.append({'type':'Feature','geometry':{'type':'Point','coordinates':[r['lon'],r['lat']]},'properties':{'nombre':r['nombre'],'categoria':r['categoria'],'prioridad':r['prioridad'],'estado':r['estado_validacion'],'descripcion':r['descripcion']}})
 return out
def add(m,fs):
 col={'Hallazgo':'red','Indicio':'orange','Punto revisado':'green','Acceso':'blue','Riesgo':'darkred','Referencia':'cadetblue'}
 for ft in fs:
  g,p=ft['geometry'],ft['properties'];pop=folium.Popup('<br>'.join(f'<b>{k}</b>: {v}' for k,v in p.items() if v not in (None,'')),max_width=420)
  if g['type']=='Point':lon,lat=g['coordinates'][:2];folium.Marker([lat,lon],popup=pop,tooltip=p.get('nombre'),icon=folium.Icon(color=col.get(p.get('categoria'),'gray'),icon='info-sign')).add_to(m)
  else:folium.GeoJson(ft,popup=pop,style_function=lambda x:{'weight':4,'fillOpacity':.15}).add_to(m)
def op_map():
 rows=query('SELECT id,folio,nombre FROM operativos ORDER BY fecha_inicio DESC');return rows,{f"{r['folio']} — {r['nombre']}":r for r in rows}

st.title('🧭 Sentinel IAT')
st.caption('Sistema de Investigación y Análisis Territorial')
st.warning('El sistema detecta y prioriza anomalías territoriales. No confirma fosas, delitos ni hechos periciales.')
menu=st.sidebar.radio('Módulos',['Dashboard','Organizaciones y brigadas','Operativos','Captura territorial','Hallazgos y evidencias','Análisis territorial','Análisis histórico','Capas GIS','Bitácora y exportación'])
if menu=='Dashboard':
 a,b,c,d,e=st.columns(5)
 a.metric('Operativos',scalar('SELECT COUNT(*) FROM operativos'))
 b.metric('En curso',scalar("SELECT COUNT(*) FROM operativos WHERE estatus='En curso'"))
 c.metric('Área revisada',f"{scalar('SELECT COALESCE(SUM(area_m2),0)/1000000 FROM geometries'):.2f} km²")
 recorridos=scalar("SELECT COALESCE(SUM(longitud_m),0)/1000 FROM geometries WHERE tipo='Recorrido'")
 d.metric('Recorridos',f"{recorridos:.1f} km")
 e.metric('Hallazgos',scalar("SELECT COUNT(*) FROM puntos WHERE categoria='Hallazgo'"))
 ops,om=op_map();sel=st.selectbox('Filtro',['Todos']+list(om));op=None if sel=='Todos' else om[sel]['id'];m=map_base();add(m,features(op));folium.LayerControl(collapsed=False).add_to(m);st_folium(m,height=620,width='stretch',key='dashboard')
 if ops:st.dataframe(pd.DataFrame(ops),width='stretch',hide_index=True)
elif menu=='Organizaciones y brigadas':
 t1,t2=st.tabs(['Organizaciones','Brigadas'])
 with t1:
  with st.form('org',clear_on_submit=True):
   n=st.text_input('Nombre *');typ=st.selectbox('Tipo',['Colectivo','Institución pública','Institución académica','Organización civil','Otro']);c=st.text_input('Contacto');tel=st.text_input('Teléfono');mail=st.text_input('Correo');obs=st.text_area('Observaciones')
   if st.form_submit_button('Guardar',type='primary') and n.strip():i=execute('INSERT INTO organizaciones(nombre,tipo,contacto,telefono,correo,observaciones) VALUES(?,?,?,?,?,?)',(n.strip(),typ,c,tel,mail,obs));audit('CREAR','organizacion',i,n);st.rerun()
  st.dataframe(pd.DataFrame(query('SELECT * FROM organizaciones ORDER BY nombre')),width='stretch',hide_index=True)
 with t2:
  org={r['nombre']:r['id'] for r in query('SELECT id,nombre FROM organizaciones')}
  with st.form('brig',clear_on_submit=True):
   n=st.text_input('Nombre *');o=st.selectbox('Organización',['Sin asignar']+list(org));r=st.text_input('Responsable');tel=st.text_input('Teléfono');num=st.number_input('Integrantes',0,500,0);esp=st.text_input('Especialidad')
   if st.form_submit_button('Guardar',type='primary') and n.strip():i=execute('INSERT INTO brigadas(nombre,organizacion_id,responsable,telefono,integrantes,especialidad) VALUES(?,?,?,?,?,?)',(n.strip(),org.get(o),r,tel,num,esp));audit('CREAR','brigada',i,n);st.rerun()
  st.dataframe(pd.DataFrame(query('SELECT b.*,o.nombre organizacion FROM brigadas b LEFT JOIN organizaciones o ON o.id=b.organizacion_id')),width='stretch',hide_index=True)
elif menu=='Operativos':
 org={r['nombre']:r['id'] for r in query('SELECT id,nombre FROM organizaciones')};br={r['nombre']:r['id'] for r in query('SELECT id,nombre FROM brigadas')}
 with st.form('op',clear_on_submit=True):
  a,b,c=st.columns(3);fol=a.text_input('Folio *');nom=b.text_input('Nombre *');typ=c.selectbox('Tipo',['Búsqueda territorial','Reconocimiento','Verificación','Análisis ambiental','Protección civil','Otro']);a,b=st.columns(2);oo=a.selectbox('Organización',['Sin asignar']+list(org));bb=b.selectbox('Brigada',['Sin asignar']+list(br));a,b,c=st.columns(3);mun=a.text_input('Municipio');loc=b.text_input('Localidad/Zona');res=c.text_input('Responsable');a,b,c,d=st.columns(4);fi=a.date_input('Inicio',date.today());ff=b.date_input('Término',None);part=c.number_input('Participantes',0,1000,0);est=d.selectbox('Estatus',['Planeado','En curso','Concluido','Suspendido']);conf=st.selectbox('Confidencialidad',['Público','Uso interno','Restringido','Confidencial']);obj=st.text_area('Objetivo');obs=st.text_area('Observaciones')
  if st.form_submit_button('Crear',type='primary'):
   if fol.strip() and nom.strip():
    try:i=execute('INSERT INTO operativos(folio,nombre,tipo,organizacion_id,brigada_id,municipio,localidad,fecha_inicio,fecha_fin,responsable,participantes,estatus,nivel_confidencialidad,objetivo,observaciones) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(fol.strip(),nom.strip(),typ,org.get(oo),br.get(bb),mun,loc,fi.isoformat(),ff.isoformat() if ff else None,res,part,est,conf,obj,obs));audit('CREAR','operativo',i,fol,i);st.rerun()
    except Exception as e:st.error(str(e))
  st.dataframe(pd.DataFrame(query('SELECT * FROM operativos ORDER BY fecha_inicio DESC')),width='stretch',hide_index=True)
elif menu=='Captura territorial':
 ops,om=op_map()
 if not om:st.info('Cree un operativo');st.stop()
 lab=st.selectbox('Operativo',list(om));op=om[lab]['id'];t1,t2,t3=st.tabs(['Dibujar','Registrar punto','Importar'])
 with t1:
  m=map_base(9);add(m,features(op));Draw(export=False,draw_options={'polyline':True,'polygon':True,'rectangle':True,'circle':False,'circlemarker':False,'marker':False},edit_options={'edit':False}).add_to(m);r=st_folium(m,height=560,width='stretch',key=f'draw{op}');d=r.get('last_active_drawing') if r else None;n=st.text_input('Nombre');f=st.date_input('Fecha',date.today())
  if st.button('Guardar geometría',type='primary'):
   if d and d.get('geometry'):
    g=d['geometry'];le,ar=metrics(g);typ='Recorrido' if g['type']=='LineString' else 'Área revisada';i=execute('INSERT INTO geometries(operativo_id,tipo,nombre,geojson,fuente,fecha,longitud_m,area_m2) VALUES(?,?,?,?,?,?,?,?)',(op,typ,n or typ,json.dumps(g),'Dibujo en mapa',f.isoformat(),le,ar));audit('CREAR','geometria',i,n or typ,op);st.success(f'{le/1000:.2f} km | {ar/1e6:.3f} km²');st.rerun()
 with t2:
  with st.form('point',clear_on_submit=True):
   a,b,c=st.columns(3);cat=a.selectbox('Categoría',['Punto revisado','Indicio','Hallazgo','Acceso','Riesgo','Referencia']);n=b.text_input('Nombre *');pri=c.selectbox('Prioridad',['Por revisar','Baja','Media','Alta','Crítica']);a,b=st.columns(2);lat=a.number_input('Latitud',value=24.8,format='%.7f');lon=b.number_input('Longitud',value=-107.4,format='%.7f');fh=st.text_input('Fecha/hora',datetime.now().strftime('%Y-%m-%d %H:%M'));desc=st.text_area('Descripción')
   if st.form_submit_button('Registrar',type='primary') and n.strip():i=execute('INSERT INTO puntos(operativo_id,categoria,nombre,lat,lon,fecha_hora,descripcion,prioridad) VALUES(?,?,?,?,?,?,?,?)',(op,cat,n.strip(),lat,lon,fh,desc,pri));audit('CREAR','punto',i,f'{cat}: {n}',op);st.rerun()
 with t3:
  u=st.file_uploader('GPX, GeoJSON o CSV',type=['gpx','geojson','json','csv'])
  if u and st.button('Importar'):
   ext=Path(u.name).suffix.lower();fs=parse_gpx(u.getvalue()) if ext=='.gpx' else parse_geojson(u.getvalue()) if ext in ('.json','.geojson') else parse_csv_points(u.getvalue());cnt=0
   for ft in fs:
    g=ft['geometry'];p=ft.get('properties',{})
    if g['type']=='Point':lon,lat=g['coordinates'][:2];execute('INSERT INTO puntos(operativo_id,categoria,nombre,lat,lon,descripcion,prioridad) VALUES(?,?,?,?,?,?,?)',(op,'Punto revisado',p.get('nombre','Punto importado'),lat,lon,json.dumps(p,ensure_ascii=False),'Por revisar'))
    else:le,ar=metrics(g);execute('INSERT INTO geometries(operativo_id,tipo,nombre,geojson,fuente,longitud_m,area_m2) VALUES(?,?,?,?,?,?,?)',(op,'Recorrido' if g['type']=='LineString' else 'Área revisada',p.get('nombre',u.name),json.dumps(g),f'Importado {u.name}',le,ar))
    cnt+=1
   audit('IMPORTAR','archivo',None,f'{u.name}: {cnt}',op);st.success(f'{cnt} elementos');st.rerun()
elif menu=='Hallazgos y evidencias':
 ops,om=op_map()
 if not om:st.info('No hay operativos');st.stop()
 lab=st.selectbox('Operativo',list(om));o=om[lab];op=o['id'];pts=query("SELECT * FROM puntos WHERE operativo_id=? AND categoria IN ('Hallazgo','Indicio') ORDER BY id DESC",(op,));pm={f"#{r['id']} {r['categoria']} — {r['nombre']}":r['id'] for r in pts};t1,t2=st.tabs(['Validación','Evidencia'])
 with t1:
  if pm:
   s=st.selectbox('Registro',list(pm));pid=pm[s];state=st.selectbox('Estado',['Registrado','En revisión','Validado','Confirmado por autoridad','Descartado']);resp=st.text_input('Responsable');obs=st.text_area('Observación')
   if st.button('Actualizar',type='primary'):execute('UPDATE puntos SET estado_validacion=?,responsable_validacion=?,fecha_validacion=? WHERE id=?',(state,resp,datetime.now().isoformat(timespec='minutes'),pid));audit('VALIDAR','punto',pid,f'{state}: {obs}',op,resp or 'Usuario');st.rerun()
  st.dataframe(pd.DataFrame(pts),width='stretch',hide_index=True)
 with t2:
  point=st.selectbox('Vincular a',['Sin punto']+list(pm));typ=st.selectbox('Tipo',['Fotografía','Video','Documento','Audio','Otro']);author=st.text_input('Autor/captor');fc=st.text_input('Fecha de captura');desc=st.text_area('Descripción');u=st.file_uploader('Archivo')
  if u and st.button('Preservar evidencia',type='primary'):
   path,h,size=save_evidence(u,o['folio']);eid=execute('INSERT INTO evidencias(operativo_id,punto_id,tipo,nombre_original,ruta,sha256,tamanio,descripcion,autor,fecha_captura) VALUES(?,?,?,?,?,?,?,?,?,?)',(op,pm.get(point),typ,u.name,path,h,size,desc,author,fc));audit('ADJUNTAR','evidencia',eid,f'{u.name} SHA256={h}',op,author or 'Usuario');st.success(f'SHA-256: {h}')
  st.dataframe(pd.DataFrame(query('SELECT * FROM evidencias WHERE operativo_id=? ORDER BY id DESC',(op,))),width='stretch',hide_index=True)
elif menu=='Análisis territorial':
 st.subheader('Comparador visual de cambios territoriales')
 st.caption('Marque un área, seleccione dos periodos y el sistema mostrará el antes, el después y las zonas con cambios fuertes.')
 st.markdown('**Flujo:** ① Marcar área → ② Elegir periodos → ③ Comparar → ④ Revisar anomalías → ⑤ Exportar evidencia')

 ops,om=op_map()
 with st.expander('1. Vinculación del estudio',expanded=False):
  oplab=st.selectbox('Vincular a operativo',['Sin vincular']+list(om),key='cmp_op')
  op=None if oplab=='Sin vincular' else om[oplab]['id']
  name=st.text_input('Nombre del análisis','Comparación de cambio superficial',key='cmp_name')

 st.markdown('### 1. Marque el área que desea comparar')
 st.info('Use la herramienta de rectángulo ubicada en el mapa. Para obtener un resultado rápido, seleccione un área pequeña, preferentemente menor a 25–35 km².')
 m=map_base(9)
 Draw(export=False,draw_options={'rectangle':True,'polyline':False,'polygon':False,'circle':False,'circlemarker':False,'marker':False},edit_options={'edit':False}).add_to(m)
 rr=st_folium(m,height=500,width='stretch',key='sat_compare_map')
 dr=rr.get('last_active_drawing') if rr else None
 if dr and dr.get('geometry'):
  cs=dr['geometry']['coordinates'][0];xs=[x[0] for x in cs];ys=[x[1] for x in cs]
  bbox_preview=[min(xs),min(ys),max(xs),max(ys)]
  st.success(f'Área marcada correctamente. Límites: {bbox_preview[1]:.5f}, {bbox_preview[0]:.5f} → {bbox_preview[3]:.5f}, {bbox_preview[2]:.5f}')
 else:
  st.warning('Todavía no hay un área marcada.')

 st.markdown('### 2. Seleccione los periodos')
 c1,c2=st.columns(2)
 with c1:
  st.markdown('**Periodo anterior (ANTES)**')
  sa=st.date_input('Inicio anterior',date.today()-timedelta(days=365*3+60),key='cmp_sa')
  ea=st.date_input('Fin anterior',date.today()-timedelta(days=365*3),key='cmp_ea')
 with c2:
  st.markdown('**Periodo reciente (DESPUÉS)**')
  sb=st.date_input('Inicio reciente',date.today()-timedelta(days=60),key='cmp_sb')
  eb=st.date_input('Fin reciente',date.today(),key='cmp_eb')
 cloud=st.slider('Nubosidad máxima permitida',5,80,30,key='cmp_cloud',help='Un valor bajo mejora la calidad, pero puede dejar periodos sin imágenes disponibles.')

 with st.expander('Factores de contexto para la prioridad operativa'):
  a1,a2,a3,a4=st.columns(4)
  persist=a1.checkbox('Cambio persistente',key='cmp_persist')
  access=a2.checkbox('Acceso nuevo',key='cmp_access')
  multi=a3.checkbox('Confirmación externa',key='cmp_multi')
  field=a4.checkbox('Reporte de campo',key='cmp_field')

 st.markdown('### 3. Ejecute la comparación')
 if st.button('🔎 Analizar cambios en el área marcada',type='primary',width='stretch'):
  try:
   if not dr or not dr.get('geometry'):raise ValueError('Primero dibuje un rectángulo sobre el mapa.')
   cs=dr['geometry']['coordinates'][0];xs=[x[0] for x in cs];ys=[x[1] for x in cs];bbox=[min(xs),min(ys),max(xs),max(ys)]
   with st.spinner('Consultando imágenes Sentinel-2 y calculando anomalías...'):
    result=compare_periods(name,bbox,sa,ea,sb,eb,cloud)
   sc,lv,fac=score(result['porcentaje_cambio'],result['cambio_medio'],persist,access,multi,field)
   result['iat_score']=sc;result['iat_nivel']=lv;result['factores']=fac
   i=execute('INSERT INTO analisis(nombre,operativo_id,bbox,fecha_a,fecha_b,escena_a,escena_b,nube_a,nube_b,media_ndvi_a,media_ndvi_b,cambio_medio,porcentaje_cambio,iat_score,iat_nivel,factores_json,raster_png,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(name,op,json.dumps(bbox),result['fecha_a'],result['fecha_b'],result['escena_a'],result['escena_b'],result['nube_a'],result['nube_b'],result['media_ndvi_a'],result['media_ndvi_b'],result['cambio_medio'],result['porcentaje_cambio'],sc,lv,json.dumps(fac),result['raster_png'],result['metadata_json']))
   audit('ANALIZAR','analisis',i,f'IAT {sc} {lv}',op)
   st.session_state['compare_result']=result
  except Exception as e:st.error(str(e))

 result=st.session_state.get('compare_result')
 if result:
  st.divider()
  st.markdown('## 4. Resultado de la comparación')
  r1,r2,r3,r4=st.columns(4)
  r1.metric('Área con cambio fuerte',f"{result['porcentaje_cambio']:.1f}%")
  r2.metric('Pérdida de vegetación',f"{result['porcentaje_disminucion']:.1f}%")
  r3.metric('Aumento de vegetación',f"{result['porcentaje_aumento']:.1f}%")
  r4.metric('Prioridad IAT',f"{result['iat_score']}/100 · {result['iat_nivel']}")
  st.info(result['interpretacion'])
  st.warning('Interpretación preventiva: las zonas resaltadas son áreas con modificación espectral y requieren revisión humana. No confirman fosas, delitos ni hallazgos.')

  st.markdown('### Comparación visual')
  left,right=st.columns(2)
  left.image(result['before_png'],caption=f"ANTES · {result.get('fecha_escena_a') or result['fecha_a']}",width='stretch')
  right.image(result['after_png'],caption=f"DESPUÉS · {result.get('fecha_escena_b') or result['fecha_b']}",width='stretch')
  st.image(result['raster_png'],caption='Vista integral: antes, después, diferencia y anomalías priorizadas',width='stretch')

  with st.expander('Explicación técnica de los colores'):
   st.markdown('''- **Rojo en anomalías:** píxeles donde el cambio absoluto de NDVI fue igual o mayor a 0.20.
- **Verde en ΔNDVI:** aumento de vegetación.
- **Rojo/amarillo en ΔNDVI:** disminución o transformación de cobertura.
- El resultado puede verse afectado por agricultura, lluvia, incendios, sombras o diferencias estacionales.''')

  st.markdown('### 5. Exportación rápida')
  summary={k:v for k,v in result.items() if not k.endswith('_png') and k!='metadata_json'}
  e1,e2,e3=st.columns(3)
  e1.download_button('Descargar resultado JSON',json.dumps(summary,ensure_ascii=False,indent=2).encode(),f"{name.replace(' ','_')}_comparacion.json",mime='application/json',width='stretch')
  e2.download_button('Descargar comparativo PNG',Path(result['raster_png']).read_bytes(),f"{name.replace(' ','_')}_comparacion.png",mime='image/png',width='stretch')
  e3.download_button('Descargar mapa de anomalías',Path(result['anomaly_png']).read_bytes(),f"{name.replace(' ','_')}_anomalias.png",mime='image/png',width='stretch')

 st.subheader('Comparaciones guardadas')
 st.dataframe(pd.DataFrame(query('SELECT id,nombre,fecha_a,fecha_b,porcentaje_cambio,iat_score,iat_nivel,estado,created_at FROM analisis ORDER BY id DESC')),width='stretch',hide_index=True)
elif menu=='Análisis histórico':
 st.subheader('Motor Multitemporal de Detección y Evolución de Cambios')
 st.caption('Compara el mismo periodo estacional entre varios años para reducir falsos positivos por agricultura, lluvias o estacionalidad.')
 ops,om=op_map();oplab=st.selectbox('Vincular a operativo',['Sin vincular']+list(om),key='hist_op');op=None if oplab=='Sin vincular' else om[oplab]['id']
 m=map_base(9);Draw(export=False,draw_options={'rectangle':True,'polyline':False,'polygon':False,'circle':False,'circlemarker':False,'marker':False},edit_options={'edit':False}).add_to(m);rr=st_folium(m,height=460,width='stretch',key='hist_map');dr=rr.get('last_active_drawing') if rr else None
 name=st.text_input('Nombre del estudio','Evolución histórica territorial',key='hist_name')
 current=date.today().year
 a,b,c,d=st.columns(4);yi=a.number_input('Año inicial',2016,current,current-6,1);yf=b.number_input('Año final',2016,current,current,1);mi=c.selectbox('Mes inicial',list(range(1,13)),index=5);mf=d.selectbox('Mes final',list(range(1,13)),index=8)
 cloud=st.slider('Nubosidad máxima por escena',5,80,30,key='hist_cloud')
 st.info('Para obtener comparaciones válidas, el sistema usa el mismo intervalo de meses en todos los años y consulta una escena Sentinel-2 de menor nubosidad por año.')
 if st.button('Ejecutar análisis histórico',type='primary'):
  try:
   if not dr:raise ValueError('Dibuje un rectángulo sobre el área que desea comparar.')
   cs=dr['geometry']['coordinates'][0];xs=[x[0] for x in cs];ys=[x[1] for x in cs];bbox=[min(xs),min(ys),max(xs),max(ys)]
   with st.spinner('Consultando y procesando la serie histórica Sentinel-2...'):
    res=run_historical_analysis(name,bbox,int(yi),int(yf),int(mi),int(mf),cloud)
   det=res['detection'];out=res['outputs']
   hid=execute('INSERT INTO analisis_historicos(nombre,operativo_id,bbox,anio_inicio,anio_fin,mes_inicio,mes_fin,nubosidad_max,clasificacion,ultimo_estable,primer_cambio,ventana_probable,anio_pico,puntuacion_pico,observaciones_json,resultados_json,serie_png,cambio_png) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(name,op,json.dumps(bbox),int(yi),int(yf),int(mi),int(mf),cloud,det.get('classification'),det.get('last_stable'),det.get('first_change'),det.get('probable_window'),det.get('peak_year'),det.get('peak_score'),json.dumps(res['observations'],ensure_ascii=False),json.dumps(det,ensure_ascii=False),out.get('series_png'),out.get('change_png')))
   audit('ANALIZAR_HISTORICO','analisis_historico',hid,f"{det.get('classification')} | pico {det.get('peak_score')}/100",op)
   st.session_state['hist_result']=res
  except Exception as e:st.error(str(e))
 res=st.session_state.get('hist_result')
 if res:
  det=res['detection'];a,b,c,d=st.columns(4);a.metric('Clasificación',det.get('classification','No concluyente'));b.metric('Último año estable',det.get('last_stable') or 'N/D');c.metric('Primer cambio',det.get('first_change') or 'N/D');d.metric('Pico de cambio',f"{det.get('peak_score',0):.1f}/100")
  st.success(det.get('explanation',''))
  if det.get('probable_window'):st.warning(f"Ventana temporal probable del cambio: {det['probable_window']}")
  col1,col2=st.columns(2)
  if res['outputs'].get('series_png'):col1.image(res['outputs']['series_png'],caption='Evolución anual de NDVI, NDMI, NBR y BSI',width='stretch')
  if res['outputs'].get('change_png'):col2.image(res['outputs']['change_png'],caption='Diferencias multiespectrales del intervalo priorizado',width='stretch')
  st.subheader('Serie anual')
  st.dataframe(pd.DataFrame(res['observations']),width='stretch',hide_index=True)
  st.subheader('Puntuación de cambio por año')
  st.dataframe(pd.DataFrame(det.get('scores',[])),width='stretch',hide_index=True)
  x1,x2,x3=st.columns(3);x1.download_button('Descargar Excel',result_excel_bytes(res),f"{name.replace(' ','_')}_historico.xlsx",mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');x2.download_button('Descargar JSON',json.dumps(res,ensure_ascii=False,indent=2).encode(),f"{name.replace(' ','_')}_historico.json",mime='application/json');x3.download_button('Descargar CSV',Path(res['outputs']['csv']).read_bytes(),f"{name.replace(' ','_')}_serie.csv",mime='text/csv')
 st.subheader('Análisis guardados')
 st.dataframe(pd.DataFrame(query('SELECT id,nombre,anio_inicio,anio_fin,clasificacion,ultimo_estable,primer_cambio,ventana_probable,anio_pico,puntuacion_pico,estado,created_at FROM analisis_historicos ORDER BY id DESC')),width='stretch',hide_index=True)
elif menu=='Capas GIS':
 n=st.text_input('Nombre de capa');cat=st.selectbox('Categoría',['Municipios','Localidades','Caminos','Hidrografía','Ambiental','Operativa','Otra']);src=st.text_input('Fuente');desc=st.text_area('Descripción');u=st.file_uploader('GeoJSON',type=['geojson','json'])
 if u and st.button('Guardar capa',type='primary') and n.strip():fs=parse_geojson(u.getvalue());i=execute('INSERT INTO capas(nombre,categoria,fuente,descripcion,geojson) VALUES(?,?,?,?,?)',(n.strip(),cat,src,desc,json.dumps({'type':'FeatureCollection','features':fs})));audit('CREAR','capa',i,n);st.rerun()
 rows=query('SELECT * FROM capas ORDER BY nombre');st.dataframe(pd.DataFrame([{k:v for k,v in r.items() if k!='geojson'} for r in rows]),width='stretch',hide_index=True);m=map_base()
 for r in rows:
  try:folium.GeoJson(json.loads(r['geojson']),name=r['nombre']).add_to(m)
  except:pass
 folium.LayerControl(collapsed=False).add_to(m);st_folium(m,height=600,width='stretch',key='layers')
else:
 ops,om=op_map()
 if not om:st.info('No hay operativos');st.stop()
 lab=st.selectbox('Operativo',list(om));o=om[lab];op=o['id'];pts=query('SELECT * FROM puntos WHERE operativo_id=?',(op,));geo=query('SELECT * FROM geometries WHERE operativo_id=?',(op,));ev=query('SELECT * FROM evidencias WHERE operativo_id=?',(op,));logs=query('SELECT * FROM bitacora WHERE operativo_id=? ORDER BY fecha_hora DESC',(op,));fs=features(op);a,b,c,d=st.columns(4);a.download_button('Excel integral',excel_bytes({'Puntos':pts,'Geometrias':geo,'Evidencias':ev,'Bitacora':logs}),f"{o['folio']}_integral.xlsx");b.download_button('GeoJSON',geojson_bytes(fs),f"{o['folio']}.geojson");c.download_button('KMZ',kmz_bytes(fs,o['folio']),f"{o['folio']}.kmz");d.download_button('Respaldo DB',DB.read_bytes() if DB.exists() else b'','sentinel_iat_backup.db');st.dataframe(pd.DataFrame(logs),width='stretch',hide_index=True)
