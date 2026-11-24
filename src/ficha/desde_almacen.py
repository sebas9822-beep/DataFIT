"""Motor de ficha sobre el almacén: lee el esquema estrella y arma la ficha con las reglas oficiales.

Las etapas de ingesta, limpieza y deduplicación ya ocurrieron en el ETL (con las mismas funciones del
motor). Aquí se seleccionan comparables, se calculan estadísticas y confianza, se leen entorno y norma
y se ensambla la ficha con el esquema oficial más la trazabilidad del almacén.

Bases de mercado (almacen.yaml › bases_mercado): la ficha calcula cada base aplicable por separado y
nunca las mezcla. La principal es la primera de `preferencia` que alcanza `minimo_para_preferir`
comparables; las demás quedan reportadas aparte.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import pandas as pd

from . import __version__, entorno_almacen
from .ficha import construir_ficha

RESUMEN_BASE = ("n", "mediana", "promedio", "minimo", "maximo", "desviacion", "coef_variacion")


# ---------------------------------------------------------------------------------------------
# Lectura del almacén
# ---------------------------------------------------------------------------------------------
def leer_inmueble(con: sqlite3.Connection, inmueble_nk: str) -> dict | None:
    """Inmueble (unidad) con su predio (punto) y su ubicación conformada."""
    fila = con.execute(
        "SELECT i.inmueble_key, i.inmueble_nk, i.origen AS origen_inmueble, i.id_inmueble, i.id_anuncio, i.origen_anuncio, "
        "i.estado_texto, i.area_m2, i.habitaciones, i.banos, i.parqueaderos, i.zona_mercado, i.datos_completos, "
        "i.caracteristicas_json, "
        "ti.tipo_inmueble, es.estado AS estado_canonico, "
        "pr.predio_key, pr.predio_nk, pr.origen AS origen_predio, pr.captura_id, pr.direccion, pr.cbml, pr.conjunto, "
        "pr.municipio, pr.barrio_texto, pr.lat, pr.lng, pr.precision_coordenadas, pr.fuente_coordenadas, pr.radio_m, "
        "pr.en_cobertura, pr.estado_captura, pr.utilizable, pr.pot_tratamiento, pr.pot_codigo, pr.pot_categoria, "
        "pr.pot_clase_suelo, pr.pot_uso_general, pr.estado_norma, pr.zge_valor_m2, pr.es_ph, pr.area_lote, pr.pisos_max, "
        "pr.estacion_masiva, pr.estacion_masiva_linea, pr.estacion_masiva_m, pr.control_9377_max_dif_m, "
        "pr.huella_config, pr.version_app, "
        "u.barrio, u.comuna, u.municipio AS municipio_nombre, u.zona_mercado AS zona_ubicacion "
        "FROM dim_inmueble i JOIN dim_predio pr ON pr.predio_key = i.predio_key "
        "JOIN dim_ubicacion u ON u.ubicacion_key = pr.ubicacion_key "
        "JOIN dim_tipo_inmueble ti ON ti.tipo_inmueble_key = i.tipo_inmueble_key "
        "JOIN dim_estado_inmueble es ON es.estado_key = i.estado_key WHERE i.inmueble_nk = ?", (inmueble_nk,)).fetchone()
    return dict(fila) if fila else None


REGLA_ENTRE_SITIOS = "R3_entre_sitios"      # la pone la depuración de anuncios (captura.curaduria.anuncios)


def marco_ofertas(con: sqlite3.Connection, base: str, cfg_base: dict, predio_key: int | None = None,
                  zona: str | None = None) -> pd.DataFrame:
    """Ofertas de una base con las columnas y los tipos que espera el motor (mercado, calidad_zona)."""
    if cfg_base.get("vinculo") == "radio":
        operacion = cfg_base.get("operacion_comparable")
        sql = ("SELECT v.*, x.distancia_m FROM v_oferta v JOIN fact_oferta_predio x ON x.oferta_key = v.oferta_key "
               "WHERE v.base_mercado = ? AND x.predio_key = ? AND x.vinculo = 'radio'"
               + (" AND v.operacion = ?" if operacion else "") + " ORDER BY v.orden_origen, v.oferta_nk")
        args = [base, predio_key] + ([operacion] if operacion else [])
    else:
        sql = "SELECT v.*, NULL AS distancia_m FROM v_oferta v WHERE v.base_mercado = ? ORDER BY v.orden_origen, v.oferta_nk"
        args = [base]
    df = pd.read_sql_query(sql, con, params=args)
    df["es_duplicado"] = df["es_duplicado"].fillna(0).astype(int).astype(bool)
    df["grupo_ambiguo"] = df["grupo_ambiguo"].fillna(0).astype(int).astype(bool)
    df["id_anuncio"] = df["id_anuncio"].fillna("").astype(str)
    df["motivo_invalido"] = df["motivo_invalido"].astype(object).where(df["motivo_invalido"].notna(), None)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["tipo_precio"] = df["tipo_precio_texto"].where(df["tipo_precio_texto"].notna(), df["tipo_precio"])
    df["fila_origen"] = df["orden_origen"]
    if zona is not None:
        df["zona"] = zona
    return df


def aplicar_vigencia(marco: pd.DataFrame, cfg_v: dict, fecha_ref: str) -> tuple[pd.DataFrame, dict]:
    """Deja fuera de los comparables lo publicado antes del plazo (almacen.yaml › vigencia).

    No borra filas: les pone `motivo_invalido`, igual que cualquier otra exclusión, y la ficha las cuenta.
    Un anuncio sin fecha de publicación vale por el día en que se vio publicado, si la regla lo permite.
    """
    corte = pd.Timestamp(fecha_ref) - pd.DateOffset(months=int(cfg_v["max_meses_publicacion"]))
    m = marco.copy()
    publicada = m["fecha"]
    vista = pd.to_datetime(m["observada_en"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    if cfg_v["sin_fecha_publicacion"] != "fecha_observacion":
        vista = pd.Series(pd.NaT, index=m.index)
    referencia = publicada.where(publicada.notna(), vista)
    validas = m["motivo_invalido"].isna()
    vieja = validas & referencia.notna() & (referencia < corte)
    sin_fecha = validas & referencia.isna()
    m.loc[vieja, "motivo_invalido"] = "publicacion_fuera_de_vigencia"
    m.loc[sin_fecha, "motivo_invalido"] = "sin_fecha_de_publicacion"
    m["fecha_observacion"] = vista
    return m, {"fecha_referencia": str(fecha_ref), "corte": corte.date().isoformat(),
               "max_meses_publicacion": int(cfg_v["max_meses_publicacion"]), "regla_sin_fecha": cfg_v["sin_fecha_publicacion"],
               "fuera_de_vigencia": int(vieja.sum()), "sin_fecha": int(sin_fecha.sum()),
               "vigentes_por_observacion": int((validas & publicada.isna() & referencia.notna() & ~vieja).sum())}


def _panorama(tipos: list[dict], marcos: dict[str, pd.DataFrame], historicos: dict[str, pd.DataFrame], cfg: dict) -> list[dict]:
    """Por cada tipo que pidió el cliente: cuántas ofertas válidas hay a la vista del predio, vigentes e históricas."""
    def cuenta(tablas: dict[str, pd.DataFrame], tipo: str) -> dict:
        partes = []
        for base, df in tablas.items():
            operacion = cfg["bases_mercado"][base].get("operacion_comparable")
            sel = df[df["motivo_invalido"].isna() & ~df["es_duplicado"] & (df["tipo_inmueble"] == tipo)]
            sel = sel[sel["operacion"] == operacion] if operacion else sel
            if not sel.empty:
                partes.append(sel[["precio_m2", "area_m2"]])
        junto = pd.concat(partes) if partes else pd.DataFrame(columns=["precio_m2", "area_m2"])
        return {"n": int(len(junto)), "mediana_m2": None if junto.empty else round(float(junto["precio_m2"].median())),
                "area_mediana_m2": None if junto.empty else round(float(junto["area_m2"].median()), 1)}

    por_radio = {b: df for b, df in marcos.items() if cfg["bases_mercado"][b]["vinculo"] == "radio"}
    antes = {b: historicos.get(b, df) for b, df in por_radio.items()}
    salida = []
    for t in tipos:
        vigentes, todas = cuenta(por_radio, t["tipo_inmueble"]), cuenta(antes, t["tipo_inmueble"])
        salida.append({"tipo": t["tipo_inmueble"], "es_cotizado": bool(t["es_cotizado"]), "origen": t["origen"],
                       "vigentes": vigentes, "historicas": {**todas, "n": todas["n"] - vigentes["n"]}})
    return salida


def _serie_inmueble(inm: dict, zona: str | None) -> pd.Series:
    """El inmueble con los nombres de columna del motor. En los de la muestra, tal como venían en el CSV."""
    de_reto = inm["origen_inmueble"] == "reto"
    return pd.Series({
        "id_inmueble": inm["id_inmueble"] if de_reto else "COT-" + inm["inmueble_nk"].split(":", 1)[1][:8].upper(),
        "id_anuncio": inm["id_anuncio"] or "", "origen": inm["origen_anuncio"] or "",
        "direccion": inm["direccion"] or "",
        "barrio": inm["barrio_texto"] if de_reto else (inm["barrio"] if inm["barrio"] != "Sin barrio" else None),
        "municipio": inm["municipio"] if de_reto else (inm["municipio_nombre"] if inm["municipio_nombre"] != "sin dato" else inm["municipio"]),
        "zona": zona, "area_m2": inm["area_m2"] if inm["area_m2"] is not None else 0.0,
        "estado": inm["estado_texto"] or "", "estado_canonico": inm["estado_canonico"],
        "tipo_inmueble": inm["tipo_inmueble"], "operacion": "venta",
        "lat": inm["lat"], "lon": inm["lng"],
        "precision_coordenadas": inm["precision_coordenadas"], "fuente_coordenadas": inm["fuente_coordenadas"]})


def _params_base(pf: dict, cfg_base: dict) -> dict:
    """Parámetros del motor para una base: la regla oficial más lo que la base declara en almacen.yaml."""
    pb = dict(pf)
    pb["comparables"] = dict(pf["comparables"])
    coincidir = ["zona"] + (["estado_canonico"] if cfg_base.get("exige_estado", True) else []) + list(cfg_base.get("coincidir") or [])
    pb["comparables"]["coincidir"] = coincidir
    if cfg_base.get("operacion_comparable"):
        pb["comparables"]["filtros"] = {"operacion": cfg_base["operacion_comparable"]}
    return pb


def _contexto_reto(con: sqlite3.Connection, pf: dict, fecha: str) -> dict:
    insumos = {r["insumo"].split(":", 1)[1]: r for r in con.execute("SELECT * FROM etl_insumo WHERE insumo LIKE 'reto:%'")}
    meta = json.loads(insumos["transporte"]["metadatos_json"] or "{}") if "transporte" in insumos else {}
    return {"fecha_generacion": fecha, "fecha_capas_osm": meta.get("fecha_osm"),
            "archivos": {k: {"archivo": r["ruta"], "sha256": r["sha256"]} for k, r in insumos.items()}}


def _corrida_vigente(con: sqlite3.Connection) -> dict:
    fila = con.execute("SELECT corrida_id, fin, estado, huella_config, version_esquema FROM etl_corrida "
                       "WHERE estado IN ('ok', 'con_alertas', 'con_fallas') ORDER BY corrida_id DESC LIMIT 1").fetchone()
    return dict(fila) if fila else {}


# ---------------------------------------------------------------------------------------------
# Ficha
# ---------------------------------------------------------------------------------------------
def ficha_inmueble(con: sqlite3.Connection, p: dict, pf: dict, inmueble_nk: str, fecha: str | None = None) -> dict:
    """Ficha del inmueble `inmueble_nk` (cot:<cotizacion_id> o reto:<id_inmueble>) leída del almacén."""
    inm = leer_inmueble(con, inmueble_nk)
    if inm is None:
        raise LookupError(f"El inmueble {inmueble_nk} no está en el almacén: corra el ETL")
    cfg = p["almacen"]
    de_reto = inm["origen_inmueble"] == "reto"
    fecha = str(fecha or pf["ejecucion"]["fecha_generacion"] or date.today().isoformat())
    faltan = [k for k, v in (("area_del_inmueble", inm["area_m2"]), ("estado_del_inmueble", None if inm["estado_canonico"] == "sin_clasificar" else 1),
                             ("tipo_de_inmueble", None if (inm["tipo_inmueble"] == "sin_especificar" and not de_reto) else 1)) if v is None]

    # ---- mercado: cada base aplicable por separado ----
    resultados: dict[str, dict] = {}
    bases = []
    historicos: dict[str, pd.DataFrame] = {}        # la base antes de la regla de vigencia (referencia histórica)
    cfg_v = cfg.get("vigencia") or {}
    con_vigencia = inm["origen_inmueble"] in (cfg_v.get("aplica_a") or [])
    for base in cfg["preferencia"]:
        cfg_base = cfg["bases_mercado"][base]
        info = {"base": base, "etiqueta": cfg_base["etiqueta"], "vinculo": cfg_base["vinculo"],
                "exige_estado": bool(cfg_base.get("exige_estado", True)), "aplicable": False, "motivo": "", "es_principal": False}
        if cfg_base["vinculo"] == "zona":
            zona = inm["zona_mercado"] or inm["zona_ubicacion"]
            if not zona:
                info["motivo"] = "El barrio del predio no pertenece a ninguna zona de mercado de esta base"
        else:
            zona = f"radio_{inm['radio_m']}_m"
            n = con.execute("SELECT COUNT(*) FROM fact_oferta_predio WHERE predio_key=? AND vinculo='radio'", (inm["predio_key"],)).fetchone()[0]
            if not n:
                zona, info["motivo"] = None, "No hay ofertas de esta base capturadas en el radio del predio"
        if zona and faltan and not (cfg_base["vinculo"] == "radio" and faltan == ["estado_del_inmueble"] and not info["exige_estado"]):
            zona, info["motivo"] = None, "Faltan datos del inmueble: " + ", ".join(f.replace("_", " ") for f in faltan)
        if zona:
            marco = marco_ofertas(con, base, cfg_base, inm["predio_key"], None if cfg_base["vinculo"] == "zona" else zona)
            if con_vigencia:
                completo = marco
                marco, info["vigencia"] = aplicar_vigencia(marco, cfg_v, fecha)
                if info["vigencia"]["fuera_de_vigencia"] or info["vigencia"]["sin_fecha"]:
                    historicos[base] = completo
            resultados[base] = {"marco": marco, "zona": zona, "params": _params_base(pf, cfg_base), "cfg": cfg_base}
            info["aplicable"] = True
        bases.append(info)

    fichas_base: dict[str, dict] = {}
    entorno, insumos_ent = entorno_almacen.bloque_entorno(con, inm, pf, estaciones_unicas=not de_reto)
    if de_reto:
        control = entorno_almacen.control_proyectado(inm, insumos_ent, entorno, pf)
        norma = entorno_almacen.norma_desde_capa(inm, pf)
        contexto = _contexto_reto(con, pf, fecha)
    else:
        control = None if inm["control_9377_max_dif_m"] is None else {
            "crs": pf["crs"]["metrico"], "max_diferencia_m": inm["control_9377_max_dif_m"],
            "nota": "Mayor diferencia entre haversine y la distancia proyectada, medida en la captura sobre una muestra de lugares"}
        norma = entorno_almacen.norma_desde_captura(inm)
        contexto = {"fecha_generacion": fecha, "fecha_capas_osm": None, "archivos": {}}
    bloques = {"entorno": entorno, "control": control, "norma": norma}
    for base, r in resultados.items():
        ctx = dict(contexto, fuentes=_fuentes(con, inm, r["cfg"], pf, de_reto))
        fichas_base[base] = construir_ficha(_serie_inmueble(inm, r["zona"]), r["marco"], None, None, r["params"], ctx, bloques)

    # Referencia histórica: el mismo cálculo sin la regla de vigencia. Se informa aparte y nunca es la principal.
    referencia = {}
    for base, completo in historicos.items():
        r = resultados[base]
        ctx = dict(contexto, fuentes=_fuentes(con, inm, r["cfg"], pf, de_reto))
        h = construir_ficha(_serie_inmueble(inm, r["zona"]), completo, None, None, r["params"], ctx, bloques)["mercado"]
        fechas = completo.loc[completo["motivo_invalido"].isna(), "fecha"].dropna()
        referencia[base] = {"etiqueta": r["cfg"]["etiqueta"], **{k: h["precio_m2"][k] for k in RESUMEN_BASE},
                            "tipo_precio": h["base_referencia"],
                            "publicadas_entre": None if fechas.empty else [fechas.min().date().isoformat(), fechas.max().date().isoformat()],
                            "nota": "Ofertas fuera del plazo de vigencia: sirven de contexto, no son comparables."}

    principal = next((b for b in cfg["preferencia"] if b in fichas_base
                      and fichas_base[b]["mercado"]["precio_m2"]["n"] >= cfg["minimo_para_preferir"]), None)
    if principal is None and fichas_base:
        principal = max(fichas_base, key=lambda b: (fichas_base[b]["mercado"]["precio_m2"]["n"], -cfg["preferencia"].index(b)))
    if principal is None:                             # ninguna base aplica: ficha sin mercado, con su motivo
        vacio = pd.DataFrame(columns=["origen", "id_anuncio", "fuente", "fecha", "zona", "area_m2", "precio", "precio_m2",
                                      "estado_canonico", "tipo_precio", "fila_origen", "es_duplicado", "motivo_invalido",
                                      "grupo_dup", "id_representante", "grupo_ambiguo", "tipo_inmueble", "operacion"])
        vacio["es_duplicado"] = vacio["es_duplicado"].astype(bool)
        vacio["grupo_ambiguo"] = vacio["grupo_ambiguo"].astype(bool)
        ctx = dict(contexto, fuentes=_fuentes(con, inm, None, pf, de_reto))
        ficha = construir_ficha(_serie_inmueble(inm, "sin_zona"), vacio, None, None, pf, ctx, bloques)
        ficha["mercado"]["motivo_sin_mercado"] = "; ".join(f"{b['etiqueta']}: {b['motivo']}" for b in bases if b["motivo"]) \
            or "No hay bases de mercado definidas"
        ficha["mercado"]["criterio_comparables"]["rango_area_m2"] = None
    else:
        ficha = fichas_base[principal]
    for info in bases:
        info["es_principal"] = info["base"] == principal
        if info["base"] in fichas_base:
            m = fichas_base[info["base"]]["mercado"]
            info.update({k: m["precio_m2"][k] for k in RESUMEN_BASE}, confianza=m["confianza"]["nivel"],
                        base_referencia=m["base_referencia"])

    # ---- campos propios de la aplicación única (no cambian ni quitan campos oficiales) ----
    mercado = ficha["mercado"]
    mercado["base_mercado"] = principal
    mercado["bases"] = bases
    if con_vigencia:
        vistos = [b["vigencia"] for b in bases if b.get("vigencia")]
        mercado["vigencia"] = {
            "max_meses_publicacion": int(cfg_v["max_meses_publicacion"]), "regla_sin_fecha": cfg_v["sin_fecha_publicacion"],
            "fecha_referencia": fecha, "corte": vistos[0]["corte"] if vistos else None,
            "fuera_de_vigencia": sum(v["fuera_de_vigencia"] for v in vistos), "sin_fecha": sum(v["sin_fecha"] for v in vistos)}
        if referencia:
            ficha["referencia_historica"] = referencia
        if not mercado["precio_m2"]["n"]:
            ficha["datos_faltantes"].append("comparables_vigentes")
    if principal:
        cfg_p = resultados[principal]["cfg"]
        _enriquecer_comparables(mercado, resultados[principal]["marco"])
        if not cfg_p.get("exige_estado", True):
            ficha["datos_faltantes"].append("estado_de_comparables")
        if cfg_p.get("claves_duplicado"):             # la base tiene su propia clave de duplicados (almacen.yaml)
            ficha["trazabilidad"]["regla_deduplicacion"] = {"claves_sin_id": list(cfg_p["claves_duplicado"]),
                                                             "representante": "primera_fila"}
        entre = cfg_p.get("duplicados_entre_sitios") or {}
        if entre.get("activa") and entre.get("accion") == "enlazar":     # criterio de tolerancia: queda a la vista
            marco_p = resultados[principal]["marco"]
            regla = {k: entre[k] for k in ("tolerancia_area_pct", "tolerancia_precio_pct")}
            regla.update(exigir_mismo_barrio=bool(entre.get("exigir_mismo_barrio", True)),
                         iguales_si_existen=list(entre.get("iguales_si_existen") or []))
            ficha["trazabilidad"]["regla_deduplicacion"]["entre_sitios"] = regla
            mercado["repetidos_entre_sitios"] = dict(regla, n=int((marco_p["regla_dup"] == REGLA_ENTRE_SITIOS).sum()))
        mercado["vinculo"] = cfg_p["vinculo"]
        mercado["radio_m"] = inm["radio_m"] if cfg_p["vinculo"] == "radio" and not cfg_p.get("ubicacion_comparables") else None
        if cfg_p.get("ubicacion_comparables"):      # la base no ubica sus ofertas por distancia
            mercado["ubicacion_comparables"] = cfg_p["ubicacion_comparables"]
    otras = {b: f["mercado"] for b, f in fichas_base.items() if b != principal}
    for b, m in otras.items():
        _enriquecer_comparables(m, resultados[b]["marco"])
    if otras:
        ficha["mercado_otras_bases"] = otras
    ficha["datos_faltantes"] += faltan
    if principal is None and "sin_base_de_mercado" not in ficha["datos_faltantes"]:
        ficha["datos_faltantes"].append("sin_base_de_mercado")
    if inm["area_m2"] is None:
        ficha["inmueble"]["area_m2"] = None
    if not de_reto:
        ficha["inmueble"].update(tipo=inm["tipo_inmueble"], habitaciones=inm["habitaciones"], banos=inm["banos"],
                                 parqueaderos=inm["parqueaderos"], comuna=inm["comuna"], conjunto=inm["conjunto"],
                                 caracteristicas=json.loads(inm["caracteristicas_json"]) if inm["caracteristicas_json"] else [])
        pedidos = [dict(r) for r in con.execute(
            "SELECT ti.tipo_inmueble, x.es_cotizado, x.origen FROM fact_interes x JOIN dim_tipo_inmueble ti "
            "ON ti.tipo_inmueble_key = x.tipo_inmueble_key WHERE x.cotizacion_id = ? ORDER BY x.orden, ti.tipo_inmueble",
            (inmueble_nk.split(":", 1)[1],))]
        ficha["busqueda_cliente"] = {
            "tipos": [t["tipo_inmueble"] for t in pedidos], "tipo_cotizado": inm["tipo_inmueble"],
            "oferta_por_tipo": _panorama(pedidos, {b: r["marco"] for b, r in resultados.items()}, historicos, cfg),
            "nota": "Ofertas válidas de venta a la vista del predio, por tipo. Sin filtro de área: el área declarada es la "
                    "del inmueble cotizado."}
        ficha["predio"] = {
            "cbml": inm["cbml"], "direccion_catastral": inm["direccion"], "conjunto": inm["conjunto"],
            "propiedad_horizontal": None if inm["es_ph"] is None else bool(inm["es_ph"]),
            "area_lote_m2": inm["area_lote"], "pisos_max": inm["pisos_max"],
            "valor_referencia_suelo_m2": inm["zge_valor_m2"],
            "nota_valor_referencia": "Valor de referencia oficial del suelo (zona geoeconómica). No es precio de mercado ni avalúo.",
            "estacion_masiva_mas_cercana": None if not inm["estacion_masiva"] else {
                "nombre": inm["estacion_masiva"], "linea": inm["estacion_masiva_linea"], "distancia_m": inm["estacion_masiva_m"]}}
        ficha["entorno"]["lugares_por_categoria"] = _lugares_por_categoria(con, inm["predio_key"], entorno["radio_busqueda_m"])
    corrida = _corrida_vigente(con)
    ficha["trazabilidad"]["version_motor"] = __version__
    ficha["trazabilidad"]["almacen"] = {
        "inmueble": inmueble_nk, "predio": inm["predio_nk"], "corrida_etl": corrida.get("corrida_id"),
        "corrida_estado": corrida.get("estado"), "corrida_fin": corrida.get("fin"),
        "version_esquema": corrida.get("version_esquema"), "huella_almacen": p["huella_almacen"],
        "huella_captura": inm["huella_config"],
        "tablas": ["dim_inmueble", "dim_predio", "dim_ubicacion", "fact_oferta", "fact_oferta_predio", "fact_entorno"]}
    return ficha


def _enriquecer_comparables(mercado: dict, marco: pd.DataFrame) -> None:
    """Agrega a cada comparable lo que el almacén sabe de él: distancia, fecha, barrio y tipo."""
    if not mercado["comparables"]:
        return
    por_origen = marco.set_index("origen")
    for c in mercado["comparables"]:
        f = por_origen.loc[c["origen"]]
        if pd.notna(f.get("distancia_m")):
            c["distancia_m"] = round(float(f["distancia_m"]))
        if pd.notna(f.get("fecha")):
            c["fecha"] = f["fecha"].date().isoformat()
        if f.get("barrio") and f["barrio"] != "Sin barrio informado":
            c["barrio"] = f["barrio"]
        if f.get("tipo_inmueble") and f["tipo_inmueble"] != "sin_especificar":
            c["tipo_inmueble"] = f["tipo_inmueble"]
        if isinstance(f.get("url"), str) and f["url"]:
            c["url"] = f["url"]
        if "fecha_observacion" in por_origen.columns and pd.isna(f.get("fecha")) and pd.notna(f.get("fecha_observacion")):
            c["vista_el"] = f["fecha_observacion"].date().isoformat()


def _lugares_por_categoria(con: sqlite3.Connection, predio_key: int, radio_ficha: int) -> dict:
    """Conteo de lugares depurados por categoría de la captura: dentro del radio de la ficha y en todo el barrido."""
    salida = {}
    for r in con.execute("SELECT categoria, SUM(distancia_m <= ?) AS en_radio, COUNT(*) AS total, MIN(distancia_m) AS minimo "
                         "FROM v_entorno WHERE predio_key=? AND cuenta=1 AND en_mapa=1 GROUP BY categoria ORDER BY categoria",
                         (radio_ficha, predio_key)):
        salida[r["categoria"]] = {"en_radio": int(r["en_radio"] or 0), "en_barrido": int(r["total"]),
                                  "mas_cercano_m": None if r["minimo"] is None else round(r["minimo"])}
    return salida


def _fuentes(con: sqlite3.Connection, inm: dict, cfg_base: dict | None, pf: dict, de_reto: bool) -> list[str] | None:
    """Fuentes que soportan la ficha. En los inmuebles de la muestra se conserva la lista oficial."""
    if de_reto:
        return None                                   # construir_ficha usa ficha.yaml › ficha.fuentes (lista oficial)
    fuentes = [cfg_base["fuente_ficha"]] if cfg_base else []
    for r in con.execute("SELECT DISTINCT f.nombre FROM fact_entorno e JOIN dim_fuente f ON f.fuente_key=e.fuente_key "
                         "WHERE e.predio_key=? ORDER BY f.nombre", (inm["predio_key"],)):
        if r[0]:
            fuentes.append(r[0])
    return fuentes or ["Sin fuentes cargadas para este predio"]


def resumen(ficha: dict) -> dict:
    """Lo que el almacén guarda de cada ficha (fact_ficha) y lo que muestran las listas."""
    m, e = ficha["mercado"], ficha["entorno"]
    pm = m["precio_m2"]
    return {"estado_referencia": m["estado_referencia"], "base_mercado": m.get("base_mercado"),
            "tipo_precio_base": m.get("base_referencia"), **{k: pm[k] for k in RESUMEN_BASE},
            "confianza": m["confianza"]["nivel"], "estaciones_en_radio": e["estaciones_en_radio"],
            "equipamientos_en_radio": sum(e["equipamientos_en_radio"].values()),
            "dist_estacion_m": (e["estacion_mas_cercana"] or {}).get("distancia_m"),
            "norma_estado": ficha["norma"]["estado"], "n_datos_faltantes": len(ficha["datos_faltantes"]),
            "flexibilizada": bool(m["criterio_comparables"].get("aplicada"))}


def fichas_reto(con: sqlite3.Connection, p: dict, pf: dict, fecha: str | None = None) -> list[dict]:
    """Las fichas de los inmuebles de prueba del reto, en el orden del CSV oficial."""
    ids = [r[0] for r in con.execute("SELECT inmueble_nk FROM dim_inmueble WHERE origen='reto' ORDER BY inmueble_key")]
    return [ficha_inmueble(con, p, pf, nk, fecha) for nk in ids]
