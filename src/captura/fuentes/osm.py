"""OpenStreetMap: barrido con Overpass y geocodificación de respaldo con Nominatim.

Licencia ODbL: se puede guardar con atribución. Uso justo de las instancias públicas: pocas
consultas, agente identificado y Nominatim a no más de una consulta por segundo.
"""
from __future__ import annotations

import time

import httpx

from .. import geo
from ..curaduria import clasificacion
from .base import Ejecucion, FuenteBloqueada, pedir

_ATRIBUTOS = ("operator", "network", "brand", "addr:street", "addr:housenumber", "ref", "line")
_CLAVES = ("amenity", "shop", "leisure", "tourism", "railway", "aerialway", "public_transport",
           "highway", "office", "building", "landuse")


def consulta_overpass(p: dict, lat: float, lng: float, radio: float) -> str:
    """Arma la consulta desde la taxonomía: cambiar `taxonomia.yaml` cambia lo que se pide."""
    tax = p["taxonomia"]
    a = f"around:{round(radio * p['params']['red']['margen_radio'])},{lat:.6f},{lng:.6f}"
    por_clave: dict[str, set[str]] = {}
    for clave, valor, _, _ in tax["osm"]:
        por_clave.setdefault(clave, set()).add(valor)
    lineas = []
    for clave, valores in por_clave.items():
        if "*" in valores:
            lineas.append(f'  nwr({a})["{clave}"];')
        else:
            lineas.append(f'  nwr({a})["{clave}"~"^({"|".join(sorted(valores))})$"];')
    for clave, valor, _, _ in tax["osm_residencial"]:
        lineas.append(f'  nwr({a})["{clave}"="{valor}"]["name"];')
    t = p["params"]["osm"]["timeout_consulta_s"]
    return (f"[out:json][timeout:{t}];\n(\n" + "\n".join(lineas) + "\n);\nout center tags;\n"
            f"is_in({lat:.6f},{lng:.6f})->.a;\narea.a[\"landuse\"=\"residential\"][\"name\"];\nout tags;")


async def barrer(cli: httpx.AsyncClient, p: dict, lat: float, lng: float,
                 radio: float) -> tuple[list[dict], list[dict], Ejecucion]:
    """Devuelve (items, candidatos_conjunto, ejecucion)."""
    ej = Ejecucion("osm", "overpass")
    licencia = p["fuentes"]["fuentes"]["osm"]["licencia"]
    consulta = consulta_overpass(p, lat, lng, radio)
    cfg = p["params"]["osm"]
    data, ultimo, inicio = None, "", time.monotonic()
    for url in cfg["overpass_urls"]:
        restante = cfg["presupuesto_total_s"] - (time.monotonic() - inicio)
        if restante <= 0:
            ultimo = ultimo or "se agotó el tiempo de espera"
            break
        ej.llamadas += 1
        try:
            r = await pedir(cli, p, "POST", url, data={"data": consulta},
                            reintentos=cfg["reintentos_por_instancia"],
                            timeout=httpx.Timeout(min(cfg["timeout_http_s"], restante),
                                                  connect=cfg["timeout_conexion_s"]))
            if r.status_code == 200:
                data = r.json()
                break
            ultimo = f"HTTP {r.status_code}"
        except FuenteBloqueada as e:
            ultimo = str(e)                      # se prueba la instancia alterna, sin insistir en esta
        except Exception as e:
            ultimo = f"{type(e).__name__}: {str(e)[:120]}"
    if data is None:
        return [], [], ej.cerrar("error", f"Overpass no respondió ({ultimo})")
    elementos = data.get("elements") if isinstance(data, dict) else None
    if elementos is None:
        return [], [], ej.cerrar("fallo_estructura", "respuesta sin 'elements'")

    radio_conj = p["reglas"]["ubicacion"]["radio_conjunto_m"]
    items, candidatos = [], []
    for el in elementos:
        tags = el.get("tags") or {}
        if el.get("type") == "area":
            if tags.get("name"):
                candidatos.append({"id": f"osm:area:{el.get('id')}", "nombre": tags["name"],
                                   "tipo": "urbanizacion", "origen": "osm", "distancia_m": 0.0,
                                   "mismo_lote": False, "contiene_punto": True})
            continue
        la = el.get("lat", (el.get("center") or {}).get("lat"))
        ln = el.get("lon", (el.get("center") or {}).get("lon"))
        clase = clasificacion.osm(tags, p["taxonomia"])
        if clase is None or la is None or ln is None:
            continue
        d = geo.haversine_m(lat, lng, la, ln)
        if d > radio:
            ej.descartados += 1
            continue
        cat, sub, conf = clase
        original = next((f"{k}={tags[k]}" for k in _CLAVES if k in tags), None)
        items.append({
            "fuente": "osm", "capa": "overpass", "id_externo": f"{el['type']}/{el['id']}",
            "categoria": cat, "subtipo": sub, "confianza_clasificacion": conf, "categoria_original": original,
            "nombre_original": tags.get("name"),
            "lat": round(la, 7), "lng": round(ln, 7), "distancia_m": round(d, 1),
            "geometria": "punto", "anillos": None,
            "atributos": {k: tags[k] for k in _ATRIBUTOS if k in tags},
            "banderas": [] if el["type"] == "node" else ["centro_de_via"],
            "motivo_exclusion": None, "dedupe": True, "ia": True, "en_mapa": True,
            "licencia": licencia, "expira_en": None,
        })
        if cat == "residencial" and tags.get("name") and d <= radio_conj:
            candidatos.append({"id": f"osm:{el['type']}/{el['id']}", "nombre": tags["name"], "tipo": sub,
                               "origen": "osm", "distancia_m": round(d, 1), "mismo_lote": False,
                               "contiene_punto": False})
    ej.registros = len(items)
    if ej.descartados:
        ej.mensaje = f"{ej.descartados} fuera del radio"
    return items, candidatos, ej.cerrar("ok")


async def geocodificar_nominatim(cli: httpx.AsyncClient, p: dict, texto: str) -> dict:
    """Respaldo (P3) cuando la dirección no está en la nomenclatura oficial."""
    lim = p["reglas"]["ubicacion"]["limites_medellin"]
    params = {"q": f"{texto}, Medellín, Colombia" if "medell" not in texto.lower() else texto,
              "format": "jsonv2", "countrycodes": "co", "limit": 1, "accept-language": "es",
              "viewbox": f"{lim['lng'][0]},{lim['lat'][1]},{lim['lng'][1]},{lim['lat'][0]}", "bounded": 1}
    try:
        r = await pedir(cli, p, "GET", p["params"]["osm"]["nominatim_url"], params=params)
        datos = r.json()
    except Exception as e:
        return {"ok": False, "motivo": f"nominatim: {type(e).__name__}"}
    if not datos or not isinstance(datos, list):
        return {"ok": False, "motivo": "nominatim_sin_resultado"}
    d = datos[0]
    return {"ok": True, "lat": float(d["lat"]), "lng": float(d["lon"]), "origen": "nominatim",
            "precision": p["reglas"]["ubicacion"]["precision"]["externo"],
            "metodo": "Nominatim (OpenStreetMap), aproximada", "direccion_normalizada": d.get("display_name"),
            "fuente": "© colaboradores de OpenStreetMap (ODbL)"}
