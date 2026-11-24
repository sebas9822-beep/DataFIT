"""ETL del almacén: extrae de la base operativa y de los insumos controlados, transforma y carga la estrella.

    python -m almacen.etl                 corrida completa (todas las capturas, insumos del reto y gestión)
    python -m almacen.etl --captura ID    solo una captura (es lo que corre la aplicación al terminar cada una)
    python -m almacen.etl --gestion       solo comerciales, clientes, cotizaciones, fichas y metas

Extracción   · base operativa `captura.db` (solo lectura) y archivos de `reto_oficial/data` (con su huella).
Transformación · limpieza y deduplicación del motor de ficha (reglas oficiales), normalización de barrios,
               fechas a hora local, sistema de transporte por línea y dimensiones conformadas.
Carga        · dimensiones por clave natural (las claves sustitutas no cambian entre corridas) y hechos
               por alcance: volver a correr no duplica ni pierde nada.

Cada corrida deja su conciliación (`etl_paso`: leídos = cargados + duplicados + rechazados), los rechazos con
su motivo (`etl_rechazo`), los valores que cambió (`etl_ajuste`) y los controles de calidad (`etl_calidad`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from captura import __version__ as VERSION_APP
from captura import bd as bd_op
from captura import geo
from ficha import carga as carga_ficha
from ficha import config as cfg_ficha
from ficha import deduplicacion, limpieza

from . import calidad, esquema
from . import normalizacion as nz

CANDADO = threading.RLock()
BASE_RETO = "muestra_v1"
FUENTE_CAPAS_RETO = "osm_reto"
LOTE_SQL = 400                    # variables por consulta IN (...): límite técnico de SQLite, no regla de negocio


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def params_ficha(p: dict) -> dict:
    """Parámetros del motor de ficha (ficha.yaml + parametros.yaml). Una prueba puede inyectarlos en p['_ficha']."""
    if p.get("_ficha") is None:
        p["_ficha"] = cfg_ficha.cargar_params()
    return p["_ficha"]


def _sha_archivo(ruta: Path) -> str | None:
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest() if Path(ruta).exists() else None


# =============================================================================================
# Bitácora de la corrida
# =============================================================================================
class Corrida:
    def __init__(self, con: sqlite3.Connection, modo: str, alcance: str, p: dict, pf: dict) -> None:
        self.con, self.modo = con, modo
        huella = f"{p['huella']}|{p['huella_almacen']}|{pf['_meta']['hash_params'][:16]}"
        self.id = con.execute(
            "INSERT INTO etl_corrida (inicio, modo, alcance, estado, version_app, version_esquema, huella_config) "
            "VALUES (?,?,?,?,?,?,?)", (ahora(), modo, alcance, "en_curso", VERSION_APP, esquema.VERSION_ESQUEMA, huella)
        ).lastrowid
        self.pasos: dict[tuple[str, str], dict] = {}
        self.resumen: dict = {}

    def paso(self, entidad: str, alcance: str = "", leidos: int = 0, cargados: int = 0, duplicados: int = 0,
             rechazados: int = 0, detalle: str | None = None) -> None:
        f = self.pasos.setdefault((entidad, alcance), {"leidos": 0, "cargados": 0, "duplicados": 0, "rechazados": 0,
                                                       "detalle": None})
        f["leidos"] += leidos
        f["cargados"] += cargados
        f["duplicados"] += duplicados
        f["rechazados"] += rechazados
        f["detalle"] = detalle or f["detalle"]

    def rechazo(self, entidad: str, motivo: str, clave: str | None = None, alcance: str | None = None, n: int = 1,
                detalle: str | None = None) -> None:
        self.con.execute("INSERT INTO etl_rechazo (corrida_id, entidad, alcance, clave, motivo_exclusion, n, detalle) "
                         "VALUES (?,?,?,?,?,?,?)", (self.id, entidad, alcance, clave, motivo, n, detalle))

    def ajuste(self, entidad: str, clave: str | None, campo: str, antes, despues, regla: str) -> None:
        self.con.execute("INSERT INTO etl_ajuste (corrida_id, entidad, clave, campo, valor_antes, valor_despues, regla) "
                         "VALUES (?,?,?,?,?,?,?)", (self.id, entidad, clave, campo,
                                                    None if antes is None else str(antes),
                                                    None if despues is None else str(despues), regla))

    def control(self, nombre: str, tabla: str | None, resultado: str, valor=None, umbral=None, detalle: str | None = None) -> None:
        self.con.execute("INSERT OR REPLACE INTO etl_calidad (corrida_id, control, tabla, resultado, valor, umbral, detalle) "
                         "VALUES (?,?,?,?,?,?,?)", (self.id, nombre, tabla, resultado, valor, umbral, detalle))

    def cerrar(self) -> dict:
        descuadres = []
        for (entidad, alcance), f in sorted(self.pasos.items()):
            cuadra = int(f["leidos"] == f["cargados"] + f["duplicados"] + f["rechazados"])
            if not cuadra:
                descuadres.append(f"{entidad}/{alcance or 'todo'}")
            self.con.execute("INSERT OR REPLACE INTO etl_paso VALUES (?,?,?,?,?,?,?,?,?)",
                             (self.id, entidad, alcance, f["leidos"], f["cargados"], f["duplicados"], f["rechazados"],
                              cuadra, f["detalle"]))
        self.control("conciliacion_leidos_cargados", "etl_paso", "falla" if descuadres else "ok", len(descuadres), 0,
                     ("No cuadran: " + ", ".join(descuadres)) if descuadres else
                     "En cada entidad: leídos = cargados + duplicados + rechazados")
        res = {r["resultado"]: r["n"] for r in self.con.execute(
            "SELECT resultado, COUNT(*) AS n FROM etl_calidad WHERE corrida_id=? GROUP BY resultado", (self.id,))}
        estado = "con_fallas" if res.get("falla") else ("con_alertas" if res.get("alerta") else "ok")
        self.resumen["controles"] = res
        self.resumen["pasos"] = {f"{e}/{a}" if a else e: {k: v for k, v in f.items() if k != "detalle"}
                                 for (e, a), f in sorted(self.pasos.items())}
        self.con.execute("UPDATE etl_corrida SET fin=?, estado=?, resumen_json=? WHERE corrida_id=?",
                         (ahora(), estado, json.dumps(self.resumen, ensure_ascii=False, sort_keys=True), self.id))
        return {"corrida_id": self.id, "estado": estado, **self.resumen}


# =============================================================================================
# Dimensiones conformadas (clave natural → clave sustituta estable)
# =============================================================================================
class Dimensiones:
    def __init__(self, con: sqlite3.Connection, p: dict, pf: dict, corrida: Corrida) -> None:
        self.con, self.p, self.pf, self.corrida = con, p, pf, corrida
        cfg = p["almacen"]
        self.desfase = cfg["tiempo"]["desfase_utc_horas"]
        self.alias = {nz.texto_base(k): v for k, v in (cfg.get("barrios_alias") or {}).items()}
        self.zonas: dict[tuple[str, str], str] = {}
        for zona, z in (cfg.get("zonas_mercado") or {}).items():
            for barrio in z.get("barrios") or []:
                self.zonas[(nz.clave_municipio(z.get("municipio")), nz.clave_barrio(barrio))] = zona
        self.lineas = {nz.texto_base(k): v for k, v in (cfg["transporte"].get("lineas") or {}).items()}
        self.municipios = {nz.clave_municipio(k): v for k, v in (cfg.get("municipios") or {}).items()}
        self._fechas: set[int] = {r[0] for r in con.execute("SELECT fecha_key FROM dim_fecha")}
        self._cache: dict[str, dict] = {}

    # ---- utilidades ----
    def _mapa(self, tabla: str, col_nk: str, col_key: str) -> dict:
        if tabla not in self._cache:
            self._cache[tabla] = {r[0]: r[1] for r in self.con.execute(f"SELECT {col_nk}, {col_key} FROM {tabla}")}
        return self._cache[tabla]

    def _upsert(self, tabla: str, col_nk: str, col_key: str, nk, valores: dict, solo_si_falta: bool = False) -> int:
        mapa = self._mapa(tabla, col_nk, col_key)
        if nk in mapa:
            if valores and not solo_si_falta:
                self.con.execute(f"UPDATE {tabla} SET {', '.join(f'{k}=?' for k in valores)} WHERE {col_key}=?",
                                 (*valores.values(), mapa[nk]))
            return mapa[nk]
        fila = {col_nk: nk, **valores}
        clave = self.con.execute(f"INSERT INTO {tabla} ({', '.join(fila)}) VALUES ({', '.join('?' for _ in fila)})",
                                 list(fila.values())).lastrowid
        mapa[nk] = clave
        return clave

    # ---- dimensiones ----
    def fecha(self, valor) -> int:
        d = nz.a_fecha(valor)
        if d is None:
            return 0
        f = nz.fila_fecha(d)
        if f["fecha_key"] not in self._fechas:
            self.con.execute(f"INSERT OR IGNORE INTO dim_fecha ({', '.join(f)}) VALUES ({', '.join('?' for _ in f)})",
                             list(f.values()))
            self._fechas.add(f["fecha_key"])
        return f["fecha_key"]

    def fecha_local(self, marca_utc: str | None) -> tuple[int, int | None]:
        """(fecha_key, hora) en hora local a partir de una marca UTC de la base operativa."""
        t = nz.a_local(marca_utc, self.desfase)
        return (0, None) if t is None else (self.fecha(t.date()), t.hour)

    def fuente(self, fuente_id: str | None, **valores) -> int:
        if not fuente_id:
            return 0
        return self._upsert("dim_fuente", "fuente_id", "fuente_key", str(fuente_id), valores, solo_si_falta=not valores)

    def tipo_inmueble(self, tipo: str | None) -> int:
        tipo = nz.texto_base(tipo).replace(" ", "_")
        if not tipo:
            return 0
        return self._upsert("dim_tipo_inmueble", "tipo_inmueble", "tipo_inmueble_key", tipo, {"uso": "sin_clasificar"},
                            solo_si_falta=True)

    def estado(self, estado: str | None) -> int:
        return self._mapa("dim_estado_inmueble", "estado", "estado_key").get(estado or "sin_clasificar", 0)

    def tipo_oferta(self, operacion: str | None, tipo_precio: str | None) -> int:
        operacion = operacion if operacion in ("venta", "arriendo") else "sin_dato"
        tipo_precio = tipo_precio if tipo_precio in ("pedido", "cierre") else "sin_dato"
        mapa = self._cache.setdefault("dim_tipo_oferta", {(r[0], r[1]): r[2] for r in self.con.execute(
            "SELECT operacion, tipo_precio, tipo_oferta_key FROM dim_tipo_oferta")})
        if (operacion, tipo_precio) not in mapa:
            mapa[(operacion, tipo_precio)] = self.con.execute(
                "INSERT INTO dim_tipo_oferta (operacion, tipo_precio) VALUES (?,?)", (operacion, tipo_precio)).lastrowid
        return mapa[(operacion, tipo_precio)]

    def categoria(self, categoria: str | None) -> int:
        if not categoria:
            return 0
        return self._upsert("dim_categoria", "categoria", "categoria_key", categoria,
                            {"etiqueta": categoria.replace("_", " ").capitalize()}, solo_si_falta=True)

    def ubicacion(self, municipio: str | None, barrio: str | None, oficial: bool = False, comuna: str | None = None,
                  comuna_cod: str | None = None, barrio_cod: str | None = None, entidad: str | None = None,
                  clave_registro: str | None = None) -> int:
        """Barrio conformado. Las grafías conocidas van a su nombre de catálogo; el resto conserva su texto."""
        mun = nz.clave_municipio(municipio)
        if not mun:
            return 0
        limpio = nz.limpiar_barrio(barrio)
        canonico = self.alias.get(nz.texto_base(limpio))
        nombre = canonico or nz.titulo(limpio)
        clave = nz.clave_barrio(nombre)
        nk = f"{mun}|{clave}"
        if entidad and barrio and str(barrio).strip() != nombre:
            self.corrida.ajuste(entidad, clave_registro, "barrio", barrio, nombre,
                                "alias_de_barrio" if canonico else "barrio_sin_referencias")
        mapa = self._mapa("dim_ubicacion", "ubicacion_nk", "ubicacion_key")
        zona = self.zonas.get((mun, clave))
        if nk in mapa:
            if oficial:
                self.con.execute("UPDATE dim_ubicacion SET barrio=?, comuna=COALESCE(?, comuna), comuna_cod=COALESCE(?, comuna_cod), "
                                 "barrio_cod=COALESCE(?, barrio_cod), zona_mercado=?, revisado=1 WHERE ubicacion_key=?",
                                 (nombre, comuna, comuna_cod, barrio_cod, zona, mapa[nk]))
            else:
                self.con.execute("UPDATE dim_ubicacion SET zona_mercado=? WHERE ubicacion_key=?", (zona, mapa[nk]))
            return mapa[nk]
        return self._upsert("dim_ubicacion", "ubicacion_nk", "ubicacion_key", nk, {
            "municipio": self.municipios.get(mun) or nz.titulo(str(municipio).capitalize() if str(municipio).islower() else municipio),
            "comuna_cod": comuna_cod, "comuna": comuna, "barrio_cod": barrio_cod,
            "barrio": nombre or "Sin barrio informado", "barrio_clave": clave, "zona_mercado": zona,
            "revisado": int(bool(oficial or canonico or zona))})

    def sistema(self, linea: str | None) -> str | None:
        return self.lineas.get(nz.texto_base(linea)) if linea else None


def cargar_catalogos(dims: Dimensiones) -> None:
    """Dimensiones pequeñas que salen de la configuración."""
    con, p, pf = dims.con, dims.p, dims.pf
    cfg = p["almacen"]
    for idf, f in p["fuentes"]["fuentes"].items():
        dims.fuente(idf, nombre=f.get("nombre"), tipo=f.get("tipo"), origen_datos="captura",
                    licencia=f.get("licencia"), activa=int(bool(f.get("activa"))))
    dims.fuente(FUENTE_CAPAS_RETO, nombre="OpenStreetMap · capas entregadas con el reto", tipo="abierta",
                origen_datos="reto", licencia="ODbL", activa=1)
    for uso, tipos in (cfg.get("tipos_inmueble") or {}).items():
        for tipo in tipos:
            clave = dims.tipo_inmueble(tipo)
            con.execute("UPDATE dim_tipo_inmueble SET uso=? WHERE tipo_inmueble_key=?", (uso, clave))
    etiquetas = p["reglas"]["inmueble"]["etiquetas_estado"]
    for orden, estado in enumerate(pf["reglas"]["estados"], start=1):
        dims._upsert("dim_estado_inmueble", "estado", "estado_key", estado,
                     {"etiqueta": etiquetas.get(estado, estado.replace("_", " ").capitalize()), "orden": orden})
    for operacion in ("venta", "arriendo"):
        for tipo_precio in pf["reglas"]["comparables"]["tipos_precio"]:
            dims.tipo_oferta(operacion, tipo_precio)
    en_ficha = cfg.get("categorias_ficha") or {}
    for cat, meta in p["taxonomia"]["categorias"].items():
        dims._upsert("dim_categoria", "categoria", "categoria_key", cat,
                     {"etiqueta": meta.get("etiqueta"), "categoria_ficha": en_ficha.get(cat)})


# =============================================================================================
# Extracción y carga · una captura de la base operativa (Momento 1)
# =============================================================================================
def _orden_externo(id_externo: str, respaldo: int) -> int:
    """'1:10590' → 1000010590: orden estable de la fuente para la regla "se conserva la primera"."""
    try:
        capa, oid = str(id_externo).split(":", 1)
        return int(capa) * 1_000_000_000 + int(oid)
    except (ValueError, TypeError):
        return respaldo


def cargar_predio_captura(dims: Dimensiones, con_op: sqlite3.Connection, captura_id: str) -> tuple[int, bool] | None:
    """Dimensión del predio capturado. Devuelve (predio_key, utilizable) o None si la captura no existe."""
    c = con_op.execute("SELECT * FROM capturas WHERE captura_id=?", (captura_id,)).fetchone()
    if c is None:
        return None
    pr = con_op.execute("SELECT * FROM predios WHERE captura_id=?", (captura_id,)).fetchone()
    utilizable = c["estado"] in dims.p["almacen"]["capturas"]["estados_utilizables"]
    municipio = c["municipio"] or ("Medellín" if c["en_cobertura"] else None)
    ubic = 0
    if pr is not None and pr["barrio_nombre"] and municipio:
        ubic = dims.ubicacion(municipio, pr["barrio_nombre"], oficial=True, comuna=nz.titulo(pr["comuna_nombre"]),
                              comuna_cod=pr["comuna_cod"], barrio_cod=pr["barrio_cod"])
    control = con_op.execute("SELECT valor FROM indicadores WHERE captura_id=? AND indicador='control_distancia_9377_max_dif_m' "
                             "ORDER BY base LIMIT 1", (captura_id,)).fetchone()
    fecha_key, _ = dims.fecha_local(c["creada_en"])
    g = (lambda k: pr[k] if pr is not None else None)
    valores = {
        "origen": "captura", "captura_id": captura_id,
        "direccion": c["direccion_catastral"] or c["direccion_ingresada"], "cbml": g("cbml"),
        "conjunto": c["conjunto_nombre"], "ubicacion_key": ubic, "municipio": municipio, "barrio_texto": g("barrio_nombre"),
        "lat": c["lat"], "lng": c["lng"], "x_9377": c["x_9377"], "y_9377": c["y_9377"],
        "precision_coordenadas": c["precision_geo"], "fuente_coordenadas": c["origen_coordenada"],
        "radio_m": c["radio_m"], "en_cobertura": c["en_cobertura"], "estado_captura": c["estado"],
        "utilizable": int(utilizable),
        "pot_tratamiento": g("pot_tratamiento"), "pot_codigo": g("pot_codigo"), "pot_categoria": g("pot_categoria"),
        "pot_clase_suelo": g("pot_clase_suelo"), "pot_uso_general": g("pot_uso_general"), "estado_norma": g("estado_norma"),
        "zge_valor_m2": g("zge_valor_m2"), "es_ph": g("es_ph"), "area_lote": g("area_lote"), "pisos_max": g("pisos_max"),
        "estacion_masiva": g("estacion_masiva"), "estacion_masiva_linea": g("estacion_masiva_linea"),
        "estacion_masiva_m": g("estacion_masiva_m"),
        "control_9377_max_dif_m": control["valor"] if control else None, "fecha_key": fecha_key,
        "huella_config": c["huella_config"], "version_app": c["version_app"]}
    return dims._upsert("dim_predio", "predio_nk", "predio_key", f"captura:{captura_id}", valores), utilizable


def _campo(fila: sqlite3.Row, columna: str):
    """Columna que una base operativa de una versión anterior puede no tener todavía."""
    return fila[columna] if columna in fila.keys() else None


def _fila_oferta_captura(dims: Dimensiones, o: sqlite3.Row, base: str, cfg_base: dict) -> dict:
    pf = dims.pf
    sin_mapeo = pf["limpieza"]["estado_sin_mapeo"]
    area, precio = nz.real(o["area_usada"]), nz.real(o["precio"])
    area = area if area and area > 0 else None
    precio = precio if precio and precio > 0 else None
    estado = limpieza.normalizar_estado(o["estado_publicado"], pf["reglas"]["estados"], sin_mapeo)
    fila = {"area_m2": area if area is not None else float("nan"), "precio": precio if precio is not None else float("nan"),
            "precio_m2": (precio / area) if area and precio else float("nan"), "estado_canonico": estado,
            "tipo_precio": o["tipo_precio"]}
    motivo = o["motivo_exclusion"] or limpieza.motivo_invalido(
        pd.Series(fila), pf["reglas"], sin_mapeo, exigir_estado=bool(cfg_base.get("exige_estado", True)),
        validar_rango=o["operacion"] == cfg_base.get("operacion_comparable", "venta"))
    atributos = bd_op.de_json(o["atributos_json"], {}) or {}
    presentes = [area, precio, o["habitaciones"], o["banos"], o["parqueaderos"], o["estrato"], o["fecha_publicacion"]]
    return {
        "oferta_nk": f"{o['fuente']}:{o['id_externo']}", "base_mercado": base,
        "fuente_key": dims.fuente(o["fuente"]), "fecha_key": dims.fecha(o["fecha_publicacion"]),
        "ubicacion_key": dims.ubicacion("Medellín", o["barrio_publicado"]) if o["barrio_publicado"] else 0,
        "tipo_inmueble_key": dims.tipo_inmueble(o["tipo_inmueble"]), "estado_key": dims.estado(estado),
        "tipo_oferta_key": dims.tipo_oferta(o["operacion"], o["tipo_precio"]),
        "origen": f"{o['fuente']}:{o['id_externo']}", "id_anuncio": str(atributos.get("codigo_origen") or ""),
        "orden_origen": _orden_externo(o["id_externo"], o["oferta_id"]),
        "zona": None, "estado_texto": o["estado_publicado"], "tipo_precio_texto": o["tipo_precio"], "url": o["url"],
        "area_m2": area, "precio": precio, "precio_m2": (precio / area) if area and precio else None,
        "habitaciones": o["habitaciones"], "banos": o["banos"], "parqueaderos": o["parqueaderos"],
        "estrato": o["estrato"], "antiguedad": o["antiguedad"], "lat": o["lat"], "lng": o["lng"],
        "completitud": round(sum(v is not None for v in presentes) / len(presentes), 4), "motivo_invalido": motivo,
        "observada_en": o["obtenido_en"],
        # repetido entre sitios: lo marcó la depuración de anuncios, antes del ETL (se conserva tal cual)
        "regla_dup_previa": _campo(o, "regla_dup"), "puntaje_dup_previo": _campo(o, "puntaje_dup"),
        "coincide_con_previo": _campo(o, "coincide_con")}


_COLS_OFERTA = ["oferta_nk", "base_mercado", "fuente_key", "fecha_key", "ubicacion_key", "tipo_inmueble_key", "estado_key",
                "tipo_oferta_key", "origen", "id_anuncio", "orden_origen", "zona", "estado_texto", "tipo_precio_texto", "url",
                "area_m2", "precio", "precio_m2", "habitaciones", "banos", "parqueaderos", "estrato", "antiguedad",
                "lat", "lng", "completitud", "motivo_invalido", "observada_en", "corrida_id",
                "regla_dup_previa", "puntaje_dup_previo", "coincide_con_previo"]
_SQL_OFERTA = (f"INSERT INTO fact_oferta ({', '.join(_COLS_OFERTA)}) VALUES ({', '.join('?' for _ in _COLS_OFERTA)}) "
               "ON CONFLICT(oferta_nk) DO UPDATE SET "
               + ", ".join(f"{c}=excluded.{c}" for c in _COLS_OFERTA if c != "oferta_nk"))


def _guardar_ofertas(con: sqlite3.Connection, filas: list[dict], corrida_id: int) -> dict[str, int]:
    con.executemany(_SQL_OFERTA, [[dict(f, corrida_id=corrida_id).get(c) for c in _COLS_OFERTA] for f in filas])
    claves: dict[str, int] = {}
    nks = [f["oferta_nk"] for f in filas]
    for i in range(0, len(nks), LOTE_SQL):
        lote = nks[i:i + LOTE_SQL]
        for r in con.execute(f"SELECT oferta_nk, oferta_key FROM fact_oferta WHERE oferta_nk IN ({', '.join('?' for _ in lote)})", lote):
            claves[r[0]] = r[1]
    return claves


def cargar_captura(dims: Dimensiones, con_op: sqlite3.Connection, captura_id: str) -> dict:
    """Carga el predio, sus ofertas y su entorno. Reemplaza lo que hubiera de esa captura (idempotente)."""
    con, corrida, cfg = dims.con, dims.corrida, dims.p["almacen"]
    alcance = captura_id[:8]
    cargado = cargar_predio_captura(dims, con_op, captura_id)
    if cargado is None:
        corrida.rechazo("captura", "captura_inexistente", captura_id, alcance)
        return {"captura_id": captura_id, "cargada": False}
    predio_key, utilizable = cargado
    con.execute("DELETE FROM fact_oferta_predio WHERE predio_key=? AND vinculo='radio'", (predio_key,))
    con.execute("DELETE FROM fact_entorno WHERE predio_key=?", (predio_key,))
    if not utilizable:
        estado = con_op.execute("SELECT estado FROM capturas WHERE captura_id=?", (captura_id,)).fetchone()[0]
        n = con_op.execute("SELECT (SELECT COUNT(*) FROM entorno_items WHERE captura_id=?) + "
                           "(SELECT COUNT(*) FROM ofertas WHERE captura_id=?)", (captura_id, captura_id)).fetchone()[0]
        motivo = "captura_sin_cerrar" if estado == "en_curso" else f"captura_en_{estado}"
        corrida.rechazo("captura", motivo, captura_id, alcance, 1,
                        f"estado '{estado}': sus {n} registros no alimentan hechos hasta que la captura termine bien")
        corrida.paso("captura", alcance, leidos=1, rechazados=1)
        return {"captura_id": captura_id, "cargada": False, "motivo": motivo}
    corrida.paso("captura", alcance, leidos=1, cargados=1)

    # ---- ofertas de mercado dentro del radio ----
    ofertas = con_op.execute("SELECT * FROM ofertas WHERE captura_id=? ORDER BY oferta_id", (captura_id,)).fetchall()
    filas, puente, sin_base = [], [], 0
    for o in ofertas:
        cfg_base = cfg["bases_mercado"].get(o["fuente"])
        if cfg_base is None:
            sin_base += 1
            continue
        fila = _fila_oferta_captura(dims, o, o["fuente"], cfg_base)
        filas.append(fila)
        puente.append((fila["oferta_nk"], o["distancia_m"]))
    claves = _guardar_ofertas(con, filas, corrida.id)
    con.executemany("INSERT OR REPLACE INTO fact_oferta_predio (predio_key, oferta_key, vinculo, distancia_m) VALUES (?,?,?,?)",
                    [(predio_key, claves[nk], "radio", d) for nk, d in puente])
    if sin_base:
        corrida.rechazo("oferta", "base_de_mercado_no_definida", None, alcance, sin_base,
                        "la fuente de la oferta no está en almacen.yaml › bases_mercado")
    corrida.paso("oferta_en_radio", alcance, leidos=len(ofertas), cargados=len(filas), rechazados=sin_base,
                 detalle="las inválidas se cargan marcadas con su motivo; no se descartan")

    # ---- entorno: lugares depurados + estaciones de transporte masivo ----
    capa_est = cfg["transporte"]["capa_estaciones"]
    n_fuentes = {r[0]: r[1] for r in con_op.execute(
        "SELECT entidad_id, COUNT(*) FROM entorno_items WHERE captura_id=? GROUP BY entidad_id", (captura_id,))}
    lugares, hechos, excluidos, duplicados, cargados = [], [], {}, 0, 0
    estaciones_vistas: set[tuple[str, str]] = set()
    lineas_sin_sistema: set[str] = set()
    for it in con_op.execute("SELECT * FROM entorno_items WHERE captura_id=? ORDER BY item_id", (captura_id,)):
        es_estacion = it["capa"] == capa_est
        if it["motivo_exclusion"]:
            excluidos[(it["capa"], it["motivo_exclusion"])] = excluidos.get((it["capa"], it["motivo_exclusion"]), 0) + 1
            continue
        depurado = bool(it["es_maestro"])
        if depurado:
            cargados += 1
        else:
            duplicados += 1
            if not es_estacion:
                continue                          # duplicado entre fuentes: su lugar ya entra por el registro maestro
        linea = sistema = None
        if es_estacion:
            linea = (bd_op.de_json(it["atributos_json"], {}) or {}).get("linea") or it["categoria_original"]
            sistema = dims.sistema(linea)
            if linea and not sistema:
                lineas_sin_sistema.add(str(linea))
            estaciones_vistas.add((nz.texto_base(it["nombre"]), nz.texto_base(linea)))
        nk = f"{it['fuente']}:{it['capa']}:{it['id_externo']}"
        lugares.append((nk, dims.fuente(it["fuente"]), dims.categoria(it["categoria"]), it["capa"], it["subtipo"],
                        it["nombre"], it["lat"], it["lng"], it["geometria"], sistema, linea, it["item_id"]))
        hechos.append((nk, dims.categoria(it["categoria"]), dims.fuente(it["fuente"]), it["distancia_m"], it["anillo_m"],
                       int(depurado), int(bool(it["en_mapa"])), int(es_estacion), n_fuentes.get(it["entidad_id"], 1)))
    # La estación más cercana del predio puede quedar fuera del radio barrido: entra como referencia.
    pr = con_op.execute("SELECT estacion_masiva, estacion_masiva_linea, estacion_masiva_m FROM predios WHERE captura_id=?",
                        (captura_id,)).fetchone()
    if pr is not None and pr["estacion_masiva"] and \
            (nz.texto_base(pr["estacion_masiva"]), nz.texto_base(pr["estacion_masiva_linea"])) not in estaciones_vistas:
        linea = pr["estacion_masiva_linea"]
        sistema = dims.sistema(linea)
        if linea and not sistema:
            lineas_sin_sistema.add(str(linea))
        nk = f"geomedellin:{capa_est}:ref:{nz.texto_base(pr['estacion_masiva'])}|{nz.texto_base(linea)}"
        cat, fu = dims.categoria("movilidad"), dims.fuente("geomedellin")
        lugares.append((nk, fu, cat, capa_est, sistema, pr["estacion_masiva"], None, None, "punto", sistema, linea, 0))
        hechos.append((nk, cat, fu, pr["estacion_masiva_m"], None, 0, 0, 1, 1))
    con.executemany(
        "INSERT INTO dim_lugar (lugar_nk, fuente_key, categoria_key, capa, subtipo, nombre, lat, lng, geometria, "
        "sistema_transporte, linea, orden_fuente) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(lugar_nk) DO UPDATE SET fuente_key=excluded.fuente_key, categoria_key=excluded.categoria_key, "
        "capa=excluded.capa, subtipo=excluded.subtipo, nombre=excluded.nombre, lat=excluded.lat, lng=excluded.lng, "
        "geometria=excluded.geometria, sistema_transporte=excluded.sistema_transporte, linea=excluded.linea", lugares)
    dims._cache.pop("dim_lugar", None)
    mapa = dims._mapa("dim_lugar", "lugar_nk", "lugar_key")
    con.executemany("INSERT OR REPLACE INTO fact_entorno (predio_key, lugar_key, categoria_key, fuente_key, distancia_m, "
                    "anillo_m, cuenta, en_mapa, es_estacion, n_fuentes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(predio_key, mapa[h[0]], *h[1:]) for h in hechos])
    for (capa, motivo), n in sorted(excluidos.items()):
        corrida.rechazo("entorno", motivo, None, alcance, n, f"capa {capa}: guardado en la base operativa, no cuenta")
    if duplicados:
        corrida.rechazo("entorno", "duplicado_entre_fuentes", None, alcance, duplicados,
                        "enlazado a su registro maestro (regla, puntaje y coincide_con en entorno_items)")
    total = con_op.execute("SELECT COUNT(*) FROM entorno_items WHERE captura_id=?", (captura_id,)).fetchone()[0]
    corrida.paso("entorno", alcance, leidos=total, cargados=cargados, duplicados=duplicados, rechazados=sum(excluidos.values()))
    if lineas_sin_sistema:
        corrida.resumen.setdefault("lineas_sin_sistema", [])
        corrida.resumen["lineas_sin_sistema"] = sorted(set(corrida.resumen["lineas_sin_sistema"]) | lineas_sin_sistema)
    return {"captura_id": captura_id, "cargada": True, "predio_key": predio_key, "ofertas": len(filas), "lugares": cargados}


# =============================================================================================
# Deduplicación de una base de mercado (reglas del motor de ficha)
# =============================================================================================
def deduplicar_base(dims: Dimensiones, base: str) -> dict:
    """Marca duplicados en `fact_oferta` para una base capturada (la muestra oficial se deduplica al cargarla).

    Regla: R1 mismo identificador de aviso; R2 misma combinación `claves_duplicado` de almacen.yaml. Se
    conserva la primera en el orden de la fuente; cada duplicado guarda regla, puntaje y con quién coincide.

    R3 · repetido entre sitios: no se calcula aquí. Lo marca la depuración de anuncios antes del ETL y llega en
    las columnas `*_previa`. Esas filas no compiten como representantes de R1/R2 y quedan como duplicado del
    anuncio que se conservó, con su regla y su puntaje.
    """
    con, cfg_base = dims.con, dims.p["almacen"]["bases_mercado"][base]
    claves = list(cfg_base.get("claves_duplicado") or [])
    df = pd.read_sql_query(
        "SELECT o.oferta_key, o.oferta_nk AS origen, COALESCE(o.id_anuncio, '') AS id_anuncio, o.orden_origen AS fila_origen, "
        "fu.fuente_id AS fuente, es.estado AS estado_canonico, o.area_m2, o.precio, o.zona, o.motivo_invalido, o.completitud, "
        "t.operacion, ti.tipo_inmueble, o.lat, o.lng, o.regla_dup_previa, o.puntaje_dup_previo, o.coincide_con_previo "
        "FROM fact_oferta o JOIN dim_fuente fu ON fu.fuente_key=o.fuente_key "
        "JOIN dim_estado_inmueble es ON es.estado_key=o.estado_key JOIN dim_tipo_oferta t ON t.tipo_oferta_key=o.tipo_oferta_key "
        "JOIN dim_tipo_inmueble ti ON ti.tipo_inmueble_key=o.tipo_inmueble_key "
        "WHERE o.base_mercado=? ORDER BY o.orden_origen, o.oferta_nk", con, params=(base,))
    if df.empty:
        return {"base": base, "ofertas": 0, "duplicados": 0}
    dec = int(cfg_base.get("decimales_punto", 5))
    df["lat_r"] = pd.to_numeric(df["lat"], errors="coerce").round(dec)      # anuncios sin punto: columna vacía
    df["lng_r"] = pd.to_numeric(df["lng"], errors="coerce").round(dec)
    df["motivo_invalido"] = df["motivo_invalido"].astype(object).where(df["motivo_invalido"].notna(), None)
    df["zona"] = df["zona"].fillna(base)
    reglas = {"reglas": {"deduplicacion": {"claves_sin_id": claves}},
              "deduplicacion": {"representante": "primera_fila", "revision_tolerancias": {"activa": False}}}
    # Un repetido entre sitios solo cuenta si el anuncio que se conservó está en la base y no es él mismo un repetido.
    previa = df["regla_dup_previa"].notna() & df["coincide_con_previo"].isin(df.loc[df["regla_dup_previa"].isna(), "origen"])
    previos, df = df[previa], df[~previa]
    out, _, _ = deduplicacion.deduplicar(df, reglas)
    filas = []
    for r in out.itertuples(index=False):
        dup = bool(r.es_duplicado)
        invalido = r.motivo_invalido if isinstance(r.motivo_invalido, str) else None
        filas.append((int(dup), r.grupo_dup, r.id_representante, r.regla_dup or None, r.coincide_con or None,
                      1.0 if dup else None, int(bool(r.grupo_ambiguo)), r.ids_grupo or None,
                      "duplicado" if dup else invalido, int(not dup and invalido is None), int(r.oferta_key)))
    destino = out.set_index("origen")[["grupo_dup", "id_representante"]]
    for r in previos.itertuples(index=False):        # R3: queda en el grupo del anuncio con el que coincide
        grupo, representante = destino.loc[r.coincide_con_previo]
        filas.append((1, grupo, representante, r.regla_dup_previa, r.coincide_con_previo, float(r.puntaje_dup_previo),
                      0, None, "duplicado", 0, int(r.oferta_key)))
    con.executemany("UPDATE fact_oferta SET es_duplicado=?, grupo_dup=?, id_representante=?, regla_dup=?, coincide_con=?, "
                    "puntaje_dup=?, grupo_ambiguo=?, ids_grupo=?, motivo_exclusion=?, es_valida=? WHERE oferta_key=?", filas)
    n_dup = int(out["es_duplicado"].sum()) + len(previos)
    n_inv = int((~out["es_duplicado"] & out["motivo_invalido"].notna()).sum())
    total = len(out) + len(previos)
    dims.corrida.paso(f"oferta_{base}", "", leidos=total, cargados=total - n_dup - n_inv, duplicados=n_dup,
                      rechazados=n_inv, detalle="base completa: válidas + duplicadas + inválidas (todas quedan en fact_oferta)"
                      + (f" · {len(previos)} repetidos entre sitios enlazados antes del ETL" if len(previos) else ""))
    return {"base": base, "ofertas": total, "duplicados": n_dup, "invalidas": n_inv, "entre_sitios": len(previos)}


# =============================================================================================
# Extracción y carga · insumos controlados del reto (Momento 2)
# =============================================================================================
def _leer_geojson(ruta: Path) -> dict:
    return json.loads(Path(ruta).read_text(encoding="utf-8"))


def _huella_reto(dims: Dimensiones) -> tuple[str, dict]:
    r, rel = dims.pf["rutas"], dims.pf["_meta"]["rutas_relativas"]
    archivos = {k: {"ruta": Path(str(rel[v])).as_posix(), "sha256": _sha_archivo(r[v])}
                for k, v in (("anuncios", "anuncios_csv"), ("inmuebles", "inmuebles_csv"), ("transporte", "transporte_geojson"),
                             ("equipamientos", "equipamientos_geojson"), ("pot", "pot_geojson"))}
    firma = json.dumps([archivos, dims.pf["_meta"]["hash_params"], dims.p["huella_almacen"], esquema.VERSION_ESQUEMA],
                       sort_keys=True)
    return hashlib.sha256(firma.encode("utf-8")).hexdigest(), archivos


def cargar_reto(dims: Dimensiones, forzar: bool = False) -> dict:
    """Anuncios de la muestra, inmuebles de prueba y capas de entorno entregadas con el reto."""
    con, corrida, p, pf = dims.con, dims.corrida, dims.p, dims.pf
    r = pf["rutas"]
    if not (r["anuncios_csv"].exists() and r["inmuebles_csv"].exists()):
        corrida.control("insumos_del_reto", "etl_insumo", "alerta", 0, None,
                        f"No están los insumos controlados en {r['anuncios_csv'].parent}: la base {BASE_RETO} queda vacía")
        return {"cargado": False, "motivo": "sin_insumos"}
    firma, archivos = _huella_reto(dims)
    previa = con.execute("SELECT valor FROM etl_estado WHERE clave='huella_reto'").fetchone()
    if previa and previa[0] == firma and not forzar:
        return {"cargado": False, "motivo": "sin_cambios"}
    sin_mapeo = pf["limpieza"]["estado_sin_mapeo"]
    cfg_base = p["almacen"]["bases_mercado"][BASE_RETO]

    # ---- anuncios: limpieza y deduplicación oficiales, fila por fila en el orden del CSV ----
    crudos = carga_ficha.leer_anuncios(r["anuncios_csv"], pf)
    if crudos["origen"].duplicated().any():
        raise ValueError("anuncios: la clave `origen` (archivo:hoja:fila) está repetida; sin ella no hay trazabilidad")
    anuncios, bitacora = limpieza.limpiar_anuncios(crudos, pf)
    anuncios, _, _ = deduplicacion.deduplicar(anuncios, pf)
    con.execute("DELETE FROM fact_oferta_predio WHERE oferta_key IN (SELECT oferta_key FROM fact_oferta WHERE base_mercado=?)",
                (BASE_RETO,))
    vigentes = {f"{BASE_RETO}:{o}" for o in anuncios["origen"]}
    sobrantes = [x[0] for x in con.execute("SELECT oferta_nk FROM fact_oferta WHERE base_mercado=?", (BASE_RETO,))
                 if x[0] not in vigentes]
    con.executemany("DELETE FROM fact_oferta WHERE oferta_nk=?", [(nk,) for nk in sobrantes])
    tipos_fuente = p["almacen"].get("fuentes_muestra") or {}
    filas, extra = [], []
    for a, crudo in zip(anuncios.to_dict("records"), crudos.to_dict("records"), strict=True):
        dup = bool(a["es_duplicado"])
        invalido = a["motivo_invalido"] if isinstance(a["motivo_invalido"], str) else None
        estado = a["estado_canonico"]
        if str(crudo["estado"]).strip() and estado != sin_mapeo and str(crudo["estado"]).strip() != estado:
            corrida.ajuste("oferta", a["origen"], "estado", crudo["estado"], estado, "estado_canonico_oficial")
        fecha = None if pd.isna(a["fecha"]) else a["fecha"]
        tipo_precio = a["tipo_precio"]
        filas.append({
            "oferta_nk": f"{BASE_RETO}:{a['origen']}", "base_mercado": BASE_RETO,
            "fuente_key": dims.fuente(a["fuente"], nombre=str(a["fuente"]).replace("_", " ").capitalize(),
                                      tipo=tipos_fuente.get(a["fuente"], "sin_dato"), origen_datos="reto"),
            "fecha_key": dims.fecha(fecha),
            "ubicacion_key": dims.ubicacion(a["municipio"], crudo["barrio"], entidad="oferta", clave_registro=a["origen"]),
            "tipo_inmueble_key": dims.tipo_inmueble(cfg_base.get("tipo_inmueble")), "estado_key": dims.estado(estado),
            "tipo_oferta_key": dims.tipo_oferta(cfg_base.get("operacion", "venta"), tipo_precio),
            "origen": a["origen"], "id_anuncio": str(a["id_anuncio"]).strip(), "orden_origen": int(a["fila_origen"]),
            "zona": a["zona"], "estado_texto": crudo["estado"], "tipo_precio_texto": tipo_precio, "url": None,
            "area_m2": nz.real(a["area_m2"]), "precio": nz.real(a["precio"]), "precio_m2": nz.real(a["precio_m2"]),
            "habitaciones": nz.real(a["habitaciones"]), "banos": nz.real(a["banos"]), "parqueaderos": nz.real(a["parqueadero"]),
            "estrato": None, "antiguedad": None, "lat": None, "lng": None,
            "completitud": nz.real(a["completitud"]), "motivo_invalido": invalido})
        extra.append((int(dup), a["grupo_dup"], a["id_representante"], a["regla_dup"] or None, a["coincide_con"] or None,
                      1.0 if dup else None, int(bool(a["grupo_ambiguo"])), a["ids_grupo"] or None,
                      "duplicado" if dup else invalido, int(not dup and invalido is None), f"{BASE_RETO}:{a['origen']}"))
    _guardar_ofertas(con, filas, corrida.id)
    con.executemany("UPDATE fact_oferta SET es_duplicado=?, grupo_dup=?, id_representante=?, regla_dup=?, coincide_con=?, "
                    "puntaje_dup=?, grupo_ambiguo=?, ids_grupo=?, motivo_exclusion=?, es_valida=? WHERE oferta_nk=?", extra)
    for b in bitacora.to_dict("records"):
        corrida.ajuste("oferta", b["origen"], b["campo"], b["detalle"] or None, None, b["evento"])
    n_dup = int(anuncios["es_duplicado"].sum())
    n_inv = int((~anuncios["es_duplicado"] & anuncios["motivo_invalido"].notna()).sum())
    corrida.paso(f"oferta_{BASE_RETO}", "", leidos=len(anuncios), cargados=len(anuncios) - n_dup - n_inv, duplicados=n_dup,
                 rechazados=n_inv, detalle="regla oficial: R1 id_anuncio, R2 zona+área+precio, primera fila")
    corrida.resumen["muestra"] = {"anuncios": len(anuncios), "duplicados": n_dup, "invalidos": n_inv,
                                  "validos": len(anuncios) - n_dup - n_inv}

    # ---- inmuebles de prueba: predio (punto) e inmueble (unidad) ----
    inmuebles = carga_ficha.leer_inmuebles(r["inmuebles_csv"], pf)
    caja = pf["crs"]["bbox_area_estudio"]
    predios: dict[str, tuple[int, float, float]] = {}
    rechazados = 0
    for inm in inmuebles.to_dict("records"):
        lat, lon = _coordenada(inm["lat"]), _coordenada(inm["lon"])     # float directo: 6.242 no es "6 mil 242"
        area = limpieza.a_positivo(inm["area_m2"])
        fallas = []
        if not (lat == lat and caja["lat_min"] <= lat <= caja["lat_max"]):
            fallas.append(f"lat fuera del área de estudio ({inm['lat']})")
        if not (lon == lon and caja["lon_min"] <= lon <= caja["lon_max"]):
            fallas.append(f"lon fuera del área de estudio ({inm['lon']})")
        if area != area:
            fallas.append(f"área no válida ({inm['area_m2']})")
        if not str(inm["id_inmueble"]).strip() or not str(inm["zona"]).strip():
            fallas.append("sin id_inmueble o sin zona")
        if fallas:
            rechazados += 1
            corrida.rechazo("inmueble_reto", "validacion_esquema", inm["id_inmueble"], "reto", 1, " | ".join(fallas))
            continue
        nk = f"reto:{inm['id_inmueble']}"
        estado = limpieza.normalizar_estado(inm["estado"], pf["reglas"]["estados"], sin_mapeo)
        ubic = dims.ubicacion(inm["municipio"], inm["barrio"], entidad="inmueble_reto", clave_registro=inm["id_inmueble"])
        x9, y9 = geo.a_9377(lon, lat)
        predio_key = dims._upsert("dim_predio", "predio_nk", "predio_key", nk, {
            "origen": "reto", "direccion": str(inm["direccion"]).strip() or None, "ubicacion_key": ubic,
            "municipio": inm["municipio"], "barrio_texto": inm["barrio"], "lat": lat, "lng": lon, "x_9377": x9, "y_9377": y9,
            "precision_coordenadas": inm["precision_coordenadas"], "fuente_coordenadas": inm["fuente_coordenadas"],
            "radio_m": int(p["reglas"]["captura"]["radio_m"]), "utilizable": 1, "estado_captura": "insumo_controlado"})
        dims._upsert("dim_inmueble", "inmueble_nk", "inmueble_key", nk, {
            "origen": "reto", "predio_key": predio_key, "id_inmueble": inm["id_inmueble"],
            "id_anuncio": str(inm["id_anuncio"]).strip(), "origen_anuncio": inm["origen"],
            "tipo_inmueble_key": dims.tipo_inmueble(cfg_base.get("tipo_inmueble")), "estado_key": dims.estado(estado),
            "estado_texto": inm["estado"], "area_m2": area, "zona_mercado": inm["zona"], "datos_completos": 1})
        predios[nk] = (predio_key, lat, lon)
    corrida.paso("inmueble_reto", "", leidos=len(inmuebles), cargados=len(predios), rechazados=rechazados)

    # ---- capas de entorno del reto: distancias con el método oficial (haversine) ----
    radio_tierra = pf["entorno"]["radio_tierra_m"]
    radio_carga = max(int(p["reglas"]["captura"]["radio_m"]), int(pf["reglas"]["entorno"]["radio_busqueda_m"]))
    fu = dims.fuente(FUENTE_CAPAS_RETO)
    mov = dims.categoria("movilidad")
    metadatos = {}
    for predio_key, _, _ in predios.values():
        con.execute("DELETE FROM fact_entorno WHERE predio_key=?", (predio_key,))
    for capa, clave_ruta, es_estacion in (("transporte", "transporte_geojson", True), ("equipamientos", "equipamientos_geojson", False)):
        if not r[clave_ruta].exists():
            corrida.control(f"capa_{capa}_del_reto", "etl_insumo", "alerta", 0, None, f"Falta {r[clave_ruta].name}")
            continue
        geojson = _leer_geojson(r[clave_ruta])
        metadatos[capa] = geojson.get("metadata") or {}
        if geojson.get("crs"):
            raise ValueError(f"{r[clave_ruta].name} declara un CRS propio ({geojson['crs']}): verifíquelo antes de medir")
        lugares, puntos, sin_punto = [], [], 0
        for orden, f in enumerate(geojson.get("features") or []):
            g, props = f.get("geometry") or {}, f.get("properties") or {}
            if g.get("type") != "Point":
                sin_punto += 1
                continue
            lon, lat = float(g["coordinates"][0]), float(g["coordinates"][1])
            if not (caja["lat_min"] <= lat <= caja["lat_max"] and caja["lon_min"] <= lon <= caja["lon_max"]):
                sin_punto += 1
                continue
            nk = f"{FUENTE_CAPAS_RETO}:{capa}:{props.get('osm_id') or orden}"
            cat = mov if es_estacion else dims.categoria(props.get(pf["entorno"]["campo_categoria"]))
            sistema = props.get(pf["entorno"]["campo_sistema"]) if es_estacion else None
            lugares.append((nk, fu, cat, capa, sistema if es_estacion else None, props.get(pf["entorno"]["campo_nombre"]),
                            lat, lon, "punto", sistema, None, orden))
            puntos.append((nk, cat, lat, lon))
        con.executemany(
            "INSERT INTO dim_lugar (lugar_nk, fuente_key, categoria_key, capa, subtipo, nombre, lat, lng, geometria, "
            "sistema_transporte, linea, orden_fuente) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(lugar_nk) DO UPDATE SET categoria_key=excluded.categoria_key, subtipo=excluded.subtipo, "
            "nombre=excluded.nombre, lat=excluded.lat, lng=excluded.lng, sistema_transporte=excluded.sistema_transporte, "
            "orden_fuente=excluded.orden_fuente", lugares)
        dims._cache.pop("dim_lugar", None)
        mapa = dims._mapa("dim_lugar", "lugar_nk", "lugar_key")
        hechos = []
        for predio_key, plat, plon in predios.values():
            for nk, cat, lat, lon in puntos:
                d = _haversine(plat, plon, lat, lon, radio_tierra)
                if es_estacion or d <= radio_carga:            # las estaciones entran todas: la más cercana puede estar lejos
                    hechos.append((predio_key, mapa[nk], cat, fu, d, geo.anillo_de(d, p["reglas"]["captura"]["anillos_m"]),
                                   1, 1, int(es_estacion), 1))
        con.executemany("INSERT OR REPLACE INTO fact_entorno (predio_key, lugar_key, categoria_key, fuente_key, distancia_m, "
                        "anillo_m, cuenta, en_mapa, es_estacion, n_fuentes) VALUES (?,?,?,?,?,?,?,?,?,?)", hechos)
        if sin_punto:
            corrida.rechazo(f"capa_{capa}", "sin_punto_en_el_area", None, "reto", sin_punto)
        corrida.paso(f"capa_{capa}", "reto", leidos=len(geojson.get("features") or []), cargados=len(lugares),
                     rechazados=sin_punto)

    t = ahora()
    for nombre, a in archivos.items():
        con.execute("INSERT OR REPLACE INTO etl_insumo VALUES (?,?,?,?,?,?,?)",
                    (f"reto:{nombre}", a["ruta"], a["sha256"], None,
                     json.dumps(metadatos.get(nombre) or {}, ensure_ascii=False), corrida.id, t))
    con.execute("INSERT OR REPLACE INTO etl_estado VALUES ('huella_reto', ?)", (firma,))
    return {"cargado": True, "anuncios": len(anuncios), "inmuebles": len(predios)}


def _coordenada(texto) -> float:
    try:
        return float(str(texto).strip())
    except ValueError:
        return float("nan")


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float, radio_m: float) -> float:
    """Método oficial del cálculo manual, con el radio terrestre de ficha.yaml."""
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radio_m * math.asin(math.sqrt(a))


def vincular_zonas(dims: Dimensiones) -> int:
    """Puente predio ↔ ofertas de la muestra por zona de mercado (el barrio del predio pertenece a la zona)."""
    con = dims.con
    con.execute("DELETE FROM fact_oferta_predio WHERE vinculo='zona'")
    return con.execute(
        "INSERT OR IGNORE INTO fact_oferta_predio (predio_key, oferta_key, vinculo, distancia_m) "
        "SELECT pr.predio_key, o.oferta_key, 'zona', NULL FROM dim_predio pr "
        "JOIN dim_ubicacion u ON u.ubicacion_key = pr.ubicacion_key "
        "LEFT JOIN dim_inmueble i ON i.predio_key = pr.predio_key AND i.origen = 'reto' "
        "JOIN fact_oferta o ON o.base_mercado = ? AND o.zona = COALESCE(i.zona_mercado, u.zona_mercado) "
        "WHERE pr.predio_key > 0 AND pr.utilizable = 1", (BASE_RETO,)).rowcount


# =============================================================================================
# Extracción y carga · gestión comercial (comerciales, clientes, cotizaciones, fichas, metas)
# =============================================================================================
def _firma_gestion(con_op: sqlite3.Connection) -> str:
    partes = []
    for tabla, marca in (("comerciales", "COALESCE(MAX(clave_cambiada_en), '') || COUNT(*) || SUM(activo)"),
                         ("clientes", "COALESCE(MAX(actualizado_en), '') || COUNT(*)"),
                         ("cotizaciones", "COALESCE(MAX(terminada_en), '') || COALESCE(MAX(creada_en), '') || COUNT(*) || COALESCE(SUM(inm_area_m2), 0) "
                                          "|| COALESCE(GROUP_CONCAT(COALESCE(inm_tipo, '') || COALESCE(inm_estado, '') || estado), '')"),
                         ("cotizacion_intereses", "COALESCE(MAX(registrado_en), '') || COUNT(*)"),
                         ("fichas", "COALESCE(MAX(generada_en), '') || COUNT(*)"),
                         ("metas", "COALESCE(MAX(actualizada_en), '') || COUNT(*)")):
        partes.append(str(con_op.execute(f"SELECT {marca} FROM {tabla}").fetchone()[0]))
    return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()


def cargar_gestion(dims: Dimensiones, con_op: sqlite3.Connection) -> dict:
    con, corrida, p = dims.con, dims.corrida, dims.p
    visibles = p["reglas"]["clientes"]["digitos_visibles"]
    cuentan = set(p["reglas"]["cotizaciones"]["estados_que_cuentan"])
    obligatorios = p["reglas"]["inmueble"]["obligatorios"]
    etiquetas = p["reglas"]["inmueble"]["etiquetas_estado"]

    comerciales = con_op.execute("SELECT usuario, nombre, rol, activo FROM comerciales").fetchall()
    for c in comerciales:
        dims._upsert("dim_comercial", "usuario", "comercial_key", c["usuario"],
                     {"nombre": c["nombre"], "rol": c["rol"], "activo": c["activo"]})
    corrida.paso("comercial", "", leidos=len(comerciales), cargados=len(comerciales))

    clientes = con_op.execute("SELECT * FROM clientes").fetchall()
    for c in clientes:
        fecha_key, _ = dims.fecha_local(c["creado_en"])
        dims._upsert("dim_cliente", "cliente_id", "cliente_key", c["cliente_id"], {
            "tipo_documento": c["tipo_documento"], "documento_mascara": nz.enmascarar(c["documento"], visibles),
            "documento_sha256": nz.huella(c["documento"]), "nombre": c["nombre"], "autoriza_datos": c["autoriza_datos"],
            "creado_por": c["creado_por"], "fecha_alta_key": fecha_key})
    corrida.paso("cliente", "", leidos=len(clientes), cargados=len(clientes))

    con_ficha = {r[0] for r in con_op.execute("SELECT cotizacion_id FROM fichas WHERE vigente=1 AND estado IN ('ok','sin_mercado')")}
    cotizaciones = con_op.execute("SELECT * FROM cotizaciones ORDER BY creada_en").fetchall()
    sin_predio = 0
    for q in cotizaciones:
        predio_key, ubic, zona = 0, 0, None
        if q["captura_id"]:
            predio_key = dims._mapa("dim_predio", "predio_nk", "predio_key").get(f"captura:{q['captura_id']}")
            if predio_key is None:
                cargado = cargar_predio_captura(dims, con_op, q["captura_id"])
                predio_key = cargado[0] if cargado else 0
            fila = con.execute("SELECT pr.ubicacion_key, u.zona_mercado FROM dim_predio pr JOIN dim_ubicacion u "
                               "ON u.ubicacion_key=pr.ubicacion_key WHERE pr.predio_key=?", (predio_key,)).fetchone()
            ubic, zona = (fila[0], fila[1]) if fila else (0, None)
        if not predio_key:
            sin_predio += 1
        datos = {"tipo": q["inm_tipo"], "area_m2": q["inm_area_m2"], "estado": q["inm_estado"]}
        inmueble_key = dims._upsert("dim_inmueble", "inmueble_nk", "inmueble_key", f"cot:{q['cotizacion_id']}", {
            "origen": "cotizacion", "predio_key": predio_key, "tipo_inmueble_key": dims.tipo_inmueble(q["inm_tipo"]),
            "estado_key": dims.estado(q["inm_estado"]), "estado_texto": etiquetas.get(q["inm_estado"], q["inm_estado"]),
            "area_m2": q["inm_area_m2"], "habitaciones": q["inm_habitaciones"], "banos": q["inm_banos"],
            "parqueaderos": q["inm_parqueaderos"], "zona_mercado": zona, "caracteristicas_json": q["inm_extras_json"],
            "datos_completos": int(all(datos.get(k) not in (None, "") for k in obligatorios))})
        fecha_key, hora = dims.fecha_local(q["creada_en"])
        ev = bd_op.de_json(q["evidencia_json"], {}) or {}
        duracion = None
        if q["terminada_en"]:
            try:
                duracion = (datetime.fromisoformat(q["terminada_en"]) - datetime.fromisoformat(q["creada_en"])).total_seconds()
            except ValueError:
                duracion = None
        dims._upsert("fact_cotizacion", "cotizacion_id", "cotizacion_key", q["cotizacion_id"], {
            "fecha_key": fecha_key, "comercial_key": dims._mapa("dim_comercial", "usuario", "comercial_key").get(q["comercial"], 0),
            "cliente_key": dims._mapa("dim_cliente", "cliente_id", "cliente_key").get(q["cliente_id"], 0),
            "inmueble_key": inmueble_key, "predio_key": predio_key, "ubicacion_key": ubic,
            "estado": q["estado"], "hora": hora, "creada_en": q["creada_en"], "cuenta": int(q["estado"] in cuentan),
            "reusada": q["reusada"], "duracion_s": duracion,
            "lugares": sum((ev.get("lugares") or {}).values()) if ev.get("lugares") else None,
            "ofertas_radio": ev.get("ofertas"), "tiene_ficha": int(q["cotizacion_id"] in con_ficha)})
    corrida.paso("cotizacion", "", leidos=len(cotizaciones), cargados=len(cotizaciones),
                 detalle=f"{sin_predio} sin predio (captura fallida o en curso): quedan con el predio 'sin dato'")

    # ---- lo que busca el cliente: una fila por tipo de inmueble pedido en cada cotización ----
    operaciones = sorted({c.get("operacion_comparable") or c.get("operacion") for c in p["almacen"]["bases_mercado"].values()} - {None})
    hechos_cot = {r["cotizacion_id"]: r for r in con.execute(
        "SELECT cotizacion_id, fecha_key, comercial_key, cliente_key, predio_key, ubicacion_key, cuenta FROM fact_cotizacion")}
    intereses = con_op.execute(
        "SELECT i.cotizacion_id, i.tipo_inmueble, i.orden, i.origen, q.inm_tipo FROM cotizacion_intereses i "
        "JOIN cotizaciones q ON q.cotizacion_id = i.cotizacion_id ORDER BY q.creada_en, i.orden, i.tipo_inmueble").fetchall()
    por_cotizacion: dict[str, int] = {}
    for i in intereses:
        por_cotizacion[i["cotizacion_id"]] = por_cotizacion.get(i["cotizacion_id"], 0) + 1
    oferta_predio: dict[int, dict[int, int]] = {}

    def _oferta_disponible(predio_key: int) -> dict[int, int]:
        if predio_key not in oferta_predio:
            oferta_predio[predio_key] = dict(con.execute(
                "SELECT o.tipo_inmueble_key, COUNT(*) FROM fact_oferta_predio op JOIN fact_oferta o ON o.oferta_key = op.oferta_key "
                "JOIN dim_tipo_oferta t ON t.tipo_oferta_key = o.tipo_oferta_key WHERE op.predio_key = ? AND o.es_valida = 1 "
                f"AND t.operacion IN ({', '.join('?' for _ in operaciones)}) GROUP BY 1", (predio_key, *operaciones)).fetchall())
        return oferta_predio[predio_key]

    deducidos = 0
    for i in intereses:
        cot = hechos_cot.get(i["cotizacion_id"])
        if cot is None:
            corrida.rechazo("interes", "interes_sin_cotizacion", f"{i['cotizacion_id']}:{i['tipo_inmueble']}")
            continue
        tipo_key = dims.tipo_inmueble(i["tipo_inmueble"])
        n_tipos = por_cotizacion[i["cotizacion_id"]]
        deducidos += int(i["origen"] != "declarado")
        dims._upsert("fact_interes", "interes_nk", "interes_key", f"{i['cotizacion_id']}:{i['tipo_inmueble']}", {
            "cotizacion_id": i["cotizacion_id"], "fecha_key": cot["fecha_key"], "comercial_key": cot["comercial_key"],
            "cliente_key": cot["cliente_key"], "tipo_inmueble_key": tipo_key, "predio_key": cot["predio_key"],
            "ubicacion_key": cot["ubicacion_key"], "origen": i["origen"], "orden": i["orden"],
            "es_cotizado": int(i["tipo_inmueble"] == i["inm_tipo"]), "cuenta": cot["cuenta"], "n_tipos": n_tipos,
            "peso": 1 / n_tipos,
            "ofertas_radio": _oferta_disponible(cot["predio_key"]).get(tipo_key, 0) if cot["predio_key"] else None})
    corrida.paso("interes", "", leidos=len(intereses), cargados=len(intereses),
                 detalle=f"{len(por_cotizacion)} cotizaciones con búsqueda registrada; {deducidos} tipos deducidos del inmueble cotizado")

    fichas = con_op.execute("SELECT * FROM fichas ORDER BY generada_en").fetchall()
    for f in fichas:
        cot = con.execute("SELECT comercial_key, cliente_key, inmueble_key, predio_key FROM fact_cotizacion WHERE cotizacion_id=?",
                          (f["cotizacion_id"],)).fetchone()
        if cot is None:
            corrida.rechazo("ficha", "ficha_sin_cotizacion", f["ficha_id"])
            continue
        res = bd_op.de_json(f["resumen_json"], {}) or {}
        fecha_key, _ = dims.fecha_local(f["generada_en"])
        dims._upsert("fact_ficha", "ficha_id", "ficha_key", f["ficha_id"], {
            "cotizacion_id": f["cotizacion_id"], "version": f["version"], "vigente": f["vigente"], "fecha_key": fecha_key,
            "comercial_key": cot["comercial_key"], "cliente_key": cot["cliente_key"], "inmueble_key": cot["inmueble_key"],
            "predio_key": cot["predio_key"], "estado_key": dims.estado(res.get("estado_referencia")),
            "base_mercado": res.get("base_mercado"), "tipo_precio_base": res.get("tipo_precio_base"),
            "n_comparables": res.get("n"), "mediana_m2": res.get("mediana"), "promedio_m2": res.get("promedio"),
            "minimo_m2": res.get("minimo"), "maximo_m2": res.get("maximo"), "desviacion_m2": res.get("desviacion"),
            "coef_variacion": res.get("coef_variacion"), "confianza": res.get("confianza"),
            "estaciones_en_radio": res.get("estaciones_en_radio"), "equipamientos_en_radio": res.get("equipamientos_en_radio"),
            "dist_estacion_m": res.get("dist_estacion_m"), "norma_estado": res.get("norma_estado"),
            "n_datos_faltantes": res.get("n_datos_faltantes"), "flexibilizada": int(bool(res.get("flexibilizada"))),
            # Solo el nombre del archivo: la carpeta es el documento del cliente y no sale de la base operativa.
            "ruta_md": Path(f["ruta_md"]).name if f["ruta_md"] else None, "sha256_md": f["sha256_md"],
            "corrida_id": f["corrida_etl"]})
    corrida.paso("ficha", "", leidos=len(fichas), cargados=len(fichas))

    con.execute("DELETE FROM fact_meta")
    metas = con_op.execute("SELECT * FROM metas").fetchall()
    usuarios = dims._mapa("dim_comercial", "usuario", "comercial_key")
    for m in metas:
        anio, mes = (int(x) for x in m["anio_mes"].split("-"))
        con.execute("INSERT INTO fact_meta VALUES (?,?,?,?,?,?,?)",
                    (usuarios.get(m["comercial"], 0), m["anio_mes"], dims.fecha(date(anio, mes, 1)), m["meta_cotizaciones"],
                     m["meta_clientes"], m["definida_por"], m["actualizada_en"]))
    corrida.paso("meta", "", leidos=len(metas), cargados=len(metas))
    con.execute("INSERT OR REPLACE INTO etl_estado VALUES ('firma_gestion', ?)", (_firma_gestion(con_op),))
    return {"comerciales": len(comerciales), "clientes": len(clientes), "cotizaciones": len(cotizaciones),
            "intereses": len(intereses), "fichas": len(fichas), "metas": len(metas)}


# =============================================================================================
# Orquestación
# =============================================================================================
def ejecutar(p: dict, modo: str = "completo", capturas: list[str] | None = None, forzar_reto: bool = False) -> dict:
    """Corre el ETL. modo: completo | captura (las de `capturas`) | gestion | reto."""
    if modo not in ("completo", "captura", "gestion", "reto"):
        raise ValueError(f"modo de ETL no reconocido: {modo}")
    pf = params_ficha(p)
    with CANDADO:
        con = esquema.conectar(p["rutas"]["almacen"])
        con_op = bd_op.conectar(p["rutas"]["bd"])
        try:
            esquema.crear(con)
            con.commit()
            if modo == "completo":
                capturas = [r[0] for r in con_op.execute("SELECT captura_id FROM capturas ORDER BY creada_en")]
            capturas = list(capturas or [])
            corrida = Corrida(con, modo, ", ".join(c[:8] for c in capturas) or modo, p, pf)
            dims = Dimensiones(con, p, pf, corrida)
            cargar_catalogos(dims)
            if modo in ("completo", "reto", "captura"):
                corrida.resumen["reto"] = cargar_reto(dims, forzar=forzar_reto or modo == "reto")
            corrida.resumen["capturas"] = [cargar_captura(dims, con_op, c) for c in capturas]
            if modo in ("completo", "captura"):
                bases = [b for b, cfg in p["almacen"]["bases_mercado"].items() if cfg.get("vinculo") == "radio"]
                corrida.resumen["deduplicacion"] = [deduplicar_base(dims, b) for b in bases]
            corrida.resumen["gestion"] = cargar_gestion(dims, con_op)
            corrida.resumen["vinculos_zona"] = vincular_zonas(dims)
            calidad.revisar(con, corrida, p, pf)
            resultado = corrida.cerrar()
            con.commit()
            return resultado
        except Exception as e:
            con.rollback()
            con.execute("INSERT INTO etl_corrida (inicio, fin, modo, alcance, estado, version_app, version_esquema, mensaje) "
                        "VALUES (?,?,?,?,?,?,?,?)", (ahora(), ahora(), modo, "", "error", VERSION_APP, esquema.VERSION_ESQUEMA,
                                                     f"{type(e).__name__}: {str(e)[:400]}"))
            con.commit()
            raise
        finally:
            con_op.close()
            con.close()


def refrescar_gestion(p: dict) -> dict | None:
    """Corre el ETL de gestión solo si la base operativa cambió desde la última carga (lo usa el tablero)."""
    ruta = Path(p["rutas"]["almacen"])
    if ruta.exists():
        con = esquema.conectar(ruta)
        con_op = bd_op.conectar(p["rutas"]["bd"])
        try:
            previa = con.execute("SELECT valor FROM etl_estado WHERE clave='firma_gestion'").fetchone()
            if previa and previa[0] == _firma_gestion(con_op):
                return None
        except sqlite3.OperationalError:
            pass
        finally:
            con_op.close()
            con.close()
    return ejecutar(p, "gestion")


def main(argv: list[str] | None = None) -> int:
    from captura import config
    ap = argparse.ArgumentParser(description="Venture One · ETL del almacén (esquema estrella)")
    ap.add_argument("--captura", action="append", help="captura_id a cargar (se puede repetir)")
    ap.add_argument("--gestion", action="store_true", help="solo comerciales, clientes, cotizaciones, fichas y metas")
    ap.add_argument("--reto", action="store_true", help="recarga los insumos controlados del reto")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    p = config.cargar()
    bd_op.iniciar(p)
    modo = "captura" if args.captura else "gestion" if args.gestion else "reto" if args.reto else "completo"
    res = ejecutar(p, modo, capturas=args.captura, forzar_reto=args.reto)
    print(f"ETL {modo} · corrida {res['corrida_id']} · estado: {res['estado']}")
    print(f"  Almacén: {p['rutas']['almacen']}")
    for nombre, f in res["pasos"].items():
        print(f"  {nombre:<28} leídos {f['leidos']:>7} = cargados {f['cargados']:>7} + duplicados {f['duplicados']:>6} "
              f"+ rechazados {f['rechazados']:>6}")
    print(f"  Controles de calidad: {res['controles']}")
    return 0 if res["estado"] in ("ok", "con_alertas") else 1


if __name__ == "__main__":
    raise SystemExit(main())
