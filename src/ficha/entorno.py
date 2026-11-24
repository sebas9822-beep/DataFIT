"""Etapa 5 · Entorno (transporte y equipamientos) y norma urbanística (POT).

Distancias: método oficial del cálculo manual, haversine con el radio terrestre de params.yaml
(`entorno.metodo_distancia: haversine`). Como control, y por regla del proyecto, la misma medición
se repite en el CRS métrico (`crs.metrico`, EPSG:9377) y la diferencia queda en la auditoría.

El cruce con el POT es informativo: reporta el atributo de la capa entregada y no constituye
concepto jurídico ni urbanístico.
"""
from __future__ import annotations

import math

import pandas as pd

try:                                    # solo las usa el lote oficial; el motor sobre el almacén usa entorno_almacen
    import geopandas as gpd
    from pyproj import Transformer
    from shapely.geometry import Point
except ImportError:                     # pragma: no cover - entorno sin pila geográfica
    gpd = None
    Transformer = None
    Point = None

NOTA_POT = "Cruce informativo con la capa entregada. No constituye concepto jurídico ni urbanístico."


def _valor(fila, campo):
    v = fila.get(campo)
    return None if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)) else v


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float, radio_m: float) -> float:
    """Distancia en metros sobre la esfera entre dos puntos en grados decimales."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radio_m * math.asin(math.sqrt(a))


def _punto(inmueble: pd.Series, crs_origen, crs_destino) -> Point:
    """Punto del inmueble en `crs_destino` (transformación escalar explícita, sin GeoSeries de un elemento)."""
    x, y = Transformer.from_crs(crs_origen, crs_destino, always_xy=True).transform(float(inmueble["lon"]),
                                                                                  float(inmueble["lat"]))
    return Point(x, y)


def _lonlat(capa: gpd.GeoDataFrame) -> list[tuple[float, float]]:
    """(lon, lat) de cada elemento en WGS84; las áreas se reducen a un punto interior."""
    geo = capa.to_crs("EPSG:4326").geometry
    puntos = [g if g.geom_type == "Point" else g.representative_point() for g in geo]
    return [(pt.x, pt.y) for pt in puntos]


def _distancias(lat: float, lon: float, capa: gpd.GeoDataFrame, cfg: dict) -> list[float]:
    if cfg["metodo_distancia"] != "haversine":
        raise ValueError(f"entorno.metodo_distancia no soportado: {cfg['metodo_distancia']}")
    return [haversine_m(lat, lon, y, x, cfg["radio_tierra_m"]) for x, y in _lonlat(capa)]


def preparar_capas(transporte: gpd.GeoDataFrame, equipamientos: gpd.GeoDataFrame, p: dict) -> dict:
    reglas, cfg = p["reglas"]["entorno"], p["entorno"]
    estaciones = transporte[transporte[cfg["campo_sistema"]].isin(reglas["sistemas_transporte"])]
    categorias = list(reglas["categorias_equipamientos"])
    equip = equipamientos[equipamientos[cfg["campo_categoria"]].isin(categorias)]
    return {"estaciones": estaciones.reset_index(drop=True), "equipamientos": equip.reset_index(drop=True),
            "categorias": categorias}


def bloque_entorno(inmueble: pd.Series, capas: dict, p: dict) -> dict:
    reglas, cfg = p["reglas"]["entorno"], p["entorno"]
    radio = reglas["radio_busqueda_m"]
    lat, lon = float(inmueble["lat"]), float(inmueble["lon"])
    est = capas["estaciones"]
    d_est = _distancias(lat, lon, est, cfg)
    cercana = None
    if d_est:
        i = min(range(len(d_est)), key=lambda k: d_est[k])          # empate: la primera de la capa
        cercana = {"nombre": est.at[i, cfg["campo_nombre"]], "sistema": est.at[i, cfg["campo_sistema"]],
                   "distancia_m": round(d_est[i])}
    eq = capas["equipamientos"]
    d_eq = _distancias(lat, lon, eq, cfg)
    conteo = {c: 0 for c in capas["categorias"]}
    for categoria, d in zip(eq[cfg["campo_categoria"]], d_eq, strict=True):
        if d <= radio:
            conteo[categoria] += 1
    return {
        "radio_busqueda_m": radio,
        "estacion_mas_cercana": cercana,
        "estaciones_en_radio": sum(1 for d in d_est if d <= radio),
        "equipamientos_en_radio": conteo,
        "aproximado": inmueble["precision_coordenadas"] not in cfg["precision_exacta"],
        "metodo_distancia": cfg["metodo_distancia"],
    }


def control_proyectado(inmueble: pd.Series, capas: dict, entorno: dict, p: dict) -> dict:
    """La misma medición en el CRS métrico, para comparar con la oficial (haversine)."""
    crs, cfg, radio = p["crs"]["metrico"], p["entorno"], p["reglas"]["entorno"]["radio_busqueda_m"]
    punto = _punto(inmueble, p["crs"]["entrada"], crs)
    est = capas["estaciones"].to_crs(crs)
    d_est = est.geometry.distance(punto)
    eq = capas["equipamientos"].to_crs(crs)
    d_eq = eq.geometry.distance(punto)
    oficial_eq = pd.Series([haversine_m(float(inmueble["lat"]), float(inmueble["lon"]), y, x, cfg["radio_tierra_m"])
                            for x, y in _lonlat(capas["equipamientos"])])
    cercana = entorno["estacion_mas_cercana"]
    i = int(d_est.values.argmin()) if len(d_est) else None
    return {
        "crs": crs,
        "estacion_mas_cercana": None if i is None else est.at[i, cfg["campo_nombre"]],
        "distancia_estacion_m": None if i is None else round(float(d_est.iloc[i])),
        "diferencia_m": None if i is None or cercana is None else round(float(d_est.iloc[i]) - cercana["distancia_m"]),
        "estaciones_en_radio": int((d_est <= radio).sum()),
        "equipamientos_en_radio": int((d_eq <= radio).sum()),
        "equipamientos_que_cambian": int(((d_eq.values <= radio) != (oficial_eq.values <= radio)).sum()),
    }


def norma_pot(inmueble: pd.Series, pot: gpd.GeoDataFrame | None, p: dict) -> dict:
    """Estados oficiales: encontrado, fuera_de_capa, pendiente_capa, no_disponible."""
    reglas = p["reglas"]["norma"]
    if inmueble["municipio"] not in reglas["municipios_cubiertos"]:
        return {"tratamiento_pot": None, "estado": "no_disponible",
                "motivo": f"La capa POT cubre solo: {', '.join(reglas['municipios_cubiertos'])}", "nota": NOTA_POT}
    if pot is None:
        return {"tratamiento_pot": None, "estado": "pendiente_capa",
                "motivo": f"Falta el archivo {reglas['archivo_pot']}", "nota": NOTA_POT}
    punto = _punto(inmueble, p["crs"]["entrada"], pot.crs)
    hit = pot[pot.geometry.covers(punto)]
    if hit.empty:
        return {"tratamiento_pot": None, "estado": "fuera_de_capa",
                "motivo": "El punto no cae en ningún polígono", "nota": NOTA_POT}
    fila = hit.iloc[0]
    columnas = {str(c).lower(): c for c in pot.columns}
    campo = columnas.get(str(reglas["campo_tratamiento"]).lower())   # TRATAMIENTO en parámetros, tratamiento en la capa
    if campo is None:
        raise ValueError(f"La capa POT no tiene el campo '{reglas['campo_tratamiento']}' (norma.campo_tratamiento). "
                         f"Campos disponibles: {sorted(c for c in pot.columns if c != 'geometry')}")
    atributos = {c: _valor(fila, columnas[c.lower()]) for c in p["entorno"].get("campos_pot_adicionales", [])
                 if c.lower() in columnas}
    motivo = "" if len(hit) == 1 else f"El punto cae en {len(hit)} polígonos; se informa el primero"
    return {"tratamiento_pot": _valor(fila, campo), "estado": "encontrado", "motivo": motivo,
            "atributos": atributos, "campo_capa": campo, "nota": NOTA_POT}
