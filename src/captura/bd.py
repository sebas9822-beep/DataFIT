"""Base de datos de la captura (SQLite, lista para migrar a DuckDB o PostGIS).

Reglas que el esquema hace cumplir:
- Nada se borra en silencio: cada registro fuera del uso lleva `motivo_exclusion`.
- Cada dato guarda su fuente, capa, licencia y fecha de obtención.
- Lo que viene de Google expira (`expira_en`) y su nombre nunca se guarda.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS fuentes (
  id_fuente TEXT PRIMARY KEY, nombre TEXT, capa_concepto TEXT, tipo TEXT, metodo_acceso TEXT,
  cobertura TEXT, licencia TEXT, activa INTEGER, actualizado_en TEXT
);

CREATE TABLE IF NOT EXISTS capturas (
  captura_id TEXT PRIMARY KEY,
  creada_en TEXT NOT NULL,
  direccion_ingresada TEXT NOT NULL,
  direccion_normalizada TEXT,          -- si viene de Google se borra al expirar
  direccion_catastral TEXT,            -- placa oficial más cercana (permanente)
  lat REAL, lng REAL, x_9377 REAL, y_9377 REAL,
  origen_coordenada TEXT NOT NULL,     -- catastro | pin_usuario | google_autocomplete | google_geocoding | nominatim
  precision_geo TEXT,                  -- P1..P5
  google_place_id TEXT,
  coord_expira_en TEXT,                -- NULL = punto propio o de fuente abierta
  radio_m INTEGER NOT NULL,
  municipio TEXT,
  en_cobertura INTEGER,                -- 1 = dentro de Medellín (hay capas oficiales)
  conjunto_nombre TEXT, conjunto_tipo TEXT, conjunto_origen TEXT, conjunto_expira_en TEXT,
  estado TEXT NOT NULL,                -- en_curso | ok | parcial | error
  huella_config TEXT, version_app TEXT,
  llamadas_google INTEGER DEFAULT 0,
  avisos_json TEXT, nota_curaduria TEXT,
  curaduria_proveedor TEXT, curaduria_modelo TEXT,
  duracion_s REAL
);

CREATE TABLE IF NOT EXISTS predios (
  captura_id TEXT PRIMARY KEY REFERENCES capturas(captura_id) ON DELETE CASCADE,
  cbml TEXT, cobama TEXT, barrio_cod TEXT, barrio_nombre TEXT, comuna_cod TEXT, comuna_nombre TEXT,
  area_lote REAL, tipo_lote TEXT, condicion_predio TEXT, es_ph INTEGER,
  n_construcciones INTEGER, pisos_max REAL, area_construida REAL,
  codigo_postal TEXT, via_cercana TEXT, placa_cercana TEXT, dist_placa_m REAL,
  zge_valor_m2 REAL, zge_vigencia TEXT, tipo_valor_zge TEXT,   -- referencia oficial del suelo, no precio
  pot_tratamiento TEXT, pot_codigo TEXT, pot_categoria TEXT, pot_clase_suelo TEXT, pot_uso_general TEXT,
  bic_nombre TEXT, en_zona_influencia_bic INTEGER,
  cuadrante_policia TEXT, estacion_policia TEXT,
  estacion_masiva TEXT, estacion_masiva_linea TEXT, estacion_masiva_m REAL,
  estado_norma TEXT,                   -- ok | pendiente_capa | pendiente_fuente_municipal
  atributos_json TEXT, fuente TEXT, licencia TEXT, obtenido_en TEXT
);

CREATE TABLE IF NOT EXISTS ejecuciones (
  ejecucion_id INTEGER PRIMARY KEY AUTOINCREMENT,
  captura_id TEXT NOT NULL REFERENCES capturas(captura_id) ON DELETE CASCADE,
  fuente TEXT NOT NULL, capa TEXT, inicio TEXT, fin TEXT,
  estado TEXT NOT NULL,                -- ok | parcial | bloqueado | fallo_estructura | error | omitida
  registros INTEGER DEFAULT 0, descartados INTEGER DEFAULT 0, llamadas INTEGER DEFAULT 0,
  mensaje TEXT
);

CREATE TABLE IF NOT EXISTS entorno_items (
  item_id INTEGER PRIMARY KEY AUTOINCREMENT,
  captura_id TEXT NOT NULL REFERENCES capturas(captura_id) ON DELETE CASCADE,
  fuente TEXT NOT NULL, capa TEXT NOT NULL, id_externo TEXT NOT NULL,
  categoria TEXT NOT NULL, subtipo TEXT, categoria_original TEXT,
  confianza_clasificacion TEXT,
  nombre_original TEXT, nombre TEXT, nombre_clave TEXT,   -- Google: siempre NULL
  lat REAL, lng REAL, x_9377 REAL, y_9377 REAL,
  distancia_m REAL, anillo_m INTEGER, geometria TEXT,
  atributos_json TEXT, banderas_json TEXT, completitud REAL,
  entidad_id TEXT, es_maestro INTEGER DEFAULT 1,
  regla_dup TEXT, puntaje_dup REAL, coincide_con TEXT,
  motivo_exclusion TEXT,
  curado_por TEXT DEFAULT 'regla',     -- regla | claude
  en_mapa INTEGER DEFAULT 1,           -- 1 = lugar que cuenta en su categoría; 0 = capa masiva resumida aparte
  licencia TEXT NOT NULL, expira_en TEXT, obtenido_en TEXT NOT NULL,
  UNIQUE (captura_id, fuente, capa, id_externo)
);
CREATE INDEX IF NOT EXISTS ix_items_captura ON entorno_items(captura_id, categoria);
CREATE INDEX IF NOT EXISTS ix_items_expira ON entorno_items(expira_en);
CREATE INDEX IF NOT EXISTS ix_items_entidad ON entorno_items(captura_id, entidad_id);

CREATE TABLE IF NOT EXISTS ofertas (
  oferta_id INTEGER PRIMARY KEY AUTOINCREMENT,
  captura_id TEXT NOT NULL REFERENCES capturas(captura_id) ON DELETE CASCADE,
  fuente TEXT NOT NULL, capa TEXT NOT NULL, id_externo TEXT NOT NULL,
  operacion TEXT, tipo_inmueble TEXT, estado_publicado TEXT, estrato INTEGER,
  area_privada REAL, area_total REAL, area_usada REAL, area_tipo_usada TEXT,
  antiguedad TEXT, precio REAL, tipo_precio TEXT NOT NULL,    -- pedido | cierre (nunca se mezclan)
  administracion REAL, habitaciones INTEGER, banos INTEGER, parqueaderos INTEGER, piso INTEGER,
  precio_m2 REAL, lat REAL, lng REAL, distancia_m REAL, barrio_cod TEXT, barrio_publicado TEXT,
  anio INTEGER, fecha_publicacion TEXT, url TEXT, origen_publicacion TEXT, precision_geo TEXT,
  banderas_json TEXT, motivo_exclusion TEXT, atributos_json TEXT,
  licencia TEXT, obtenido_en TEXT NOT NULL,
  regla_dup TEXT, puntaje_dup REAL, coincide_con TEXT,   -- repetido entre sitios (paso de depuración de anuncios)
  UNIQUE (captura_id, fuente, capa, id_externo)
);
CREATE INDEX IF NOT EXISTS ix_ofertas_captura ON ofertas(captura_id, operacion, tipo_inmueble);

CREATE TABLE IF NOT EXISTS resumen_categoria (
  captura_id TEXT NOT NULL REFERENCES capturas(captura_id) ON DELETE CASCADE,
  categoria TEXT NOT NULL, fuente TEXT NOT NULL,
  conteo INTEGER NOT NULL, excluidos INTEGER DEFAULT 0, distancia_min_m REAL,
  saturado INTEGER DEFAULT 0, expira_en TEXT,
  PRIMARY KEY (captura_id, categoria, fuente)
);

CREATE TABLE IF NOT EXISTS indicadores (
  captura_id TEXT NOT NULL REFERENCES capturas(captura_id) ON DELETE CASCADE,
  indicador TEXT NOT NULL, base TEXT NOT NULL,          -- abiertas | todas (incluye Google, expira)
  valor REAL, unidad TEXT, detalle_json TEXT, expira_en TEXT,
  PRIMARY KEY (captura_id, indicador, base)
);

CREATE TABLE IF NOT EXISTS curaduria_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  captura_id TEXT NOT NULL REFERENCES capturas(captura_id) ON DELETE CASCADE,
  item_ref TEXT, etapa TEXT, campo TEXT, valor_antes TEXT, valor_despues TEXT,
  regla TEXT, origen TEXT,              -- regla | claude
  aceptado INTEGER DEFAULT 1, modelo TEXT, creado_en TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_captura ON curaduria_log(captura_id, origen);

CREATE TABLE IF NOT EXISTS cola_revision (
  rev_id INTEGER PRIMARY KEY AUTOINCREMENT,
  captura_id TEXT NOT NULL REFERENCES capturas(captura_id) ON DELETE CASCADE,
  tipo TEXT, par TEXT, detalle_json TEXT, creado_en TEXT, resuelto_por TEXT, decision TEXT
);

CREATE VIEW IF NOT EXISTS v_entorno_curado AS
  SELECT * FROM entorno_items WHERE es_maestro = 1 AND motivo_exclusion IS NULL;

-- ---------------------------------------------------------------------------------------------
-- Comerciales, clientes y cotizaciones (quién captura y para quién)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comerciales (
  usuario TEXT PRIMARY KEY,
  nombre TEXT NOT NULL,
  clave_hash TEXT NOT NULL,            -- PBKDF2-SHA256; nunca la clave en texto
  sal TEXT NOT NULL,
  iteraciones INTEGER NOT NULL,
  activo INTEGER NOT NULL DEFAULT 1,
  creado_en TEXT NOT NULL,
  clave_cambiada_en TEXT               -- NULL = sigue con la clave inicial
);

CREATE TABLE IF NOT EXISTS sesiones (
  token_sha256 TEXT PRIMARY KEY,       -- se guarda la huella del token, no el token
  usuario TEXT NOT NULL REFERENCES comerciales(usuario),
  creada_en TEXT NOT NULL, expira_en TEXT NOT NULL, cerrada_en TEXT
);

CREATE TABLE IF NOT EXISTS accesos (
  acceso_id INTEGER PRIMARY KEY AUTOINCREMENT,
  usuario TEXT NOT NULL,
  evento TEXT NOT NULL,                -- entrada | clave_errada | bloqueado | salida | clave_cambiada
  creado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_accesos ON accesos(usuario, creado_en);

CREATE TABLE IF NOT EXISTS clientes (
  cliente_id INTEGER PRIMARY KEY AUTOINCREMENT,
  documento TEXT NOT NULL UNIQUE,      -- cédula, solo dígitos
  nombre TEXT NOT NULL,                -- el último registrado; cada cotización guarda el de ese momento
  autoriza_datos INTEGER NOT NULL DEFAULT 0,
  autorizado_en TEXT, autorizado_por TEXT,
  texto_autorizacion_sha256 TEXT,      -- huella del texto que se aceptó
  creado_por TEXT NOT NULL REFERENCES comerciales(usuario),
  creado_en TEXT NOT NULL,
  actualizado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cotizaciones (
  cotizacion_id TEXT PRIMARY KEY,
  comercial TEXT NOT NULL REFERENCES comerciales(usuario),
  cliente_id INTEGER NOT NULL REFERENCES clientes(cliente_id),
  cliente_nombre TEXT NOT NULL,        -- copia al momento de la cotización (evidencia)
  cliente_documento TEXT NOT NULL,
  direccion_ingresada TEXT,
  captura_id TEXT REFERENCES capturas(captura_id),
  reusada INTEGER NOT NULL DEFAULT 0,  -- 1 = se reabrió una captura reciente del mismo punto
  estado TEXT NOT NULL,                -- en_curso | ok | parcial | error
  mensaje TEXT,
  evidencia_json TEXT,                 -- lo que vio el comercial: dirección, lote, lugares por categoría...
  evidencia_sha256 TEXT,
  huella_config TEXT, version_app TEXT,
  creada_en TEXT NOT NULL, terminada_en TEXT
);
CREATE INDEX IF NOT EXISTS ix_cot_comercial ON cotizaciones(comercial, creada_en);
CREATE INDEX IF NOT EXISTS ix_cot_cliente ON cotizaciones(cliente_id, creada_en);

-- Una fila por cotización con todo lo necesario para agrupar por comercial, cliente, barrio o fecha.
CREATE VIEW IF NOT EXISTS v_cotizaciones AS
  SELECT q.cotizacion_id, q.creada_en, substr(q.creada_en, 1, 10) AS fecha, q.estado, q.reusada,
         q.comercial, co.nombre AS comercial_nombre,
         q.cliente_id, q.cliente_nombre, q.cliente_documento,
         q.direccion_ingresada, ca.direccion_catastral, pr.barrio_nombre, pr.comuna_nombre, pr.cbml,
         q.captura_id, q.evidencia_sha256
  FROM cotizaciones q
  JOIN comerciales co ON co.usuario = q.comercial
  LEFT JOIN capturas ca ON ca.captura_id = q.captura_id
  LEFT JOIN predios pr ON pr.captura_id = q.captura_id;

-- ---------------------------------------------------------------------------------------------
-- Aplicación única: fichas del inmueble, metas y seguimiento comercial
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fichas (
  ficha_id TEXT PRIMARY KEY,           -- <cotizacion_id>:v<version>
  cotizacion_id TEXT NOT NULL REFERENCES cotizaciones(cotizacion_id),
  version INTEGER NOT NULL,
  vigente INTEGER NOT NULL DEFAULT 1,  -- 1 = la última; las anteriores se conservan
  comercial TEXT NOT NULL REFERENCES comerciales(usuario),
  cliente_id INTEGER NOT NULL REFERENCES clientes(cliente_id),
  generada_en TEXT NOT NULL,
  estado TEXT NOT NULL,                -- ok | sin_mercado | incompleta | error
  mensaje TEXT,
  ruta_md TEXT, ruta_json TEXT,        -- relativas a la carpeta de fichas (una carpeta por NIT o cédula)
  sha256_md TEXT, sha256_json TEXT,
  resumen_json TEXT,                   -- lo que alimenta fact_ficha: n, mediana, CV, confianza, base...
  corrida_etl INTEGER, huella_parametros TEXT, version_motor TEXT,
  UNIQUE (cotizacion_id, version)
);
CREATE INDEX IF NOT EXISTS ix_fichas_cot ON fichas(cotizacion_id, vigente);

CREATE TABLE IF NOT EXISTS metas (
  comercial TEXT NOT NULL REFERENCES comerciales(usuario),
  anio_mes TEXT NOT NULL,              -- AAAA-MM
  meta_cotizaciones INTEGER, meta_clientes INTEGER,
  definida_por TEXT NOT NULL REFERENCES comerciales(usuario),
  creada_en TEXT NOT NULL, actualizada_en TEXT NOT NULL,
  PRIMARY KEY (comercial, anio_mes)
);

CREATE TABLE IF NOT EXISTS metas_historial (     -- una meta que cambia no pisa la anterior en silencio
  hist_id INTEGER PRIMARY KEY AUTOINCREMENT,
  comercial TEXT NOT NULL, anio_mes TEXT NOT NULL,
  meta_cotizaciones INTEGER, meta_clientes INTEGER,
  definida_por TEXT NOT NULL, registrada_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seguimientos (
  seguimiento_id INTEGER PRIMARY KEY AUTOINCREMENT,
  supervisor TEXT NOT NULL REFERENCES comerciales(usuario),
  comercial TEXT NOT NULL REFERENCES comerciales(usuario),
  cotizacion_id TEXT REFERENCES cotizaciones(cotizacion_id),
  tipo TEXT NOT NULL,                  -- observacion | compromiso | reconocimiento | alerta
  nota TEXT NOT NULL,
  estado TEXT NOT NULL DEFAULT 'abierto',          -- abierto | cerrado
  fecha_compromiso TEXT,
  creado_en TEXT NOT NULL, cerrado_en TEXT, cerrado_por TEXT
);
CREATE INDEX IF NOT EXISTS ix_seg_comercial ON seguimientos(comercial, estado);

-- Estado de la ficha con el cliente (decisión DU-37): historial; vale la última fila de cada cotización.
-- Solo se agregan filas: el comercial registra un estado nuevo, nunca corrige uno anterior.
CREATE TABLE IF NOT EXISTS gestion_cliente (
  gestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cotizacion_id TEXT NOT NULL REFERENCES cotizaciones(cotizacion_id),
  cliente_id INTEGER NOT NULL REFERENCES clientes(cliente_id),
  comercial TEXT NOT NULL REFERENCES comerciales(usuario),
  estado TEXT NOT NULL,                -- los de reglas.yaml › gestion_cliente.estados
  motivo_tipo TEXT, motivo TEXT,       -- obligatorios cuando el estado lo exige (rechazada)
  nota TEXT,
  ficha_version INTEGER,               -- versión de la ficha vigente al registrar el estado
  registrado_en TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gestion_cliente ON gestion_cliente(cotizacion_id, gestion_id);

-- Bitácora de administración (decisión DU-36): qué hizo el administrador, sobre qué y cuándo. Nunca guarda claves.
CREATE TABLE IF NOT EXISTS admin_bitacora (
  evento_id INTEGER PRIMARY KEY AUTOINCREMENT,
  administrador TEXT NOT NULL,
  accion TEXT NOT NULL,                -- usuario_creado | usuario_editado | usuario_eliminado | clave_restablecida |
                                       -- sitio_agregado | sitio_editado | sitio_eliminado | respaldo_creado | respaldo_eliminado
  objeto TEXT,                         -- el usuario, el sitio o el archivo
  detalle_json TEXT,                   -- qué cambió (antes y después)
  creado_en TEXT NOT NULL
);

-- Lo que busca el cliente en una cotización: una fila por tipo de inmueble (decisión DU-19).
-- Solo se agregan filas: lo que el cliente pidió al cotizar no se reescribe.
CREATE TABLE IF NOT EXISTS cotizacion_intereses (
  cotizacion_id TEXT NOT NULL REFERENCES cotizaciones(cotizacion_id),
  tipo_inmueble TEXT NOT NULL,
  orden INTEGER NOT NULL DEFAULT 0,
  origen TEXT NOT NULL DEFAULT 'declarado',        -- declarado | tipo_cotizado (se dedujo del inmueble cotizado)
  registrado_en TEXT NOT NULL,
  PRIMARY KEY (cotizacion_id, tipo_inmueble)
);
CREATE INDEX IF NOT EXISTS ix_interes_tipo ON cotizacion_intereses(tipo_inmueble);
"""

# Columnas que se agregan a tablas que ya existían en la fase 1 (la base anterior se conserva).
MIGRACIONES = [
    ("comerciales", "rol", "TEXT NOT NULL DEFAULT 'comercial'"),      # comercial | supervisor
    ("comerciales", "supervisa_json", "TEXT"),                         # 'todos' o lista de usuarios
    ("clientes", "tipo_documento", "TEXT NOT NULL DEFAULT 'CC'"),      # CC | NIT | CE
    ("cotizaciones", "cliente_tipo_documento", "TEXT"),
    ("cotizaciones", "inm_tipo", "TEXT"),                              # datos del inmueble declarados al cotizar
    ("cotizaciones", "inm_area_m2", "REAL"),
    ("cotizaciones", "inm_estado", "TEXT"),
    ("cotizaciones", "inm_habitaciones", "INTEGER"),
    ("cotizaciones", "inm_banos", "INTEGER"),
    ("cotizaciones", "inm_parqueaderos", "INTEGER"),
    ("clientes", "celular", "TEXT"),                                # solo en la base local; enmascarado en listas
    ("cotizaciones", "inm_extras_json", "TEXT"),                     # características adicionales [{nombre, valor}]
    ("cotizaciones", "portales_json", "TEXT"),                       # inmobiliarias encendidas al cotizar
    ("cotizaciones", "rpa_json", "TEXT"),                            # resultado de la búsqueda en inmobiliarias
    ("ofertas", "regla_dup", "TEXT"),                                # anuncio repetido entre sitios: regla,
    ("ofertas", "puntaje_dup", "REAL"),                              # puntaje (1.0 = iguales)
    ("ofertas", "coincide_con", "TEXT"),                             # y el anuncio que se conserva
    ("comerciales", "editado_en", "TEXT"),                           # lo cambió el administrador: ya no se sincroniza desde usuarios.yaml
    ("comerciales", "eliminado_en", "TEXT"),                         # eliminado por el administrador (su historial se conserva)
    ("comerciales", "creado_por", "TEXT"),                           # administrador que lo creó; vacío = usuarios.yaml
]


def migrar(con: sqlite3.Connection) -> list[str]:
    """Agrega las columnas que falten. Nunca borra ni reescribe datos existentes."""
    hechas = []
    for tabla, columna, tipo in MIGRACIONES:
        existentes = {f[1] for f in con.execute(f"PRAGMA table_info({tabla})")}
        if columna not in existentes:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")
            hechas.append(f"{tabla}.{columna}")
    # Una cotización con tipo de inmueble declarado y sin búsqueda registrada: el cliente buscaba al menos ese tipo.
    n = con.execute(
        "INSERT OR IGNORE INTO cotizacion_intereses (cotizacion_id, tipo_inmueble, orden, origen, registrado_en) "
        "SELECT q.cotizacion_id, q.inm_tipo, 0, 'tipo_cotizado', q.creada_en FROM cotizaciones q "
        "WHERE q.inm_tipo IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cotizacion_intereses i "
        "WHERE i.cotizacion_id = q.cotizacion_id AND i.tipo_inmueble = q.inm_tipo)").rowcount
    if n:
        hechas.append(f"cotizacion_intereses: {n} deducidos del tipo cotizado")
    return hechas


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conectar(ruta: Path) -> sqlite3.Connection:
    Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(ruta), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def iniciar(p: dict) -> None:
    """Crea el esquema y sincroniza el catálogo de fuentes desde fuentes.yaml."""
    with conectar(p["rutas"]["bd"]) as con:
        con.executescript(ESQUEMA)
        migrar(con)
        for idf, f in p["fuentes"]["fuentes"].items():
            con.execute(
                "INSERT OR REPLACE INTO fuentes VALUES (?,?,?,?,?,?,?,?,?)",
                (idf, f.get("nombre"), f.get("capa_concepto"), f.get("tipo"), f.get("metodo_acceso"),
                 f.get("cobertura"), f.get("licencia"), int(bool(f.get("activa"))), ahora()))
    from . import acceso                                  # evita importación circular
    acceso.sembrar_comerciales(p)


def insertar(con: sqlite3.Connection, tabla: str, fila: dict, modo: str = "INSERT") -> None:
    columnas = ", ".join(fila)
    marcas = ", ".join("?" for _ in fila)
    con.execute(f"{modo} INTO {tabla} ({columnas}) VALUES ({marcas})", list(fila.values()))


def a_json(valor) -> str | None:
    if valor is None:
        return None
    return json.dumps(valor, ensure_ascii=False, default=str)


def de_json(texto, defecto=None):
    if not texto:
        return defecto
    try:
        return json.loads(texto)
    except (TypeError, ValueError):
        return defecto


def purgar_expirados(p: dict) -> dict[str, int]:
    """Regla de 30 días de Google: borra coordenadas, direcciones y agregados vencidos.
    Conserva el place_id y la categoría propia."""
    t = ahora()
    with conectar(p["rutas"]["bd"]) as con:
        a = con.execute(
            "UPDATE entorno_items SET lat=NULL, lng=NULL, x_9377=NULL, y_9377=NULL, distancia_m=NULL, "
            "anillo_m=NULL, atributos_json=NULL, subtipo=NULL, expira_en=NULL "
            "WHERE expira_en IS NOT NULL AND expira_en < ?", (t,)).rowcount
        b = con.execute(
            "UPDATE capturas SET lat=NULL, lng=NULL, x_9377=NULL, y_9377=NULL, direccion_normalizada=NULL, "
            "coord_expira_en=NULL WHERE coord_expira_en IS NOT NULL AND coord_expira_en < ?", (t,)).rowcount
        c = con.execute(
            "UPDATE capturas SET conjunto_nombre=NULL, conjunto_expira_en=NULL "
            "WHERE conjunto_expira_en IS NOT NULL AND conjunto_expira_en < ?", (t,)).rowcount
        con.execute("DELETE FROM sesiones WHERE expira_en < ?", (t,))
        d = con.execute("DELETE FROM resumen_categoria WHERE expira_en IS NOT NULL AND expira_en < ?", (t,)).rowcount
        e = con.execute("DELETE FROM indicadores WHERE expira_en IS NOT NULL AND expira_en < ?", (t,)).rowcount
    return {"items_depurados": a, "capturas_depuradas": b, "conjuntos_depurados": c,
            "resumenes_borrados": d, "indicadores_borrados": e}
