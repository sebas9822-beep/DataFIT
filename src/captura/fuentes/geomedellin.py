"""Barrido de entorno sobre las capas oficiales definidas en `config/fuentes.yaml`.

Cada capa activa es una consulta ArcGIS por distancia al punto. Lo que la capa trae se convierte
al registro común del entorno (o a `ofertas` para la capa del OIME). Los campos de contacto se
descartan antes de construir el registro.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from .. import geo
from ..curaduria import clasificacion
from ..curaduria.normalizacion import a_etiqueta, es_vacio
from . import arcgis
from .base import Ejecucion, FuenteBloqueada, limpiar_personales, primero


MOTIVOS_CORTE = {"tope_registros": "se alcanzó el tope de registros de la capa",
                 "tope_paginas": "se alcanzó el tope de solicitudes por capa",
                 "tope_del_servidor": "el servidor recortó la respuesta",
                 "error_servidor": "el servidor dejó de responder la consulta"}
ILEGIBLE = "atributos_ilegibles_en_fuente"


def _fecha(valor) -> str | None:
    """Fechas de ArcGIS (milisegundos desde 1970) a ISO."""
    if valor in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(valor) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(valor)[:10]


def _clasificar(capa: dict, a: dict, tax: dict) -> tuple[str, str | None, str, str | None]:
    """(categoria, subtipo, confianza, categoria_original) según el clasificador de la capa."""
    modo = capa.get("clasificador", "fija")
    if modo == "geomed_equipamientos":
        cat, sub, conf = clasificacion.geomed_equipamiento(a.get("tipo"), a.get("categoria"), tax)
        return cat, sub, conf, a.get("tipo")
    if modo == "toponimia":
        cat, sub, conf = clasificacion.toponimia(a.get("tipo_sitio"), tax)
        return cat, sub, conf, str(a.get("tipo_sitio"))
    if modo == "ciiu":
        cat, sub, conf = clasificacion.ciiu(a.get("codigociiu"), a.get("grupo_actividad"), tax)
        return cat, sub, conf, str(a.get("codigociiu") or "")
    if modo == "estaciones":
        cat, sub, conf = clasificacion.estacion(a.get("linea"), a.get("tipo_est"), tax)
        return cat, sub, conf, str(a.get("linea") or "")
    original = primero(a, capa.get("subtipo", []))
    sub = a_etiqueta(original) or capa.get("subtipo_defecto")
    return capa["categoria"], sub, "alta", None if original is None else str(original)


def _a_item(capa: dict, feature: dict, p: dict, lat0: float, lng0: float, licencia: str) -> dict:
    prohibidos = {c.lower() for c in p["reglas"]["datos_personales"]["campos_prohibidos"]}
    a = limpiar_personales(arcgis.atributos(feature), prohibidos)
    g = arcgis.geometria(feature)
    if g is None and a.get("latitud") is not None and a.get("longitud") is not None:
        g = {"tipo": "punto", "lat": float(a["latitud"]), "lng": float(a["longitud"]), "anillos": None}
    clave = primero(a, capa.get("clave", ["objectid"]))
    if clave is None:
        clave = a.get("objectid")
    cat, sub, conf, original = _clasificar(capa, a, p["taxonomia"])
    nombre = primero(a, capa.get("nombre", []))
    atributos = {}
    for k in capa.get("atributos", []):
        if a.get(k) not in (None, ""):
            atributos[k] = _fecha(a.get(k)) if "fecha" in k else a.get(k)
    item = {
        "fuente": capa["fuente"], "capa": capa["id"], "id_externo": f"{capa['capa']}:{clave}",
        "categoria": cat, "subtipo": sub, "confianza_clasificacion": conf, "categoria_original": original,
        "nombre_original": None if es_vacio(nombre) else str(nombre),
        "lat": None, "lng": None, "distancia_m": None, "geometria": None, "anillos": None,
        "atributos": atributos, "banderas": [], "motivo_exclusion": None,
        "dedupe": bool(capa.get("dedupe", True)), "ia": bool(capa.get("ia", True)),
        "en_mapa": bool(capa.get("mapa", True)), "licencia": licencia, "expira_en": None,
    }
    if capa.get("fecha") and a.get(capa["fecha"]):
        item["atributos"]["fecha_dato"] = _fecha(a.get(capa["fecha"]))
    if a.get("_solo_llave"):
        item["banderas"].append(ILEGIBLE)
    if g is None:
        item["motivo_exclusion"] = "sin_coordenadas"
        return item
    item.update(lat=round(g["lat"], 7), lng=round(g["lng"], 7), geometria=g["tipo"], anillos=g["anillos"])
    if g["tipo"] == "poligono":
        item["distancia_m"] = round(geo.distancia_punto_poligono_m(lat0, lng0, g["anillos"]), 1)
        item["atributos"]["dist_centroide_m"] = round(geo.haversine_m(lat0, lng0, g["lat"], g["lng"]), 1)
        item["banderas"].append("centroide_poligono")
    else:
        item["distancia_m"] = round(geo.haversine_m(lat0, lng0, g["lat"], g["lng"]), 1)
    if capa.get("clasificador") == "ciiu":
        sin_local = {n.upper() for n in p["reglas"]["establecimientos"]["nombres_sin_local"]}
        if (item["nombre_original"] or "").strip().upper() in sin_local:
            item["motivo_exclusion"] = p["reglas"]["establecimientos"]["motivo_sin_local"]
            item["nombre_original"] = None
        grupo = str(a.get("grupo_actividad")).zfill(2) if a.get("grupo_actividad") else None
        item["atributos"]["grupo"] = p["taxonomia"]["grupos_actividad"].get(grupo) if grupo else None
    return item


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _entero(v):
    f = _num(v)
    return int(f) if f is not None else None


def _a_oferta(capa: dict, feature: dict, p: dict, lat0: float, lng0: float, licencia: str) -> dict:
    """Capa de ofertas del OIME (investigaciones de internet) al registro de `ofertas`.

    Precio pedido (nunca de cierre). El área preferida es la privada; si falta se usa la total y
    queda marcado. Nada se descarta: lo inválido lleva `motivo_exclusion`.
    """
    r = p["reglas"]["ofertas"]
    a = arcgis.atributos(feature)
    g = arcgis.geometria(feature)
    lat = g["lat"] if g else _num(a.get("latitude"))
    lng = g["lng"] if g else _num(a.get("longitude"))
    privada, total = _num(a.get("private_area_m2")), _num(a.get("total_area_m2"))
    privada = privada if privada and privada > 0 else None
    total = total if total and total > 0 else None
    banderas: list[str] = ["oferta_historica"]
    if r["preferencia_area"] == "privada" and privada:
        area, clase = privada, "privada"
    elif total:
        area, clase = total, "total"
        banderas.append("area_total_solo")
    else:
        area, clase = privada, ("privada" if privada else None)
    precio = _num(a.get("price"))
    precio = precio if precio and precio > 0 else None
    motivo = None
    amin, amax = r["area_valida_m2"]
    if a.get("_solo_llave"):
        banderas.append(ILEGIBLE)
        motivo = ILEGIBLE
    elif lat is None or lng is None:
        motivo = "sin_coordenadas"
    elif precio is None:
        motivo = "sin_precio"
    elif area is None:
        motivo = "sin_area"
    elif not (amin <= area <= amax):
        motivo = "area_fuera_de_rango"
    return {
        "fuente": capa["fuente"], "capa": capa["id"],
        "id_externo": f"{capa['capa']}:{a.get('id') or a.get('objectid')}",
        "operacion": r["operaciones"].get(str(a.get("commercial_type") or "").upper()),
        "tipo_inmueble": a_etiqueta(a.get("tipo_inmueble") or a.get("type")),
        "estado_publicado": a_etiqueta(a.get("status")),
        "estrato": _entero(a.get("stratum")),
        "area_privada": privada, "area_total": total, "area_usada": area, "area_tipo_usada": clase,
        "antiguedad": None if es_vacio(a.get("age")) else str(a.get("age")),
        "precio": precio, "tipo_precio": r["tipo_precio"],
        "administracion": _num(a.get("administration")),
        "habitaciones": _entero(a.get("bedrooms_number")), "banos": _entero(a.get("bathrooms_number")),
        "parqueaderos": _entero(a.get("parking_number")) or _entero(a.get("garages_number")),
        "piso": _entero(a.get("floor_number")),
        "precio_m2": round(precio / area) if precio and area else None,
        "lat": lat, "lng": lng,
        "distancia_m": round(geo.haversine_m(lat0, lng0, lat, lng), 1) if lat is not None and lng is not None else None,
        "barrio_cod": None if es_vacio(a.get("codigo_barrio")) else str(a.get("codigo_barrio")),
        "barrio_publicado": None if es_vacio(a.get("original_neighborhood")) else str(a.get("original_neighborhood")),
        "anio": _entero(a.get("year")), "fecha_publicacion": _fecha(a.get("created_at")),
        "url": None if es_vacio(a.get("link")) else str(a.get("link")),
        "origen_publicacion": None if es_vacio(a.get("source_name")) else str(a.get("source_name")),
        "precision_geo": "P2" if lat is not None else "P5",
        "banderas": banderas, "motivo_exclusion": motivo,
        "atributos": {"ascensor": _entero(a.get("elevator")), "categoria": a.get("category"),
                      "codigo_origen": a.get("code")},
        "licencia": licencia,
    }


async def _barrer_capa(cli, p, capa: dict, lat: float, lng: float, radio: float,
                       sem: asyncio.Semaphore) -> tuple[list[dict], list[dict], Ejecucion]:
    ej = Ejecucion(capa["fuente"], capa["id"])
    licencia = p["fuentes"]["fuentes"][capa["fuente"]]["licencia"]
    campos = ",".join(capa["campos"]) if capa.get("campos") else "*"
    try:
        async with sem:
            feats, ej.llamadas, corte = await arcgis.consultar(
                cli, p, capa["servicio"], capa["capa"], punto=(lat, lng),
                distancia_m=round(radio * p["params"]["red"]["margen_radio"]),
                donde=capa.get("donde", "1=1"), campos=campos, max_registros=capa.get("max_registros"))
    except FuenteBloqueada as e:
        return [], [], ej.cerrar("bloqueado", str(e))
    except Exception as e:
        return [], [], ej.cerrar("error", f"{type(e).__name__}: {str(e)[:160]}")
    if corte.startswith("error_servidor") and not feats:
        return [], [], ej.cerrar("error", corte)

    items, ofertas = [], []
    if capa.get("destino") == "ofertas":
        todas = [_a_oferta(capa, f, p, lat, lng, licencia) for f in feats]
        ofertas = [o for o in todas if o["distancia_m"] is None or o["distancia_m"] <= radio]
        ej.registros, ej.descartados = len(ofertas), len(todas) - len(ofertas)
    else:
        unico, vistos, repetidos = capa.get("unico_por"), set(), 0
        for f in feats:
            if unico:
                k = arcgis.atributos(f).get(unico)
                if k in vistos:
                    repetidos += 1
                    continue
                vistos.add(k)
            it = _a_item(capa, f, p, lat, lng, licencia)
            if it["distancia_m"] is not None and it["distancia_m"] > radio:
                ej.descartados += 1               # tocaba el filtro del servidor pero queda fuera del radio
                continue
            items.append(it)
        ej.registros = len(items)
        if repetidos:
            ej.mensaje = f"{repetidos} filas repetidas por {unico} agrupadas"
    if ej.descartados:
        ej.mensaje = (ej.mensaje + " · " if ej.mensaje else "") + f"{ej.descartados} fuera del radio"
    ilegibles = sum(1 for f in feats if arcgis.atributos(f).get("_solo_llave"))
    if ilegibles:
        ej.mensaje = (ej.mensaje + " · " if ej.mensaje else "") + (
            f"{ilegibles} con atributos ilegibles en la fuente (se guardan con su ubicación y quedan marcados)")
    if corte:
        return items, ofertas, ej.cerrar("parcial", f"lectura incompleta ({MOTIVOS_CORTE.get(corte.split(':')[0], corte)})")
    return items, ofertas, ej.cerrar("ok")


async def barrer(cli: httpx.AsyncClient, p: dict, lat: float, lng: float, radio: float,
                 fuentes_activas: set[str], avance=None) -> tuple[list[dict], list[dict], list[Ejecucion]]:
    """Recorre las capas activas de las fuentes pedidas. Devuelve (items, ofertas, ejecuciones)."""
    sem = asyncio.Semaphore(p["params"]["arcgis"]["concurrencia"])
    capas = [c for c in p["fuentes"]["capas_entorno"]
             if c.get("activa") and c["fuente"] in fuentes_activas
             and p["fuentes"]["fuentes"].get(c["fuente"], {}).get("activa")]

    async def una(c):
        r = await _barrer_capa(cli, p, c, lat, lng, radio, sem)
        if avance:
            avance(c, r[2])
        return r

    resultados = await asyncio.gather(*[una(c) for c in capas])
    items = [i for r in resultados for i in r[0]]
    ofertas = [o for r in resultados for o in r[1]]
    return items, ofertas, [r[2] for r in resultados]
