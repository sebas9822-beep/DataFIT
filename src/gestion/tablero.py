"""Tablero comercial y estado del almacén. Todo se lee del esquema estrella.

Las cifras salen de `fact_cotizacion`, `fact_ficha` y `fact_meta` cruzadas con sus dimensiones. Antes de
leer se refresca la gestión (comerciales, clientes, cotizaciones, fichas y metas) si la base operativa cambió.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from almacen import esquema, etl
from almacen import normalizacion as nz
from captura import acceso, bd

from .metas import GestionInvalida


def _hoy_local(p: dict) -> date:
    return nz.a_local(bd.ahora(), p["almacen"]["tiempo"]["desfase_utc_horas"]).date()


def _clave(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def _periodo(p: dict, desde, hasta) -> tuple[date, date]:
    hoy = _hoy_local(p)
    try:
        fin = date.fromisoformat(str(hasta)[:10]) if hasta else hoy
        ini = date.fromisoformat(str(desde)[:10]) if desde else fin - timedelta(days=p["reglas"]["supervision"]["dias_tablero_por_defecto"] - 1)
    except ValueError:
        raise GestionInvalida("Las fechas van como AAAA-MM-DD") from None
    if ini > fin:
        raise GestionInvalida("La fecha inicial es posterior a la final")
    tope = p["reglas"]["supervision"]["max_dias_periodo"]
    if (fin - ini).days > tope:
        raise GestionInvalida(f"El periodo máximo es de {tope} días")
    return ini, fin


def _filtro(usuarios: list[str] | None, alias: str = "co") -> tuple[str, list]:
    if usuarios is None:
        return "", []
    if not usuarios:
        return f" AND {alias}.usuario IS NULL", []
    return f" AND {alias}.usuario IN ({', '.join('?' for _ in usuarios)})", list(usuarios)


def tablero(p: dict, quien: dict, desde=None, hasta=None, comercial: str | None = None) -> dict:
    """Consolidado del periodo para los comerciales que `quien` tiene a cargo (o uno solo de ellos)."""
    ini, fin = _periodo(p, desde, hasta)
    a_cargo = acceso.listar_comerciales(p, quien)
    usuarios = [c["usuario"] for c in a_cargo]
    if comercial:
        if comercial not in usuarios:
            raise GestionInvalida("Ese comercial no está a tu cargo")
        usuarios = [comercial]
    etl.refrescar_gestion(p)
    dias = (fin - ini).days + 1
    ini_prev, fin_prev = ini - timedelta(days=dias), ini - timedelta(days=1)
    f_sql, f_args = _filtro(usuarios)
    con = esquema.conectar(p["rutas"]["almacen"])
    try:
        def uno(sql: str, *args):
            return con.execute(sql, args).fetchone()

        base = ("FROM fact_cotizacion f JOIN dim_comercial co ON co.comercial_key = f.comercial_key "
                "WHERE f.fecha_key BETWEEN ? AND ?" + f_sql)

        def totales(a: date, b: date) -> dict:
            r = uno("SELECT COALESCE(SUM(f.cuenta), 0) AS cotizaciones, SUM(f.cuenta = 0 AND f.estado = 'error') AS fallidas, "
                    "COUNT(DISTINCT CASE WHEN f.cuenta = 1 THEN f.cliente_key END) AS clientes, "
                    "COALESCE(SUM(f.cuenta AND f.reusada), 0) AS reusadas, COALESCE(SUM(f.cuenta AND f.tiene_ficha), 0) AS con_ficha, "
                    "AVG(CASE WHEN f.cuenta = 1 AND f.reusada = 0 THEN f.duracion_s END) AS duracion_media, "
                    "COUNT(DISTINCT CASE WHEN f.cuenta = 1 THEN f.comercial_key END) AS comerciales_activos " + base,
                    _clave(a), _clave(b), *f_args)
            return {k: (r[k] or 0) if k != "duracion_media" else (None if r[k] is None else round(r[k], 1)) for k in r.keys()}

        actual, previo = totales(ini, fin), totales(ini_prev, fin_prev)
        c_sql, c_args = _filtro(usuarios, "cl_co")
        nuevos = uno("SELECT COUNT(*) FROM dim_cliente cl JOIN dim_comercial cl_co ON cl_co.usuario = cl.creado_por "
                     "WHERE cl.cliente_key > 0 AND cl.fecha_alta_key BETWEEN ? AND ?" + c_sql, _clave(ini), _clave(fin), *c_args)[0]
        nuevos_prev = uno("SELECT COUNT(*) FROM dim_cliente cl JOIN dim_comercial cl_co ON cl_co.usuario = cl.creado_por "
                          "WHERE cl.cliente_key > 0 AND cl.fecha_alta_key BETWEEN ? AND ?" + c_sql,
                          _clave(ini_prev), _clave(fin_prev), *c_args)[0]
        registrados = uno("SELECT COUNT(*) FROM dim_cliente cl JOIN dim_comercial cl_co ON cl_co.usuario = cl.creado_por "
                          "WHERE cl.cliente_key > 0" + c_sql, *c_args)[0]

        # ---- por comercial, con la meta del mes en que termina el periodo ----
        mes = f"{fin.year:04d}-{fin.month:02d}"
        mes_ini, mes_fin = date(fin.year, fin.month, 1), (date(fin.year + (fin.month == 12), fin.month % 12 + 1, 1) - timedelta(days=1))
        filas = {r["usuario"]: dict(r) for r in con.execute(
            "SELECT co.usuario, COALESCE(SUM(f.cuenta), 0) AS cotizaciones, "
            "COUNT(DISTINCT CASE WHEN f.cuenta = 1 THEN f.cliente_key END) AS clientes, "
            "COALESCE(SUM(f.cuenta AND f.tiene_ficha), 0) AS fichas, SUM(f.cuenta = 0 AND f.estado = 'error') AS fallidas, "
            "MAX(f.creada_en) AS ultima " + base + " GROUP BY co.usuario", (_clave(ini), _clave(fin), *f_args))}
        del_mes = {r["usuario"]: dict(r) for r in con.execute(
            "SELECT co.usuario, COALESCE(SUM(f.cuenta), 0) AS cotizaciones, "
            "COUNT(DISTINCT CASE WHEN f.cuenta = 1 THEN f.cliente_key END) AS clientes " + base + " GROUP BY co.usuario",
            (_clave(mes_ini), _clave(mes_fin), *f_args))}
        metas = {r["usuario"]: dict(r) for r in con.execute(
            "SELECT co.usuario, m.meta_cotizaciones, m.meta_clientes, m.definida_por FROM fact_meta m "
            "JOIN dim_comercial co ON co.comercial_key = m.comercial_key WHERE m.anio_mes = ?" + f_sql, (mes, *f_args))}
        por_comercial = []
        for c in a_cargo:
            if c["usuario"] not in usuarios:
                continue
            f = filas.get(c["usuario"], {})
            m, dm = metas.get(c["usuario"], {}), del_mes.get(c["usuario"], {})
            meta = m.get("meta_cotizaciones")
            por_comercial.append({
                "usuario": c["usuario"], "nombre": c["nombre"], "activo": c["activo"],
                "cotizaciones": f.get("cotizaciones", 0), "clientes": f.get("clientes", 0), "fichas": f.get("fichas", 0),
                "fallidas": f.get("fallidas") or 0, "ultima": f.get("ultima"),
                "mes": mes, "cotizaciones_mes": dm.get("cotizaciones", 0), "clientes_mes": dm.get("clientes", 0),
                "meta_cotizaciones": meta, "meta_clientes": m.get("meta_clientes"),
                "avance": None if not meta else round(dm.get("cotizaciones", 0) / meta, 4)})
        meta_total = sum(c["meta_cotizaciones"] or 0 for c in por_comercial)
        hechas_mes = sum(c["cotizaciones_mes"] for c in por_comercial if c["meta_cotizaciones"])

        # ---- serie diaria (con los días sin cotizaciones en cero) ----
        por_dia = {r["fecha"]: r for r in con.execute(
            "SELECT d.fecha, COALESCE(SUM(f.cuenta), 0) AS cotizaciones, COUNT(DISTINCT CASE WHEN f.cuenta = 1 THEN f.cliente_key END) AS clientes "
            "FROM fact_cotizacion f JOIN dim_fecha d ON d.fecha_key = f.fecha_key JOIN dim_comercial co ON co.comercial_key = f.comercial_key "
            "WHERE f.fecha_key BETWEEN ? AND ?" + f_sql + " GROUP BY d.fecha", (_clave(ini), _clave(fin), *f_args))}
        serie = []
        for k in range(dias):
            d = (ini + timedelta(days=k)).isoformat()
            serie.append({"fecha": d, "cotizaciones": por_dia[d]["cotizaciones"] if d in por_dia else 0,
                          "clientes": por_dia[d]["clientes"] if d in por_dia else 0})

        def grupo(sql: str) -> list[dict]:
            return [dict(r) for r in con.execute(sql, (_clave(ini), _clave(fin), *f_args))]

        cuerpo = ("FROM fact_cotizacion f JOIN dim_comercial co ON co.comercial_key = f.comercial_key {joins} "
                  "WHERE f.cuenta = 1 AND f.fecha_key BETWEEN ? AND ?" + f_sql + " GROUP BY 1 ORDER BY n DESC, 1 {limite}")
        por_barrio = grupo("SELECT COALESCE(u.barrio, 'Sin barrio') AS etiqueta, COUNT(*) AS n " + cuerpo.format(
            joins="JOIN dim_ubicacion u ON u.ubicacion_key = f.ubicacion_key",
            limite=f"LIMIT {int(p['reglas']['supervision']['barrios_en_tablero'])}"))
        por_tipo = grupo("SELECT ti.tipo_inmueble AS etiqueta, COUNT(*) AS n " + cuerpo.format(
            joins="JOIN dim_inmueble i ON i.inmueble_key = f.inmueble_key JOIN dim_tipo_inmueble ti ON ti.tipo_inmueble_key = i.tipo_inmueble_key",
            limite=""))
        por_estado_inm = grupo("SELECT es.estado AS etiqueta, COUNT(*) AS n " + cuerpo.format(
            joins="JOIN dim_inmueble i ON i.inmueble_key = f.inmueble_key JOIN dim_estado_inmueble es ON es.estado_key = i.estado_key",
            limite=""))
        por_confianza = grupo("SELECT COALESCE(fi.confianza, 'sin_ficha') AS etiqueta, COUNT(*) AS n " + cuerpo.format(
            joins="LEFT JOIN fact_ficha fi ON fi.cotizacion_id = f.cotizacion_id AND fi.vigente = 1", limite=""))
        por_base = grupo("SELECT COALESCE(fi.base_mercado, 'sin_mercado') AS etiqueta, COUNT(*) AS n " + cuerpo.format(
            joins="JOIN fact_ficha fi ON fi.cotizacion_id = f.cotizacion_id AND fi.vigente = 1", limite=""))
        por_estado = [dict(r) for r in con.execute(
            "SELECT f.estado AS etiqueta, COUNT(*) AS n " + base + " GROUP BY 1 ORDER BY n DESC", (_clave(ini), _clave(fin), *f_args))]
        por_dia_semana = grupo("SELECT d.dia_semana AS orden, d.dia_nombre AS etiqueta, COUNT(*) AS n "
                               "FROM fact_cotizacion f JOIN dim_comercial co ON co.comercial_key = f.comercial_key "
                               "JOIN dim_fecha d ON d.fecha_key = f.fecha_key WHERE f.cuenta = 1 AND f.fecha_key BETWEEN ? AND ?"
                               + f_sql + " GROUP BY 1, 2 ORDER BY 1")
        # ---- demanda: qué tipo de inmueble pidieron los clientes (fact_interes, una fila por tipo pedido) ----
        dem = ("FROM fact_interes x JOIN dim_comercial co ON co.comercial_key = x.comercial_key "
               "JOIN dim_tipo_inmueble ti ON ti.tipo_inmueble_key = x.tipo_inmueble_key "
               "WHERE x.cuenta = 1 AND x.fecha_key BETWEEN ? AND ?" + f_sql)
        dem_args = (_clave(ini), _clave(fin), *f_args)
        con_busqueda = uno("SELECT COUNT(DISTINCT x.cotizacion_id) " + dem, *dem_args)[0] or 0
        pedidos = {r["tipo"]: dict(r) for r in con.execute(
            "SELECT ti.tipo_inmueble AS tipo, COUNT(*) AS solicitudes, COALESCE(SUM(x.es_cotizado), 0) AS cotizado, "
            "COUNT(DISTINCT x.cliente_key) AS clientes, COUNT(DISTINCT x.comercial_key) AS comerciales, "
            "ROUND(AVG(x.ofertas_radio), 1) AS ofertas_media " + dem + " GROUP BY 1", dem_args)}
        tipos = list(p["reglas"]["inmueble"]["tipos"]) + sorted(set(pedidos) - set(p["reglas"]["inmueble"]["tipos"]))
        demanda_tipo = sorted(
            ({"tipo": t, "solicitudes": 0, "cotizado": 0, "clientes": 0, "comerciales": 0, "ofertas_media": None,
              **pedidos.get(t, {}), "no_lo_pidieron": con_busqueda - pedidos.get(t, {}).get("solicitudes", 0)} for t in tipos),
            key=lambda x: (-x["solicitudes"], tipos.index(x["tipo"])))
        demanda_comercial = [dict(r) for r in con.execute(
            "SELECT co.usuario, co.nombre, ti.tipo_inmueble AS tipo, COUNT(*) AS solicitudes, COUNT(DISTINCT x.cliente_key) AS clientes "
            + dem + " GROUP BY 1, 2, 3 ORDER BY co.nombre, solicitudes DESC", dem_args)]
        corrida = con.execute("SELECT corrida_id, fin, estado, modo FROM etl_corrida WHERE estado <> 'error' "
                              "ORDER BY corrida_id DESC LIMIT 1").fetchone()
    finally:
        con.close()

    def delta(a, b):
        return None if not b else round((a - b) / b, 4)

    return {
        "periodo": {"desde": ini.isoformat(), "hasta": fin.isoformat(), "dias": dias,
                    "anterior": {"desde": ini_prev.isoformat(), "hasta": fin_prev.isoformat()}},
        "alcance": {"comerciales": len(usuarios), "comercial": comercial, "todos": comercial is None},
        "kpis": {
            "cotizaciones": {"valor": actual["cotizaciones"], "anterior": previo["cotizaciones"],
                             "variacion": delta(actual["cotizaciones"], previo["cotizaciones"])},
            "clientes_cotizados": {"valor": actual["clientes"], "anterior": previo["clientes"],
                                   "variacion": delta(actual["clientes"], previo["clientes"])},
            "clientes_nuevos": {"valor": nuevos, "anterior": nuevos_prev, "variacion": delta(nuevos, nuevos_prev),
                                "registrados_total": registrados},
            "fichas": {"valor": actual["con_ficha"], "de": actual["cotizaciones"]},
            "fallidas": {"valor": actual["fallidas"]},
            "reusadas": {"valor": actual["reusadas"], "de": actual["cotizaciones"]},
            "duracion_media_s": actual["duracion_media"],
            "comerciales_activos": {"valor": actual["comerciales_activos"], "de": len(usuarios)},
            "meta_mes": {"mes": mes, "meta": meta_total or None, "hechas": hechas_mes,
                         "avance": None if not meta_total else round(hechas_mes / meta_total, 4)},
        },
        "por_comercial": por_comercial, "serie_diaria": serie, "por_barrio": por_barrio, "por_tipo_inmueble": por_tipo,
        "por_estado_inmueble": por_estado_inm, "por_confianza": por_confianza, "por_base_mercado": por_base,
        "por_estado": por_estado, "por_dia_semana": por_dia_semana,
        "demanda": {"cotizaciones_con_busqueda": con_busqueda,
                    "cotizaciones_sin_busqueda": max(0, actual["cotizaciones"] - con_busqueda),
                    "por_tipo": demanda_tipo, "por_comercial": demanda_comercial,
                    "sin_solicitudes": [x["tipo"] for x in demanda_tipo if not x["solicitudes"]]},
        "almacen": dict(corrida) if corrida else None,
    }


def mi_gestion(p: dict, quien: dict) -> dict:
    """Lo que ve un comercial de su propia gestión: meta y avance del mes, y el seguimiento que le dejaron."""
    from . import seguimiento
    hoy = _hoy_local(p)
    mes = f"{hoy.year:04d}-{hoy.month:02d}"
    etl.refrescar_gestion(p)
    con = esquema.conectar(p["rutas"]["almacen"])
    try:
        fila = con.execute("SELECT * FROM v_avance_meta WHERE comercial=? AND anio_mes=?", (quien["usuario"], mes)).fetchone()
        hechas = con.execute(
            "SELECT COALESCE(SUM(f.cuenta), 0) AS cotizaciones, COUNT(DISTINCT CASE WHEN f.cuenta = 1 THEN f.cliente_key END) AS clientes "
            "FROM fact_cotizacion f JOIN dim_comercial co ON co.comercial_key = f.comercial_key "
            "JOIN dim_fecha d ON d.fecha_key = f.fecha_key WHERE co.usuario=? AND d.anio_mes=?", (quien["usuario"], mes)).fetchone()
    finally:
        con.close()
    meta = fila["meta_cotizaciones"] if fila else None
    return {"mes": mes, "cotizaciones": hechas["cotizaciones"], "clientes": hechas["clientes"],
            "meta_cotizaciones": meta, "meta_clientes": fila["meta_clientes"] if fila else None,
            "avance": None if not meta else round(hechas["cotizaciones"] / meta, 4),
            "seguimiento": seguimiento.listar(p, [quien["usuario"]], "abierto",
                                              p["reglas"]["supervision"]["seguimientos_del_comercial"])}


def estado_almacen(p: dict) -> dict:
    """Panel de calidad de datos: tablas de la estrella, últimas corridas, controles y rechazos."""
    ruta = p["rutas"]["almacen"]
    corridas = p["reglas"]["supervision"]["corridas_en_panel"]
    con = esquema.conectar(ruta)
    try:
        esquema.crear(con)
        con.commit()
        tablas = []
        for nombre in esquema.TABLAS_DIM + esquema.TABLAS_HECHOS:
            n = con.execute(f"SELECT COUNT(*) FROM {nombre}").fetchone()[0]
            tablas.append({"tabla": nombre, "tipo": "dimensión" if nombre.startswith("dim_") else "hecho",
                           "filas": n - (1 if nombre.startswith("dim_") else 0)})
        lista = [dict(r) for r in con.execute(
            "SELECT corrida_id, inicio, fin, modo, alcance, estado, version_app, huella_config, mensaje, resumen_json "
            "FROM etl_corrida ORDER BY corrida_id DESC LIMIT ?", (corridas,))]
        for c in lista:
            c["resumen"] = json.loads(c.pop("resumen_json") or "{}")
        ultima = next((c for c in lista if c["estado"] in ("ok", "con_alertas", "con_fallas")), None)
        completa = con.execute("SELECT corrida_id, fin, estado FROM etl_corrida WHERE modo='completo' AND estado <> 'error' "
                               "ORDER BY corrida_id DESC LIMIT 1").fetchone()
        controles, pasos, rechazos = [], [], []
        if ultima:
            controles = [dict(r) for r in con.execute(
                "SELECT control, tabla, resultado, valor, umbral, detalle FROM etl_calidad WHERE corrida_id=? "
                "ORDER BY CASE resultado WHEN 'falla' THEN 0 WHEN 'alerta' THEN 1 ELSE 2 END, control", (ultima["corrida_id"],))]
        ref = completa["corrida_id"] if completa else (ultima["corrida_id"] if ultima else None)
        if ref:
            pasos = [dict(r) for r in con.execute(
                "SELECT entidad, alcance, leidos, cargados, duplicados, rechazados, cuadra, detalle FROM etl_paso "
                "WHERE corrida_id=? ORDER BY entidad, alcance", (ref,))]
            rechazos = [dict(r) for r in con.execute(
                "SELECT entidad, motivo_exclusion, SUM(n) AS n, COUNT(DISTINCT alcance) AS alcances, MAX(detalle) AS detalle "
                "FROM etl_rechazo WHERE corrida_id=? GROUP BY entidad, motivo_exclusion ORDER BY n DESC", (ref,))]
        ofertas = [dict(r) for r in con.execute(
            "SELECT base_mercado, COUNT(*) AS total, SUM(es_valida) AS validas, SUM(es_duplicado) AS duplicadas, "
            "SUM(es_duplicado = 0 AND motivo_invalido IS NOT NULL) AS invalidas FROM fact_oferta GROUP BY base_mercado")]
        motivos = [dict(r) for r in con.execute(
            "SELECT base_mercado, motivo_exclusion, COUNT(*) AS n FROM fact_oferta WHERE motivo_exclusion IS NOT NULL "
            "GROUP BY 1, 2 ORDER BY 1, n DESC")]
        insumos = [dict(r) for r in con.execute("SELECT insumo, ruta, sha256, cargado_en FROM etl_insumo ORDER BY insumo")]
    except sqlite3.OperationalError as e:
        return {"disponible": False, "mensaje": f"El almacén todavía no está listo: {e}"}
    finally:
        con.close()
    return {"disponible": True, "ruta": str(ruta), "version_esquema": esquema.VERSION_ESQUEMA, "tablas": tablas,
            "corridas": lista, "ultima": ultima, "corrida_de_conciliacion": ref, "controles": controles, "pasos": pasos,
            "rechazos": rechazos, "ofertas": ofertas, "motivos_oferta": motivos, "insumos": insumos}
