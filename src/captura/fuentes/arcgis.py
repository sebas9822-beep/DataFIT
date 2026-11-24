"""Cliente genérico de servicios ArcGIS REST (servidor de mapas de la Alcaldía de Medellín).

Se consulta en EPSG:4326 (`inSR`/`outSR`): el servidor reproyecta desde su sistema nativo, EPSG:9377.
"""
from __future__ import annotations

import asyncio

import httpx

from .. import geo
from .base import pedir


def url_capa(p: dict, servicio: str, capa: int) -> str:
    return f"{p['params']['arcgis']['base']}/{servicio}/MapServer/{capa}/query"


async def _una_pagina(cli, p, url: str, params: dict, reintentos: int = 0) -> dict:
    """Una solicitud. Un error que el servidor devuelve dentro del JSON se reintenta solo si se pide."""
    red = p["params"]["red"]
    for intento in range(reintentos + 1):
        r = await pedir(cli, p, "GET", url, params=params)
        data = r.json()
        if "error" not in data:
            return data
        if intento < reintentos:
            await asyncio.sleep(red["espera_base_s"] * (2 ** intento))
    raise RuntimeError(f"ArcGIS: {str(data['error'])[:220]}")


def _oid(feature: dict):
    for k, v in (feature.get("attributes") or {}).items():
        if k.lower() == "objectid":
            return v
    return None


async def consultar(cli: httpx.AsyncClient, p: dict, servicio: str, capa: int, *,
                    punto: tuple[float, float] | None = None, distancia_m: float | None = None,
                    donde: str = "1=1", campos: str = "*", geometria: bool = True,
                    max_registros: int | None = None) -> tuple[list[dict], int, str]:
    """Devuelve (features, llamadas, corte). `punto` es (lat, lng).

    `corte` es "" cuando se leyó todo; si no, dice por qué quedó incompleto: tope_del_servidor,
    tope_registros, tope_paginas o error_servidor.

    Sin `distancia_m` la consulta es punto-en-polígono (o por atributo si no hay punto) y no pagina.
    Con `distancia_m` trae todo lo que toque el círculo, paginando por llave (objectid > último).

    Dos comportamientos del servidor real que esta función absorbe (verificados el 21-nov-2026):
    - `resultOffset` cuenta las filas ANTES del filtro exacto de distancia: cada página trae menos
      filas de las pedidas (921 de 1000). Avanzar el desplazamiento por las filas recibidas repite
      registros; avanzar por la llave no repite ni salta, sea cual sea el tope de página del servidor.
    - Una fila con un valor que el servidor no sabe escribir (una fecha inválida, por ejemplo) hace
      fallar la página entera. La ventana se va partiendo hasta aislar esa fila; de ella se trae solo
      la llave y la geometría, marcada con `_solo_llave`, y la lectura sigue. No se pierde en silencio.
    """
    cfg = p["params"]["arcgis"]
    pagina = cfg["tamano_pagina"]
    if campos != "*" and "objectid" not in [c.strip().lower() for c in campos.split(",")]:
        campos = "objectid," + campos
    base = {"where": donde, "outFields": campos, "returnGeometry": "true" if geometria else "false",
            "outSR": 4326, "f": "json"}
    if punto is not None:
        lat, lng = punto
        base.update({"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
                     "spatialRel": "esriSpatialRelIntersects"})
        if distancia_m:
            base.update({"distance": distancia_m, "units": "esriSRUnit_Meter"})
    url = url_capa(p, servicio, capa)

    if not distancia_m and not max_registros:
        data = await _una_pagina(cli, p, url, base)
        return data.get("features", []), 1, "tope_del_servidor" if data.get("exceededTransferLimit") else ""

    features: list[dict] = []
    vistos: set = set()
    reintentos = p["params"]["red"]["reintentos"]
    ultimo, salto, llamadas, corte, tam, buenas = None, 0, 0, "", pagina, 0
    while True:
        if llamadas >= cfg["max_paginas"]:
            corte = "tope_paginas"
            break
        params = dict(base, resultRecordCount=tam, orderByFields="objectid")
        if ultimo is not None:
            params["where"] = f"({donde}) AND objectid > {ultimo}"
        if salto:
            params["resultOffset"] = salto
        llamadas += 1
        aislada = ilegible = False
        try:
            # El primer fallo de una ventana completa puede ser pasajero: se reintenta. Al partirla, no.
            data = await _una_pagina(cli, p, url, params, reintentos if llamadas > 1 and tam == pagina else 0)
            buenas += 1
        except RuntimeError as e:
            buenas = 0
            if llamadas == 1:                            # ¿la capa no admite paginación? una sola lectura
                llamadas += 1
                try:
                    data = await _una_pagina(cli, p, url, base)
                    return (data.get("features", []), llamadas,
                            "tope_del_servidor" if data.get("exceededTransferLimit") else "")
                except RuntimeError:
                    pass                                 # tampoco: el problema está en alguna fila
            if tam > 1:
                tam = max(1, tam // 2)                   # alguna fila de la ventana no se deja leer
                continue
            # Ventana de una fila que falla. El servidor lee también la fila siguiente (para saber si
            # hay más), así que la ilegible puede ser esta o la que sigue: se pide la llave y se
            # comprueba esta fila por sí sola.
            llamadas += 1
            try:
                data = await _una_pagina(cli, p, url, dict(params, outFields="objectid"))
            except RuntimeError:
                corte = f"error_servidor: {str(e)[:120]}"
                break
            aislada, revisadas = True, []
            for f in data.get("features", []):
                k, completa = _oid(f), None
                if k is not None:
                    llamadas += 1
                    try:
                        sola = await _una_pagina(cli, p, url, dict(base, where=f"({donde}) AND objectid = {k}"))
                        completa = next(iter(sola.get("features") or []), None)
                    except RuntimeError:
                        completa = None
                if completa is None:
                    f.setdefault("attributes", {})["_solo_llave"] = True
                    ilegible = True
                    revisadas.append(f)
                else:
                    revisadas.append(completa)
            data = dict(data, features=revisadas)
        lote = data.get("features", [])
        llaves = []
        for f in lote:
            k = _oid(f)
            if k is not None:
                llaves.append(k)
                if k in vistos:
                    continue
                vistos.add(k)
            features.append(f)
        if not data.get("exceededTransferLimit"):
            break
        if max_registros and len(features) >= max_registros:
            features, corte = features[:max_registros], "tope_registros"
            break
        if llaves and (ultimo is None or max(llaves) > ultimo):
            ultimo, salto = max(llaves), 0
        else:
            salto += tam           # ventana sin filas dentro del círculo (o sin llave): se avanza por posición
        # Se vuelve al tamaño normal cuando la fila ilegible quedó atrás.
        if ilegible or (not aislada and tam < pagina and (tam == 1 or buenas * tam >= pagina)):
            tam, buenas = pagina, 0
        await asyncio.sleep(cfg["pausa_paginas_s"])
    return features, llamadas, corte


def atributos(feature: dict) -> dict:
    """Atributos con las claves en minúscula."""
    return {str(k).lower(): v for k, v in (feature.get("attributes") or {}).items()}


def geometria(feature: dict) -> dict | None:
    """Punto representativo y anillos de una geometría Esri JSON.

    Devuelve {tipo, lat, lng, anillos} o None. Para polígonos el punto es el centroide del
    anillo mayor; los anillos se conservan para medir la distancia al borde.
    """
    g = feature.get("geometry") or {}
    if g.get("x") is not None and g.get("y") is not None:
        return {"tipo": "punto", "lat": float(g["y"]), "lng": float(g["x"]), "anillos": None}
    if g.get("rings"):
        anillos = [a for a in g["rings"] if len(a) >= 3]
        if not anillos:
            return None
        mayor = max(anillos, key=geo.area_anillo)
        lng, lat = geo.centroide_anillo(mayor)
        return {"tipo": "poligono", "lat": lat, "lng": lng, "anillos": anillos}
    if g.get("paths"):
        pts = [pt for camino in g["paths"] for pt in camino]
        if not pts:
            return None
        medio = pts[len(pts) // 2]
        return {"tipo": "linea", "lat": float(medio[1]), "lng": float(medio[0]), "anillos": None}
    return None
