"""Esquema en estrella del almacén (SQLite) y tablas de control del ETL.

Constelación con dimensiones conformadas:

    dim_fecha · dim_fuente · dim_ubicacion · dim_tipo_inmueble · dim_estado_inmueble · dim_tipo_oferta
    dim_categoria · dim_lugar · dim_comercial · dim_cliente · dim_predio · dim_inmueble

    fact_oferta          un anuncio u oferta de mercado (precio pedido o de cierre, nunca mezclados)
    fact_oferta_predio   una oferta dentro del mercado de un predio (por radio o por zona)
    fact_entorno         un lugar del entorno de un predio, con su distancia
    fact_cotizacion      una cotización: comercial + cliente + inmueble + fecha
    fact_ficha           una ficha generada para una cotización (resultado del motor de ficha)
    fact_meta            la meta de un comercial en un mes

Reglas que el esquema hace cumplir:
- La clave 0 de cada dimensión es el miembro "sin dato": ningún hecho queda huérfano.
- Las coordenadas están en EPSG:4326 (CHECK de rango); las distancias, en metros.
- `tipo_precio` solo admite pedido o cierre, en su dimensión.
- Nada se borra en silencio: lo que no entra a un hecho queda en `etl_rechazo` con su motivo.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

VERSION_ESQUEMA = "1.3"

DDL = """
PRAGMA foreign_keys = ON;

-- =============================== DIMENSIONES ===============================
CREATE TABLE IF NOT EXISTS dim_fecha (
  fecha_key INTEGER PRIMARY KEY,             -- AAAAMMDD; 0 = sin fecha
  fecha TEXT, anio INTEGER, trimestre INTEGER, mes INTEGER, mes_nombre TEXT, anio_mes TEXT,
  dia INTEGER, semana_iso INTEGER, dia_semana INTEGER, dia_nombre TEXT, es_fin_de_semana INTEGER
);

CREATE TABLE IF NOT EXISTS dim_fuente (
  fuente_key INTEGER PRIMARY KEY,
  fuente_id TEXT NOT NULL UNIQUE,            -- oime, geomedellin, catastro, osm, fincaraiz, facebook...
  nombre TEXT, tipo TEXT,                    -- oficial | abierta | comercial | portal | informal | sin_dato
  origen_datos TEXT,                         -- captura (Momento 1) | reto (insumo controlado) | sistema
  licencia TEXT, activa INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dim_ubicacion (
  ubicacion_key INTEGER PRIMARY KEY,
  ubicacion_nk TEXT NOT NULL UNIQUE,         -- municipio|clave del barrio
  municipio TEXT, comuna_cod TEXT, comuna TEXT, barrio_cod TEXT, barrio TEXT, barrio_clave TEXT,
  zona_mercado TEXT,                         -- zona de la muestra de Venture One a la que pertenece el barrio
  revisado INTEGER DEFAULT 0                 -- 1 = barrio oficial (catastro) o de catálogo; 0 = grafía sin revisar
);

CREATE TABLE IF NOT EXISTS dim_tipo_inmueble (
  tipo_inmueble_key INTEGER PRIMARY KEY, tipo_inmueble TEXT NOT NULL UNIQUE, uso TEXT
);

CREATE TABLE IF NOT EXISTS dim_estado_inmueble (
  estado_key INTEGER PRIMARY KEY, estado TEXT NOT NULL UNIQUE, etiqueta TEXT, orden INTEGER
);

CREATE TABLE IF NOT EXISTS dim_tipo_oferta (
  tipo_oferta_key INTEGER PRIMARY KEY,
  operacion TEXT NOT NULL,                   -- venta | arriendo | sin_dato
  tipo_precio TEXT NOT NULL CHECK (tipo_precio IN ('pedido', 'cierre', 'sin_dato')),
  UNIQUE (operacion, tipo_precio)
);

CREATE TABLE IF NOT EXISTS dim_categoria (
  categoria_key INTEGER PRIMARY KEY, categoria TEXT NOT NULL UNIQUE, etiqueta TEXT,
  categoria_ficha TEXT                       -- categoría oficial de la ficha (parametros.yaml) o NULL si no cuenta
);

CREATE TABLE IF NOT EXISTS dim_lugar (
  lugar_key INTEGER PRIMARY KEY,
  lugar_nk TEXT NOT NULL UNIQUE,             -- fuente:capa:id_externo
  fuente_key INTEGER NOT NULL REFERENCES dim_fuente(fuente_key),
  categoria_key INTEGER NOT NULL REFERENCES dim_categoria(categoria_key),
  capa TEXT, subtipo TEXT, nombre TEXT,
  lat REAL CHECK (lat IS NULL OR lat BETWEEN -90 AND 90),
  lng REAL CHECK (lng IS NULL OR lng BETWEEN -180 AND 180),
  geometria TEXT, sistema_transporte TEXT, linea TEXT, orden_fuente INTEGER
);

CREATE TABLE IF NOT EXISTS dim_comercial (
  comercial_key INTEGER PRIMARY KEY, usuario TEXT NOT NULL UNIQUE, nombre TEXT,
  rol TEXT, activo INTEGER
);

CREATE TABLE IF NOT EXISTS dim_cliente (
  cliente_key INTEGER PRIMARY KEY,
  cliente_id INTEGER NOT NULL UNIQUE,        -- llave de la base operativa
  tipo_documento TEXT, documento_mascara TEXT, documento_sha256 TEXT,   -- el número completo no sale de la base operativa
  nombre TEXT, autoriza_datos INTEGER, creado_por TEXT, fecha_alta_key INTEGER
);

CREATE TABLE IF NOT EXISTS dim_predio (
  predio_key INTEGER PRIMARY KEY,
  predio_nk TEXT NOT NULL UNIQUE,            -- captura:<id> | reto:<id_inmueble>
  origen TEXT NOT NULL,                      -- captura | reto
  captura_id TEXT, direccion TEXT, cbml TEXT, conjunto TEXT,
  ubicacion_key INTEGER NOT NULL REFERENCES dim_ubicacion(ubicacion_key),
  municipio TEXT, barrio_texto TEXT,          -- tal como llegaron de la fuente (el barrio conformado está en dim_ubicacion)
  lat REAL CHECK (lat IS NULL OR lat BETWEEN -90 AND 90),
  lng REAL CHECK (lng IS NULL OR lng BETWEEN -180 AND 180),
  crs TEXT NOT NULL DEFAULT 'EPSG:4326' CHECK (crs = 'EPSG:4326'),
  x_9377 REAL, y_9377 REAL,
  precision_coordenadas TEXT, fuente_coordenadas TEXT, radio_m INTEGER, en_cobertura INTEGER,
  estado_captura TEXT, utilizable INTEGER NOT NULL DEFAULT 1,
  pot_tratamiento TEXT, pot_codigo TEXT, pot_categoria TEXT, pot_clase_suelo TEXT, pot_uso_general TEXT,
  estado_norma TEXT, zge_valor_m2 REAL, es_ph INTEGER, area_lote REAL, pisos_max REAL,
  estacion_masiva TEXT, estacion_masiva_linea TEXT, estacion_masiva_m REAL,
  control_9377_max_dif_m REAL, fecha_key INTEGER, huella_config TEXT, version_app TEXT
);

CREATE TABLE IF NOT EXISTS dim_inmueble (
  inmueble_key INTEGER PRIMARY KEY,
  inmueble_nk TEXT NOT NULL UNIQUE,          -- cot:<cotizacion_id> | reto:<id_inmueble>
  origen TEXT NOT NULL,                      -- cotizacion | reto
  predio_key INTEGER NOT NULL REFERENCES dim_predio(predio_key),
  id_inmueble TEXT, id_anuncio TEXT, origen_anuncio TEXT,
  tipo_inmueble_key INTEGER NOT NULL REFERENCES dim_tipo_inmueble(tipo_inmueble_key),
  estado_key INTEGER NOT NULL REFERENCES dim_estado_inmueble(estado_key),
  estado_texto TEXT, area_m2 REAL, habitaciones INTEGER, banos INTEGER, parqueaderos INTEGER,
  zona_mercado TEXT, datos_completos INTEGER NOT NULL DEFAULT 0
);

-- ================================= HECHOS =================================
CREATE TABLE IF NOT EXISTS fact_oferta (
  oferta_key INTEGER PRIMARY KEY,
  oferta_nk TEXT NOT NULL UNIQUE,            -- muestra_v1:<archivo:hoja:fila> | oime:<id>
  base_mercado TEXT NOT NULL,                -- muestra_v1 | oime | portales
  fuente_key INTEGER NOT NULL REFERENCES dim_fuente(fuente_key),
  fecha_key INTEGER NOT NULL REFERENCES dim_fecha(fecha_key),
  ubicacion_key INTEGER NOT NULL REFERENCES dim_ubicacion(ubicacion_key),
  tipo_inmueble_key INTEGER NOT NULL REFERENCES dim_tipo_inmueble(tipo_inmueble_key),
  estado_key INTEGER NOT NULL REFERENCES dim_estado_inmueble(estado_key),
  tipo_oferta_key INTEGER NOT NULL REFERENCES dim_tipo_oferta(tipo_oferta_key),
  origen TEXT NOT NULL, id_anuncio TEXT, orden_origen INTEGER,     -- trazabilidad hasta la fila de la fuente
  zona TEXT, estado_texto TEXT, tipo_precio_texto TEXT, url TEXT,
  area_m2 REAL, precio REAL, precio_m2 REAL,                        -- medidas
  habitaciones REAL, banos REAL, parqueaderos REAL, estrato INTEGER, antiguedad TEXT,
  lat REAL CHECK (lat IS NULL OR lat BETWEEN -90 AND 90),
  lng REAL CHECK (lng IS NULL OR lng BETWEEN -180 AND 180),
  completitud REAL,
  motivo_invalido TEXT,                      -- motivo oficial de invalidez (NULL = sirve como comparable)
  es_duplicado INTEGER NOT NULL DEFAULT 0, grupo_dup TEXT, id_representante TEXT,
  regla_dup TEXT, coincide_con TEXT, puntaje_dup REAL, grupo_ambiguo INTEGER DEFAULT 0, ids_grupo TEXT,
  motivo_exclusion TEXT,                     -- 'duplicado', el motivo de invalidez o NULL
  es_valida INTEGER NOT NULL DEFAULT 0,      -- 1 = única y sin motivo de exclusión
  corrida_id INTEGER,
  regla_dup_previa TEXT, puntaje_dup_previo REAL, coincide_con_previo TEXT   -- repetido entre sitios, marcado antes del ETL
);
CREATE INDEX IF NOT EXISTS ix_oferta_base ON fact_oferta(base_mercado, es_valida);
CREATE INDEX IF NOT EXISTS ix_oferta_zona ON fact_oferta(zona);

CREATE TABLE IF NOT EXISTS fact_oferta_predio (
  predio_key INTEGER NOT NULL REFERENCES dim_predio(predio_key),
  oferta_key INTEGER NOT NULL REFERENCES fact_oferta(oferta_key),
  vinculo TEXT NOT NULL,                     -- radio | zona
  distancia_m REAL,
  PRIMARY KEY (predio_key, oferta_key)
);

CREATE TABLE IF NOT EXISTS fact_entorno (
  predio_key INTEGER NOT NULL REFERENCES dim_predio(predio_key),
  lugar_key INTEGER NOT NULL REFERENCES dim_lugar(lugar_key),
  categoria_key INTEGER NOT NULL REFERENCES dim_categoria(categoria_key),
  fuente_key INTEGER NOT NULL REFERENCES dim_fuente(fuente_key),
  distancia_m REAL, anillo_m INTEGER,
  cuenta INTEGER NOT NULL DEFAULT 1,         -- 1 = lugar depurado que cuenta en su categoría; 0 = solo referencia de transporte
  en_mapa INTEGER NOT NULL DEFAULT 1,        -- 0 = capa masiva (establecimientos, cámaras) que se resume aparte
  es_estacion INTEGER NOT NULL DEFAULT 0,    -- 1 = estación de transporte masivo
  n_fuentes INTEGER DEFAULT 1,
  PRIMARY KEY (predio_key, lugar_key)
);
CREATE INDEX IF NOT EXISTS ix_entorno_cat ON fact_entorno(predio_key, categoria_key);

CREATE TABLE IF NOT EXISTS fact_cotizacion (
  cotizacion_key INTEGER PRIMARY KEY,
  cotizacion_id TEXT NOT NULL UNIQUE,        -- dimensión degenerada
  fecha_key INTEGER NOT NULL REFERENCES dim_fecha(fecha_key),
  comercial_key INTEGER NOT NULL REFERENCES dim_comercial(comercial_key),
  cliente_key INTEGER NOT NULL REFERENCES dim_cliente(cliente_key),
  inmueble_key INTEGER NOT NULL REFERENCES dim_inmueble(inmueble_key),
  predio_key INTEGER NOT NULL REFERENCES dim_predio(predio_key),
  ubicacion_key INTEGER NOT NULL REFERENCES dim_ubicacion(ubicacion_key),
  estado TEXT NOT NULL, hora INTEGER, creada_en TEXT,
  cuenta INTEGER NOT NULL DEFAULT 0,         -- 1 = estado que suma (reglas.yaml › cotizaciones.estados_que_cuentan)
  reusada INTEGER NOT NULL DEFAULT 0, duracion_s REAL,
  lugares INTEGER, ofertas_radio INTEGER, tiene_ficha INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cot_fecha ON fact_cotizacion(fecha_key, comercial_key);

-- Lo que busca el cliente: una fila por tipo de inmueble pedido en una cotización (hecho sin medida propia).
-- Responde qué pidió cada cliente, a qué comercial, y cuántas cotizaciones pidieron o no cada tipo.
CREATE TABLE IF NOT EXISTS fact_interes (
  interes_key INTEGER PRIMARY KEY,
  interes_nk TEXT NOT NULL UNIQUE,           -- <cotizacion_id>:<tipo_inmueble>
  cotizacion_id TEXT NOT NULL,               -- dimensión degenerada
  fecha_key INTEGER NOT NULL REFERENCES dim_fecha(fecha_key),
  comercial_key INTEGER NOT NULL REFERENCES dim_comercial(comercial_key),
  cliente_key INTEGER NOT NULL REFERENCES dim_cliente(cliente_key),
  tipo_inmueble_key INTEGER NOT NULL REFERENCES dim_tipo_inmueble(tipo_inmueble_key),
  predio_key INTEGER NOT NULL REFERENCES dim_predio(predio_key),
  ubicacion_key INTEGER NOT NULL REFERENCES dim_ubicacion(ubicacion_key),
  origen TEXT NOT NULL,                      -- declarado | tipo_cotizado
  orden INTEGER NOT NULL DEFAULT 0,
  es_cotizado INTEGER NOT NULL DEFAULT 0,    -- 1 = es el tipo del inmueble que se cotizó
  cuenta INTEGER NOT NULL DEFAULT 0,         -- 1 = la cotización terminó bien (misma regla de fact_cotizacion)
  n_tipos INTEGER NOT NULL,                  -- tipos que pidió el cliente en esa cotización
  peso REAL NOT NULL,                        -- 1 / n_tipos: sumar peso da cotizaciones; contar filas da solicitudes
  ofertas_radio INTEGER                      -- ofertas válidas de ese tipo a la vista del predio (oferta disponible)
);
CREATE INDEX IF NOT EXISTS ix_interes_tipo ON fact_interes(tipo_inmueble_key, fecha_key);
CREATE INDEX IF NOT EXISTS ix_interes_cot ON fact_interes(cotizacion_id);

CREATE TABLE IF NOT EXISTS fact_ficha (
  ficha_key INTEGER PRIMARY KEY,
  ficha_id TEXT NOT NULL UNIQUE,
  cotizacion_id TEXT NOT NULL, version INTEGER NOT NULL, vigente INTEGER NOT NULL DEFAULT 1,
  fecha_key INTEGER NOT NULL REFERENCES dim_fecha(fecha_key),
  comercial_key INTEGER NOT NULL REFERENCES dim_comercial(comercial_key),
  cliente_key INTEGER NOT NULL REFERENCES dim_cliente(cliente_key),
  inmueble_key INTEGER NOT NULL REFERENCES dim_inmueble(inmueble_key),
  predio_key INTEGER NOT NULL REFERENCES dim_predio(predio_key),
  estado_key INTEGER NOT NULL REFERENCES dim_estado_inmueble(estado_key),
  base_mercado TEXT, tipo_precio_base TEXT,
  n_comparables INTEGER, mediana_m2 REAL, promedio_m2 REAL, minimo_m2 REAL, maximo_m2 REAL,
  desviacion_m2 REAL, coef_variacion REAL, confianza TEXT,
  estaciones_en_radio INTEGER, equipamientos_en_radio INTEGER, dist_estacion_m REAL,
  norma_estado TEXT, n_datos_faltantes INTEGER, flexibilizada INTEGER DEFAULT 0,
  ruta_md TEXT,                              -- nombre del archivo (sin la carpeta, que es el documento del cliente)
  sha256_md TEXT, corrida_id INTEGER
);

CREATE TABLE IF NOT EXISTS fact_meta (
  comercial_key INTEGER NOT NULL REFERENCES dim_comercial(comercial_key),
  anio_mes TEXT NOT NULL,                    -- AAAA-MM
  fecha_key INTEGER NOT NULL REFERENCES dim_fecha(fecha_key),      -- primer día del mes
  meta_cotizaciones INTEGER, meta_clientes INTEGER, definida_por TEXT, actualizada_en TEXT,
  PRIMARY KEY (comercial_key, anio_mes)
);

-- ============================ CONTROL DEL ETL =============================
CREATE TABLE IF NOT EXISTS etl_corrida (
  corrida_id INTEGER PRIMARY KEY AUTOINCREMENT,
  inicio TEXT NOT NULL, fin TEXT, modo TEXT NOT NULL,          -- completo | captura | gestion | reto
  alcance TEXT, estado TEXT NOT NULL,                          -- en_curso | ok | con_alertas | con_fallas | error
  version_app TEXT, version_esquema TEXT, huella_config TEXT, huellas_json TEXT, resumen_json TEXT, mensaje TEXT
);

CREATE TABLE IF NOT EXISTS etl_paso (                          -- conciliación: leídos = cargados + duplicados + rechazados
  corrida_id INTEGER NOT NULL REFERENCES etl_corrida(corrida_id),
  entidad TEXT NOT NULL, alcance TEXT NOT NULL DEFAULT '',
  leidos INTEGER DEFAULT 0, cargados INTEGER DEFAULT 0, duplicados INTEGER DEFAULT 0, rechazados INTEGER DEFAULT 0,
  cuadra INTEGER DEFAULT 1, detalle TEXT,
  PRIMARY KEY (corrida_id, entidad, alcance)
);

CREATE TABLE IF NOT EXISTS etl_rechazo (                       -- todo lo que no entra a un hecho, con su motivo
  rechazo_id INTEGER PRIMARY KEY AUTOINCREMENT,
  corrida_id INTEGER NOT NULL REFERENCES etl_corrida(corrida_id),
  entidad TEXT NOT NULL, alcance TEXT, clave TEXT,             -- clave natural, o NULL si es un conteo agrupado
  motivo_exclusion TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 1, detalle TEXT
);
CREATE INDEX IF NOT EXISTS ix_rechazo ON etl_rechazo(corrida_id, entidad, motivo_exclusion);

CREATE TABLE IF NOT EXISTS etl_ajuste (                        -- cada valor que la transformación cambió
  ajuste_id INTEGER PRIMARY KEY AUTOINCREMENT,
  corrida_id INTEGER NOT NULL REFERENCES etl_corrida(corrida_id),
  entidad TEXT NOT NULL, clave TEXT, campo TEXT, valor_antes TEXT, valor_despues TEXT, regla TEXT
);

CREATE TABLE IF NOT EXISTS etl_calidad (                       -- controles de calidad de cada corrida
  corrida_id INTEGER NOT NULL REFERENCES etl_corrida(corrida_id),
  control TEXT NOT NULL, tabla TEXT, resultado TEXT NOT NULL,  -- ok | alerta | falla
  valor REAL, umbral REAL, detalle TEXT,
  PRIMARY KEY (corrida_id, control)
);

CREATE TABLE IF NOT EXISTS etl_insumo (                        -- qué archivos y tablas se usaron, con su huella
  insumo TEXT PRIMARY KEY, ruta TEXT, sha256 TEXT, registros INTEGER, metadatos_json TEXT,
  corrida_id INTEGER, cargado_en TEXT
);

CREATE TABLE IF NOT EXISTS etl_estado (clave TEXT PRIMARY KEY, valor TEXT);

-- ================================= VISTAS =================================
CREATE VIEW IF NOT EXISTS v_cotizacion AS
  SELECT f.cotizacion_id, f.creada_en, d.fecha, d.anio_mes, d.semana_iso, d.dia_nombre, f.hora, f.estado, f.cuenta,
         f.reusada, f.duracion_s, f.lugares, f.ofertas_radio, f.tiene_ficha,
         co.usuario AS comercial, co.nombre AS comercial_nombre,
         cl.cliente_id, cl.nombre AS cliente_nombre, cl.tipo_documento, cl.documento_mascara,
         pr.direccion, pr.cbml, pr.conjunto, u.barrio, u.comuna, u.municipio,
         ti.tipo_inmueble, es.estado AS estado_inmueble, i.area_m2,
         fi.confianza, fi.n_comparables, fi.mediana_m2, fi.base_mercado
  FROM fact_cotizacion f
  JOIN dim_fecha d ON d.fecha_key = f.fecha_key
  JOIN dim_comercial co ON co.comercial_key = f.comercial_key
  JOIN dim_cliente cl ON cl.cliente_key = f.cliente_key
  JOIN dim_inmueble i ON i.inmueble_key = f.inmueble_key
  JOIN dim_predio pr ON pr.predio_key = f.predio_key
  JOIN dim_ubicacion u ON u.ubicacion_key = f.ubicacion_key
  JOIN dim_tipo_inmueble ti ON ti.tipo_inmueble_key = i.tipo_inmueble_key
  JOIN dim_estado_inmueble es ON es.estado_key = i.estado_key
  LEFT JOIN fact_ficha fi ON fi.cotizacion_id = f.cotizacion_id AND fi.vigente = 1;

CREATE VIEW IF NOT EXISTS v_interes AS
  SELECT f.cotizacion_id, d.fecha, d.anio_mes, co.usuario AS comercial, co.nombre AS comercial_nombre,
         cl.cliente_id, cl.nombre AS cliente_nombre, cl.tipo_documento, cl.documento_mascara,
         ti.tipo_inmueble, ti.uso, f.origen, f.orden, f.es_cotizado, f.cuenta, f.n_tipos, f.peso, f.ofertas_radio,
         pr.direccion, u.barrio, u.comuna, u.municipio
  FROM fact_interes f
  JOIN dim_fecha d ON d.fecha_key = f.fecha_key
  JOIN dim_comercial co ON co.comercial_key = f.comercial_key
  JOIN dim_cliente cl ON cl.cliente_key = f.cliente_key
  JOIN dim_tipo_inmueble ti ON ti.tipo_inmueble_key = f.tipo_inmueble_key
  JOIN dim_predio pr ON pr.predio_key = f.predio_key
  JOIN dim_ubicacion u ON u.ubicacion_key = f.ubicacion_key;

CREATE VIEW IF NOT EXISTS v_oferta AS
  SELECT o.oferta_key, o.oferta_nk, o.base_mercado, fu.fuente_id AS fuente, d.fecha, o.zona,
         u.municipio, u.barrio, u.zona_mercado, ti.tipo_inmueble, es.estado AS estado_canonico, o.estado_texto,
         t.operacion, t.tipo_precio, o.tipo_precio_texto, o.origen, o.id_anuncio, o.orden_origen, o.url,
         o.area_m2, o.precio, o.precio_m2, o.habitaciones, o.banos, o.parqueaderos, o.estrato, o.lat, o.lng,
         o.completitud, o.motivo_invalido, o.es_duplicado, o.grupo_dup, o.id_representante, o.regla_dup,
         o.coincide_con, o.puntaje_dup, o.grupo_ambiguo, o.ids_grupo, o.motivo_exclusion, o.es_valida, o.observada_en
  FROM fact_oferta o
  JOIN dim_fuente fu ON fu.fuente_key = o.fuente_key
  JOIN dim_fecha d ON d.fecha_key = o.fecha_key
  JOIN dim_ubicacion u ON u.ubicacion_key = o.ubicacion_key
  JOIN dim_tipo_inmueble ti ON ti.tipo_inmueble_key = o.tipo_inmueble_key
  JOIN dim_estado_inmueble es ON es.estado_key = o.estado_key
  JOIN dim_tipo_oferta t ON t.tipo_oferta_key = o.tipo_oferta_key;

CREATE VIEW IF NOT EXISTS v_entorno AS
  SELECT e.predio_key, pr.predio_nk, l.lugar_nk, c.categoria, c.categoria_ficha, fu.fuente_id AS fuente,
         l.capa, l.subtipo, l.nombre, l.lat, l.lng, l.sistema_transporte, l.linea, l.orden_fuente,
         e.distancia_m, e.anillo_m, e.cuenta, e.en_mapa, e.es_estacion, e.n_fuentes
  FROM fact_entorno e
  JOIN dim_predio pr ON pr.predio_key = e.predio_key
  JOIN dim_lugar l ON l.lugar_key = e.lugar_key
  JOIN dim_categoria c ON c.categoria_key = e.categoria_key
  JOIN dim_fuente fu ON fu.fuente_key = e.fuente_key;

CREATE VIEW IF NOT EXISTS v_avance_meta AS
  SELECT co.usuario AS comercial, co.nombre AS comercial_nombre, m.anio_mes,
         m.meta_cotizaciones, m.meta_clientes,
         (SELECT COALESCE(SUM(f.cuenta), 0) FROM fact_cotizacion f JOIN dim_fecha d ON d.fecha_key = f.fecha_key
           WHERE f.comercial_key = m.comercial_key AND d.anio_mes = m.anio_mes) AS cotizaciones,
         (SELECT COUNT(DISTINCT f.cliente_key) FROM fact_cotizacion f JOIN dim_fecha d ON d.fecha_key = f.fecha_key
           WHERE f.comercial_key = m.comercial_key AND d.anio_mes = m.anio_mes AND f.cuenta = 1) AS clientes
  FROM fact_meta m JOIN dim_comercial co ON co.comercial_key = m.comercial_key;
"""

TABLAS_HECHOS = ("fact_oferta", "fact_oferta_predio", "fact_entorno", "fact_cotizacion", "fact_interes", "fact_ficha",
                 "fact_meta")
TABLAS_DIM = ("dim_fecha", "dim_fuente", "dim_ubicacion", "dim_tipo_inmueble", "dim_estado_inmueble",
              "dim_tipo_oferta", "dim_categoria", "dim_lugar", "dim_comercial", "dim_cliente", "dim_predio",
              "dim_inmueble")


def conectar(ruta: Path) -> sqlite3.Connection:
    Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(ruta), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


# Columnas que se agregaron después de la versión 1.0. El almacén se puede rehacer entero con el ETL, pero
# agregarlas así evita borrar el archivo al actualizar la aplicación.
COLUMNAS_NUEVAS = [
    ("fact_oferta", "observada_en", "TEXT"),             # cuándo se vio publicada (anuncios sin fecha de publicación)
    ("dim_inmueble", "caracteristicas_json", "TEXT"),    # características adicionales que anotó el comercial
    ("fact_oferta", "regla_dup_previa", "TEXT"),         # v1.3 · repetido entre sitios: lo marca la depuración de
    ("fact_oferta", "puntaje_dup_previo", "REAL"),       # anuncios (antes del ETL) y aquí se conserva tal cual; el
    ("fact_oferta", "coincide_con_previo", "TEXT"),      # ETL lo convierte en duplicado con su regla y su puntaje
]
VISTAS = ("v_cotizacion", "v_interes", "v_oferta", "v_entorno", "v_avance_meta")


def crear(con: sqlite3.Connection) -> None:
    """Crea el esquema si falta y siembra los miembros "sin dato" (clave 0) de cada dimensión."""
    for vista in VISTAS:                              # las vistas se rehacen: su definición puede haber cambiado
        con.execute(f"DROP VIEW IF EXISTS {vista}")
    con.executescript(DDL)
    for tabla, columna, tipo in COLUMNAS_NUEVAS:
        if columna not in {f[1] for f in con.execute(f"PRAGMA table_info({tabla})")}:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")
    con.execute("INSERT OR IGNORE INTO dim_fecha (fecha_key, fecha, anio_mes) VALUES (0, NULL, 'sin fecha')")
    con.execute("INSERT OR IGNORE INTO dim_fuente (fuente_key, fuente_id, nombre, tipo, origen_datos) "
                "VALUES (0, 'sin_dato', 'Sin fuente', 'sin_dato', 'sistema')")
    con.execute("INSERT OR IGNORE INTO dim_ubicacion (ubicacion_key, ubicacion_nk, municipio, barrio, barrio_clave) "
                "VALUES (0, 'sin_dato|', 'sin dato', 'Sin barrio', '')")
    con.execute("INSERT OR IGNORE INTO dim_tipo_inmueble VALUES (0, 'sin_especificar', 'sin_dato')")
    con.execute("INSERT OR IGNORE INTO dim_estado_inmueble VALUES (0, 'sin_clasificar', 'Sin clasificar', 99)")
    con.execute("INSERT OR IGNORE INTO dim_tipo_oferta VALUES (0, 'sin_dato', 'sin_dato')")
    con.execute("INSERT OR IGNORE INTO dim_categoria VALUES (0, 'sin_categoria', 'Sin categoría', NULL)")
    con.execute("INSERT OR IGNORE INTO dim_comercial VALUES (0, 'sin_dato', 'Sin comercial', 'sin_dato', 0)")
    con.execute("INSERT OR IGNORE INTO dim_cliente (cliente_key, cliente_id, nombre) VALUES (0, 0, 'Sin cliente')")
    con.execute("INSERT OR IGNORE INTO dim_predio (predio_key, predio_nk, origen, ubicacion_key, utilizable) "
                "VALUES (0, 'sin_dato', 'sistema', 0, 0)")
    con.execute("INSERT OR IGNORE INTO dim_inmueble (inmueble_key, inmueble_nk, origen, predio_key, tipo_inmueble_key, estado_key) "
                "VALUES (0, 'sin_dato', 'sistema', 0, 0, 0)")
