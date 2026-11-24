"""Controles de calidad del almacén. Corren al final de cada corrida del ETL y quedan en `etl_calidad`.

- `falla`: un dato rompe una regla dura (integridad, pedido/cierre mezclados, exclusión sin motivo...).
  La corrida queda "con_fallas".
- `alerta`: el dato es válido pero conviene saberlo (ofertas históricas, cotizaciones sin ficha...).
- `ok`: el control pasó.
"""
from __future__ import annotations

import sqlite3
from datetime import date


def _n(con: sqlite3.Connection, sql: str, *args) -> int:
    return int(con.execute(sql, args).fetchone()[0] or 0)


def revisar(con: sqlite3.Connection, corrida, p: dict, pf: dict) -> None:
    cfg = p["almacen"]["calidad"]
    caja = pf["crs"]["bbox_area_estudio"]

    def duro(nombre: str, tabla: str, n: int, bien: str, mal: str) -> None:
        corrida.control(nombre, tabla, "falla" if n else "ok", n, 0, mal.format(n=n) if n else bien)

    def aviso(nombre: str, tabla: str, n: int, bien: str, mal: str, valor=None, umbral=None) -> None:
        corrida.control(nombre, tabla, "alerta" if n else "ok", n if valor is None else valor, umbral,
                        mal.format(n=n) if n else bien)

    # ---- reglas duras ----
    duro("integridad_referencial", "todas", len(con.execute("PRAGMA foreign_key_check").fetchall()),
         "Ningún hecho apunta a una dimensión inexistente", "{n} filas con llave foránea rota")
    duro("pedido_y_cierre_separados", "fact_oferta",
         _n(con, "SELECT COUNT(*) FROM fact_oferta o JOIN dim_tipo_oferta t ON t.tipo_oferta_key=o.tipo_oferta_key "
                 "WHERE o.es_valida=1 AND t.tipo_precio NOT IN ('pedido','cierre')"),
         "Toda oferta válida declara si su precio es pedido o de cierre", "{n} ofertas válidas sin tipo de precio reconocido")
    duro("precio_m2_consistente", "fact_oferta",
         _n(con, "SELECT COUNT(*) FROM fact_oferta WHERE area_m2 > 0 AND precio > 0 AND ABS(precio / area_m2 - precio_m2) > ?",
            cfg["tolerancia_precio_m2_cop"]),
         "precio_m2 = precio / área en todas las ofertas", "{n} ofertas con precio por m² que no corresponde a precio / área")
    duro("validas_con_medidas", "fact_oferta",
         _n(con, "SELECT COUNT(*) FROM fact_oferta WHERE es_valida=1 AND (area_m2 IS NULL OR precio IS NULL OR precio_m2 IS NULL "
                 "OR area_m2 <= 0 OR precio <= 0)"),
         "Toda oferta válida tiene área, precio y precio por m² positivos", "{n} ofertas válidas sin área o sin precio")
    duro("duplicados_con_auditoria", "fact_oferta",
         _n(con, "SELECT COUNT(*) FROM fact_oferta WHERE es_duplicado=1 AND (regla_dup IS NULL OR coincide_con IS NULL "
                 "OR id_representante IS NULL OR puntaje_dup IS NULL)"),
         "Cada duplicado guarda regla, puntaje, representante y la fila con la que coincidió",
         "{n} duplicados sin regla, puntaje o fila de coincidencia")
    duro("exclusiones_con_motivo", "fact_oferta",
         _n(con, "SELECT COUNT(*) FROM fact_oferta WHERE (es_valida=0 AND motivo_exclusion IS NULL) OR "
                 "(es_valida=1 AND motivo_exclusion IS NOT NULL)"),
         "Toda oferta fuera del cálculo tiene motivo_exclusion, y solo esas", "{n} ofertas excluidas sin motivo (o válidas con motivo)")
    duro("cotizacion_con_comercial_y_cliente", "fact_cotizacion",
         _n(con, "SELECT COUNT(*) FROM fact_cotizacion WHERE comercial_key=0 OR cliente_key=0"),
         "Cada cotización tiene su comercial y su cliente", "{n} cotizaciones sin comercial o sin cliente")
    duro("ficha_con_comercial_y_cliente", "fact_ficha",
         _n(con, "SELECT COUNT(*) FROM fact_ficha WHERE comercial_key=0 OR cliente_key=0"),
         "Cada ficha conserva el comercial que asesoró y el cliente que la recibe", "{n} fichas sin comercial o sin cliente")
    duro("distancias_validas", "fact_entorno",
         _n(con, "SELECT COUNT(*) FROM fact_entorno e JOIN dim_predio pr ON pr.predio_key=e.predio_key "
                 "WHERE e.distancia_m < 0 OR (e.es_estacion=0 AND e.distancia_m > pr.radio_m * 1.011)"),
         "Las distancias del entorno están en metros y dentro del radio del predio", "{n} lugares con distancia negativa o fuera del radio")
    duro("predios_en_area_de_estudio", "dim_predio",
         _n(con, "SELECT COUNT(*) FROM dim_predio WHERE utilizable=1 AND predio_key>0 AND en_cobertura IS NOT 0 AND (lat IS NULL OR lng IS NULL "
                 "OR lat NOT BETWEEN ? AND ? OR lng NOT BETWEEN ? AND ?)", caja["lat_min"], caja["lat_max"], caja["lon_min"], caja["lon_max"]),
         "Todos los predios tienen coordenadas EPSG:4326 dentro del área de estudio", "{n} predios sin coordenadas o fuera del área")
    duro("un_inmueble_por_cotizacion", "fact_cotizacion",
         _n(con, "SELECT COUNT(*) FROM (SELECT inmueble_key FROM fact_cotizacion GROUP BY inmueble_key HAVING COUNT(*) > 1)"),
         "Cada cotización tiene su propio inmueble", "{n} inmuebles compartidos por más de una cotización")

    # ---- alertas ----
    aviso("ofertas_fuera_del_area", "fact_oferta",
          _n(con, "SELECT COUNT(*) FROM fact_oferta WHERE lat IS NOT NULL AND (lat NOT BETWEEN ? AND ? OR lng NOT BETWEEN ? AND ?)",
             caja["lat_min"], caja["lat_max"], caja["lon_min"], caja["lon_max"]),
          "Las ofertas con coordenadas caen en el área de estudio", "{n} ofertas con coordenadas fuera del área de estudio")
    aviso("capturas_no_utilizables", "dim_predio",
          _n(con, "SELECT COUNT(*) FROM dim_predio WHERE origen='captura' AND utilizable=0"),
          "Todas las capturas terminaron y alimentan el almacén",
          "{n} capturas sin cerrar o con error: sus registros siguen en la base operativa y no alimentan hechos")
    aviso("cotizaciones_sin_predio", "fact_cotizacion",
          _n(con, "SELECT COUNT(*) FROM fact_cotizacion WHERE cuenta=1 AND predio_key=0"),
          "Toda cotización que cuenta tiene su predio", "{n} cotizaciones que cuentan sin predio")
    aviso("cotizaciones_sin_datos_del_inmueble", "dim_inmueble",
          _n(con, "SELECT COUNT(*) FROM fact_cotizacion f JOIN dim_inmueble i ON i.inmueble_key=f.inmueble_key "
                  "WHERE f.cuenta=1 AND i.datos_completos=0"),
          "Todas las cotizaciones traen tipo, área y estado del inmueble",
          "{n} cotizaciones sin tipo, área o estado del inmueble: su ficha no puede buscar comparables")
    duro("busqueda_del_cliente_consistente", "fact_interes",
         _n(con, "SELECT COUNT(*) FROM (SELECT cotizacion_id FROM fact_interes GROUP BY 1 "
                 "HAVING COUNT(*) <> MAX(n_tipos) OR SUM(es_cotizado) > 1 OR COUNT(DISTINCT comercial_key) > 1)")
         + _n(con, "SELECT COUNT(*) FROM fact_cotizacion f JOIN dim_inmueble i ON i.inmueble_key = f.inmueble_key "
                   "WHERE i.tipo_inmueble_key <> 0 AND NOT EXISTS (SELECT 1 FROM fact_interes x WHERE x.cotizacion_id = f.cotizacion_id "
                   "AND x.tipo_inmueble_key = i.tipo_inmueble_key AND x.es_cotizado = 1)"),
         "Cada cotización cuenta una vez en la demanda y el tipo cotizado está entre lo que busca el cliente",
         "{n} cotizaciones con la búsqueda del cliente inconsistente (tipos contados de más o tipo cotizado fuera de la búsqueda)")
    aviso("cotizaciones_sin_busqueda_registrada", "fact_interes",
          _n(con, "SELECT COUNT(*) FROM fact_cotizacion f WHERE f.cuenta = 1 AND NOT EXISTS "
                  "(SELECT 1 FROM fact_interes x WHERE x.cotizacion_id = f.cotizacion_id)"),
          "Todas las cotizaciones que cuentan registran qué tipo de inmueble busca el cliente",
          "{n} cotizaciones que cuentan sin registrar qué busca el cliente: no entran a la demanda por tipo")
    aviso("cotizaciones_sin_ficha", "fact_cotizacion",
          _n(con, "SELECT COUNT(*) FROM fact_cotizacion WHERE cuenta=1 AND tiene_ficha=0"),
          "Todas las cotizaciones que cuentan tienen ficha", "{n} cotizaciones que cuentan todavía sin ficha")
    lineas = [r[0] for r in con.execute("SELECT DISTINCT linea FROM dim_lugar WHERE capa=? AND sistema_transporte IS NULL "
                                        "AND linea IS NOT NULL ORDER BY 1", (p["almacen"]["transporte"]["capa_estaciones"],))]
    aviso("estaciones_sin_sistema", "dim_lugar", len(lineas), "Todas las líneas de transporte tienen sistema asignado",
          "{n} líneas sin sistema en almacen.yaml › transporte.lineas: " + ", ".join(lineas[:12]))
    # Solo importan los barrios de los que depende un vínculo por zona: predios y bases que entran por zona.
    por_zona = [b for b, c in p["almacen"]["bases_mercado"].items() if c.get("vinculo") == "zona"] or [""]
    aviso("barrios_sin_revisar", "dim_ubicacion",
          _n(con, f"SELECT COUNT(*) FROM dim_ubicacion u WHERE u.revisado=0 AND u.ubicacion_key>0 AND u.barrio_clave<>'' AND ("
                  f"EXISTS (SELECT 1 FROM fact_oferta o WHERE o.ubicacion_key=u.ubicacion_key AND o.base_mercado IN ({', '.join('?' for _ in por_zona)})) "
                  "OR EXISTS (SELECT 1 FROM dim_predio pr WHERE pr.ubicacion_key=u.ubicacion_key))", *por_zona),
          "Los barrios de los predios y de la muestra son oficiales o de catálogo",
          "{n} grafías de barrio sin revisar en la muestra o en los predios (no se adivinan; dim_ubicacion.revisado = 0)")
    anio_limite = date.today().year - int(cfg["anios_oferta_vigente"])
    for base in p["almacen"]["bases_mercado"]:
        total = _n(con, "SELECT COUNT(*) FROM fact_oferta WHERE base_mercado=? AND es_valida=1", base)
        if not total:
            continue
        sin_fecha = _n(con, "SELECT COUNT(*) FROM fact_oferta WHERE base_mercado=? AND es_valida=1 AND fecha_key=0", base)
        viejas = _n(con, "SELECT COUNT(*) FROM fact_oferta o JOIN dim_fecha d ON d.fecha_key=o.fecha_key "
                         "WHERE o.base_mercado=? AND o.es_valida=1 AND d.anio < ?", base, anio_limite)
        corrida.control(f"vigencia_ofertas_{base}", "fact_oferta", "alerta" if (sin_fecha or viejas) else "ok",
                        round((sin_fecha + viejas) / total, 4), None,
                        f"{total} válidas · {sin_fecha} sin fecha de publicación · {viejas} anteriores a {anio_limite}: "
                        "son referencia de precio pedido, no oferta vigente" if (sin_fecha or viejas)
                        else f"{total} válidas, todas con fecha reciente")
        cierres = _n(con, "SELECT COUNT(*) FROM fact_oferta o JOIN dim_tipo_oferta t ON t.tipo_oferta_key=o.tipo_oferta_key "
                          "WHERE o.base_mercado=? AND o.es_valida=1 AND t.tipo_precio='cierre'", base)
        corrida.control(f"precios_de_cierre_{base}", "fact_oferta", "alerta" if not cierres else "ok", cierres, None,
                        "Sin precios de cierre: toda la base es de precio pedido" if not cierres
                        else f"{cierres} ofertas con precio de cierre (se reportan aparte de los pedidos)")
    total_lugares = _n(con, "SELECT COUNT(*) FROM dim_lugar")
    sin_nombre = _n(con, "SELECT COUNT(*) FROM dim_lugar l JOIN dim_fuente f ON f.fuente_key=l.fuente_key "
                         "WHERE (l.nombre IS NULL OR TRIM(l.nombre)='') AND f.fuente_id <> 'google'")
    pct = round(sin_nombre / total_lugares, 4) if total_lugares else 0.0
    corrida.control("lugares_sin_nombre", "dim_lugar", "alerta" if pct > cfg["max_pct_lugares_sin_nombre"] else "ok", pct,
                    cfg["max_pct_lugares_sin_nombre"], f"{sin_nombre} de {total_lugares} lugares sin nombre")
    ambiguos = _n(con, "SELECT COUNT(DISTINCT grupo_dup) FROM fact_oferta WHERE grupo_ambiguo=1")
    aviso("duplicados_ambiguos", "fact_oferta", ambiguos, "Ningún grupo de duplicados une identificadores distintos",
          "{n} grupos de duplicados con identificadores distintos (puede ser el mismo inmueble en varias fuentes)")
