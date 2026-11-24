"""Google Maps Platform: Geocoding y Places Nearby Search (New).

Solo corre si hay GOOGLE_SERVER_KEY en `.env`. Reglas de los términos de servicio que este módulo
respeta: el `place_id` se puede guardar indefinidamente, las coordenadas como máximo 30 días y el
nombre del lugar no se guarda (viaja solo en la respuesta de la sesión).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from .. import geo
from ..curaduria import clasificacion
from .base import Ejecucion, FuenteBloqueada, pedir


def habilitado(p: dict) -> bool:
    return bool(p["secretos"]["google_servidor"]) and bool(p["fuentes"]["fuentes"]["google"].get("activa"))


def expira(p: dict) -> str:
    dias = p["reglas"]["google"]["dias_cache"]
    return (datetime.now(timezone.utc) + timedelta(days=dias)).isoformat(timespec="seconds")


async def geocodificar(cli: httpx.AsyncClient, p: dict, direccion: str) -> dict:
    if not habilitado(p):
        return {"ok": False, "motivo": "google_sin_llave"}
    try:
        r = await pedir(cli, p, "GET", p["params"]["google"]["geocode_url"], params={
            "address": direccion, "region": "co", "components": "country:CO", "language": "es",
            "key": p["secretos"]["google_servidor"]})
        data = r.json()
    except Exception as e:
        return {"ok": False, "motivo": f"google_geocoding: {type(e).__name__}"}
    if data.get("status") != "OK" or not data.get("results"):
        return {"ok": False, "motivo": f"google_geocoding: {data.get('status')}"}
    res = data["results"][0]
    loc = res["geometry"]["location"]
    avisos = []
    if res.get("partial_match"):
        avisos.append("Geocodificación parcial de Google: confirma el punto arrastrando el pin.")
    if res["geometry"].get("location_type") in ("APPROXIMATE", "GEOMETRIC_CENTER"):
        avisos.append(f"Precisión {res['geometry']['location_type']} de Google: confirma el punto en el mapa.")
    return {"ok": True, "lat": loc["lat"], "lng": loc["lng"], "origen": "google_geocoding",
            "precision": p["reglas"]["ubicacion"]["precision"]["externo"], "metodo": "Google Geocoding",
            "direccion_normalizada": res.get("formatted_address"), "place_id": res.get("place_id"),
            "avisos": avisos}


async def _nearby(cli, p, sem, tipos: list[str], lat: float, lng: float, radio: float) -> list[dict]:
    cuerpo = {
        "includedTypes": tipos, "maxResultCount": p["reglas"]["google"]["max_resultados"],
        "rankPreference": "DISTANCE", "languageCode": "es", "regionCode": "CO",
        "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radio)}},
    }
    cabeceras = {"Content-Type": "application/json", "X-Goog-Api-Key": p["secretos"]["google_servidor"],
                 "X-Goog-FieldMask": p["params"]["google"]["field_mask"]}
    async with sem:
        r = await pedir(cli, p, "POST", p["params"]["google"]["places_url"], json=cuerpo, headers=cabeceras)
    if r.status_code != 200:
        raise RuntimeError(f"Places {r.status_code}: {r.text[:200]}")
    return r.json().get("places", [])


async def barrer(cli: httpx.AsyncClient, p: dict, lat: float, lng: float,
                 radio: float) -> tuple[list[dict], list[dict], dict[str, bool], Ejecucion]:
    """Una solicitud por grupo de tipos; un grupo que llega al tope se re-barre con 7 subcírculos.

    Devuelve (items, candidatos_conjunto, saturados_por_categoria, ejecucion).
    """
    ej = Ejecucion("google", "places_nearby")
    if not habilitado(p):
        return [], [], {}, ej.cerrar("omitida", "sin GOOGLE_SERVER_KEY en .env")
    rg, tax = p["reglas"]["google"], p["taxonomia"]
    tope = rg["max_resultados"]
    sem = asyncio.Semaphore(p["params"]["google"]["concurrencia"])
    licencia = p["fuentes"]["fuentes"]["google"]["licencia"]
    vence = expira(p)
    saturados: dict[str, bool] = {}
    vistos: dict[str, dict] = {}
    llamadas = 0

    async def grupo(cat: str, tipos: list[str]) -> None:
        nonlocal llamadas
        llamadas += 1
        principal = await _nearby(cli, p, sem, tipos, lat, lng, radio)
        lotes = [principal]
        if len(principal) >= tope and rg["subdividir"]:
            subs = geo.subcirculos(lat, lng, radio, rg["traslape_subcirculo"])
            llamadas += len(subs)
            extra = await asyncio.gather(*[_nearby(cli, p, sem, tipos, a, b, r) for a, b, r in subs])
            lotes += list(extra)
            saturados[cat] = saturados.get(cat, False) or any(len(x) >= tope for x in extra)
        elif len(principal) >= tope:
            saturados[cat] = True
        else:
            saturados.setdefault(cat, False)
        for lote in lotes:
            for lugar in lote:
                pid, loc = lugar.get("id"), lugar.get("location") or {}
                if not pid or "latitude" not in loc:
                    continue
                d = geo.haversine_m(lat, lng, loc["latitude"], loc["longitude"])
                if d > radio:                     # los subcírculos se salen del círculo original
                    ej.descartados += 1
                    continue
                if pid in vistos:
                    continue
                primario = lugar.get("primaryType") or (lugar.get("types") or [None])[0]
                c, sub, conf = clasificacion.google(primario, cat)
                vistos[pid] = {
                    "fuente": "google", "capa": "places_nearby", "id_externo": pid,
                    "categoria": c, "subtipo": sub, "confianza_clasificacion": conf,
                    "categoria_original": primario,
                    "nombre_original": (lugar.get("displayName") or {}).get("text"),   # solo en sesión
                    "lat": loc["latitude"], "lng": loc["longitude"], "distancia_m": round(d, 1),
                    "geometria": "punto", "anillos": None,
                    "atributos": {"types": lugar.get("types", [])}, "banderas": [],
                    "motivo_exclusion": None, "dedupe": True, "ia": False, "en_mapa": True,
                    "licencia": licencia, "expira_en": vence,
                }

    candidatos: list[dict] = []

    async def conjunto() -> None:
        nonlocal llamadas
        llamadas += 1
        r_conj = p["reglas"]["ubicacion"]["radio_conjunto_m"]
        for lugar in await _nearby(cli, p, sem, tax["google_conjunto"], lat, lng, r_conj):
            loc = lugar.get("location") or {}
            nombre = (lugar.get("displayName") or {}).get("text")
            if not nombre or "latitude" not in loc:
                continue
            candidatos.append({"id": f"google:{lugar.get('id')}", "nombre": nombre,
                               "tipo": lugar.get("primaryType") or "conjunto", "origen": "google",
                               "distancia_m": round(geo.haversine_m(lat, lng, loc["latitude"], loc["longitude"]), 1),
                               "mismo_lote": False, "contiene_punto": False})

    tareas = [grupo(cat, tipos) for cat, grupos in tax["google"].items() for tipos in grupos] + [conjunto()]
    resultados = await asyncio.gather(*tareas, return_exceptions=True)
    ej.llamadas = llamadas
    errores = [r for r in resultados if isinstance(r, Exception)]
    items = list(vistos.values())
    ej.registros = len(items)
    if any(isinstance(e, FuenteBloqueada) for e in errores) and not items:
        return [], candidatos, saturados, ej.cerrar("bloqueado", str(errores[0])[:200])
    if errores and not items:
        return [], candidatos, saturados, ej.cerrar("error", str(errores[0])[:200])
    if errores:
        return items, candidatos, saturados, ej.cerrar("parcial", f"{len(errores)} grupos fallaron: {str(errores[0])[:140]}")
    return items, candidatos, saturados, ej.cerrar("ok")
