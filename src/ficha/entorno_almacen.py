"""Entorno y norma de la ficha calculados sobre el almacén (esquema estrella), sin capas GeoJSON.

Mismas reglas del reto (parametros.yaml › entorno y norma): estación más cercana entre los sistemas
definidos, estaciones y equipamientos dentro del radio, y tratamiento del POT como dato informativo.
Las distancias ya vienen medidas en metros desde el ETL (haversine); el control en EPSG:9377 se repite aquí.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from captura import geo

NOTA_POT = "Cruce informativo con la capa entregada. No constituye concepto jurídico ni urbanístico."


def _lugares(con: sqlite3.Connection, predio_key: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT lugar_nk, categoria, categoria_ficha, fuente, capa, nombre, lat, lng, sistema_transporte, linea, "
        "orden_fuente, distancia_m, cuenta, en_mapa, es_estacion FROM v_entorno WHERE predio_key=? "
        "ORDER BY distancia_m, orden_fuente, lugar_nk", (predio_key,)).fetchall()


def _estaciones(lugares: list, sistemas: list[str], unicas_por_nombre: bool) -> list:
    """Estaciones de los sistemas que cuentan, de la más cercana a la más lejana (empate: orden de la fuente)."""
    filas = [x for x in lugares if x["es_estacion"] and x["sistema_transporte"] in sistemas and x["distancia_m"] is not None]
    if not unicas_por_nombre:
        return filas
    vistas, unicas = set(), []
    for x in filas:                                   # una estación con varias líneas cuenta una vez por sistema
        clave = (str(x["nombre"] or "").strip().lower(), x["sistema_transporte"])
        if clave not in vistas:
            vistas.add(clave)
            unicas.append(x)
    return unicas


def bloque_entorno(con: sqlite3.Connection, predio: dict, pf: dict, estaciones_unicas: bool = False) -> tuple[dict, dict]:
    """Devuelve (bloque oficial de entorno, insumos para el control proyectado)."""
    reglas, cfg = pf["reglas"]["entorno"], pf["entorno"]
    radio = reglas["radio_busqueda_m"]
    lugares = _lugares(con, predio["predio_key"])
    estaciones = _estaciones(lugares, reglas["sistemas_transporte"], estaciones_unicas)
    cercana = None
    if estaciones:
        e = estaciones[0]
        cercana = {"nombre": e["nombre"], "sistema": e["sistema_transporte"], "distancia_m": round(e["distancia_m"])}
    conteo = {c: 0 for c in reglas["categorias_equipamientos"]}
    equipamientos = [x for x in lugares if x["cuenta"] and x["en_mapa"] and not x["es_estacion"]
                     and x["categoria_ficha"] in conteo and x["distancia_m"] is not None]
    for x in equipamientos:
        if x["distancia_m"] <= radio:
            conteo[x["categoria_ficha"]] += 1
    bloque = {
        "radio_busqueda_m": radio,
        "estacion_mas_cercana": cercana,
        "estaciones_en_radio": sum(1 for e in estaciones if e["distancia_m"] <= radio),
        "equipamientos_en_radio": conteo,
        "aproximado": predio["precision_coordenadas"] not in cfg["precision_exacta"],
        "metodo_distancia": cfg["metodo_distancia"],
    }
    return bloque, {"estaciones": estaciones, "equipamientos": equipamientos}


def control_proyectado(predio: dict, insumos: dict, entorno: dict, pf: dict) -> dict | None:
    """La misma medición en el CRS métrico (EPSG:9377), para compararla con la oficial (haversine)."""
    if not pf["entorno"]["control_proyectado"] or predio.get("lat") is None:
        return None
    crs, radio = pf["crs"]["metrico"], pf["reglas"]["entorno"]["radio_busqueda_m"]

    def proyectada(x) -> float | None:
        if x["lat"] is None or x["lng"] is None:
            return None
        return geo.distancia_9377_m(predio["lat"], predio["lng"], x["lat"], x["lng"])

    est = [(proyectada(e), e) for e in insumos["estaciones"]]
    est = [(d, e) for d, e in est if d is not None]
    eq = [(proyectada(x), x) for x in insumos["equipamientos"]]
    eq = [(d, x) for d, x in eq if d is not None]
    if not est and not eq:
        return None
    cercana_ficha = entorno["estacion_mas_cercana"]
    mejor = min(est, key=lambda par: par[0]) if est else None
    return {
        "crs": crs,
        "estacion_mas_cercana": mejor[1]["nombre"] if mejor else None,
        "distancia_estacion_m": round(mejor[0]) if mejor else None,
        "diferencia_m": round(mejor[0] - cercana_ficha["distancia_m"]) if mejor and cercana_ficha else None,
        "estaciones_en_radio": sum(1 for d, _ in est if d <= radio),
        "equipamientos_en_radio": sum(1 for d, _ in eq if d <= radio),
        "equipamientos_que_cambian": sum(1 for d, x in eq if (d <= radio) != (x["distancia_m"] <= radio)),
    }


# ---------------------------------------------------------------------------------------------
# Norma
# ---------------------------------------------------------------------------------------------
def _en_anillo(lon: float, lat: float, anillo: list) -> bool:
    dentro, j = False, len(anillo) - 1
    for i in range(len(anillo)):
        (xi, yi), (xj, yj) = anillo[i][:2], anillo[j][:2]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            dentro = not dentro
        j = i
    return dentro


def norma_desde_capa(predio: dict, pf: dict) -> dict:
    """Regla del reto: tratamiento de la capa POT entregada como archivo (inmuebles de la muestra oficial)."""
    reglas = pf["reglas"]["norma"]
    if predio["municipio"] not in reglas["municipios_cubiertos"]:
        return {"tratamiento_pot": None, "estado": "no_disponible",
                "motivo": f"La capa POT cubre solo: {', '.join(reglas['municipios_cubiertos'])}", "nota": NOTA_POT}
    ruta = Path(pf["rutas"]["pot_geojson"])
    if not ruta.exists():
        return {"tratamiento_pot": None, "estado": "pendiente_capa",
                "motivo": f"Falta el archivo {reglas['archivo_pot']}", "nota": NOTA_POT}
    capa = json.loads(ruta.read_text(encoding="utf-8"))
    if capa.get("crs") and "4326" not in json.dumps(capa["crs"]) and "CRS84" not in json.dumps(capa["crs"]):
        raise ValueError(f"La capa POT declara un CRS distinto de EPSG:4326 ({capa['crs']}): reproyéctela antes de cruzarla")
    campo = str(reglas["campo_tratamiento"]).lower()
    for f in capa.get("features") or []:
        g = f.get("geometry") or {}
        poligonos = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) \
            if g.get("type") == "MultiPolygon" else []
        for exterior, *huecos in poligonos:
            if _en_anillo(predio["lng"], predio["lat"], exterior) and not any(_en_anillo(predio["lng"], predio["lat"], h) for h in huecos):
                props = {str(k).lower(): v for k, v in (f.get("properties") or {}).items()}
                if campo not in props:
                    raise ValueError(f"La capa POT no tiene el campo '{reglas['campo_tratamiento']}' (norma.campo_tratamiento)")
                atributos = {c: props.get(c.lower()) for c in pf["entorno"].get("campos_pot_adicionales", []) if c.lower() in props}
                return {"tratamiento_pot": props[campo], "estado": "encontrado", "motivo": "", "atributos": atributos,
                        "campo_capa": reglas["campo_tratamiento"], "nota": NOTA_POT}
    return {"tratamiento_pot": None, "estado": "fuera_de_capa", "motivo": "El punto no cae en ningún polígono", "nota": NOTA_POT}


def norma_desde_captura(predio: dict) -> dict:
    """Tratamiento que la captura leyó del servidor de mapas de la Alcaldía para el punto del predio."""
    if predio.get("en_cobertura") == 0:
        return {"tratamiento_pot": None, "estado": "no_disponible",
                "motivo": "El punto está fuera de Medellín: la capa del POT consultada cubre solo Medellín", "nota": NOTA_POT}
    if predio.get("pot_tratamiento"):
        atributos = {"codigo_tratamiento": predio.get("pot_codigo"), "categoria_tratamiento": predio.get("pot_categoria"),
                     "clase_suelo": predio.get("pot_clase_suelo"), "uso_general_del_suelo": predio.get("pot_uso_general")}
        return {"tratamiento_pot": predio["pot_tratamiento"], "estado": "encontrado", "motivo": "",
                "atributos": {k: v for k, v in atributos.items() if v}, "campo_capa": "tratamiento",
                "fuente": "Capa de tratamientos urbanos del POT · servidor de mapas de la Alcaldía de Medellín (leída en la captura)",
                "nota": NOTA_POT}
    return {"tratamiento_pot": None, "estado": "pendiente_capa",
            "motivo": "La capa del POT no respondió durante la captura del predio", "nota": NOTA_POT}
