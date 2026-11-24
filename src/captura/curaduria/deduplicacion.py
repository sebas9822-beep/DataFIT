"""Enlace de duplicados entre fuentes: ¿es el mismo lugar reportado dos veces?

No borra nada: todos los registros se conservan y comparten `entidad_id`. Uno queda como
maestro y los demás guardan con quién coincidieron, con qué regla y con qué puntaje.
Los pares dudosos van a revisión (Claude o una persona), nunca se fusionan solos.
"""
from __future__ import annotations

import math

from rapidfuzz import fuzz

from .. import geo


def _dist_par(a: dict, b: dict, xa, xb) -> float:
    """Distancia entre dos registros; si uno es polígono se mide al borde (0 si el otro está adentro)."""
    if a.get("anillos") and not b.get("anillos"):
        return geo.distancia_punto_poligono_m(b["lat"], b["lng"], a["anillos"])
    if b.get("anillos") and not a.get("anillos"):
        return geo.distancia_punto_poligono_m(a["lat"], a["lng"], b["anillos"])
    if a.get("anillos") and b.get("anillos"):
        return min(geo.distancia_punto_poligono_m(b["lat"], b["lng"], a["anillos"]),
                   geo.distancia_punto_poligono_m(a["lat"], a["lng"], b["anillos"]))
    return math.hypot(xa[0] - xb[0], xa[1] - xb[1])


def similitud_nombres(ka: str, kb: str) -> float:
    """Parecido 0-100 entre dos claves de nombre.

    Promedia dos medidas: una perdona palabras de más ("San José" dentro de "San José de la Salle")
    y la otra las castiga. Así un nombre contenido en otro queda en revisión, no en fusión automática.
    """
    return (fuzz.token_set_ratio(ka, kb) + fuzz.token_sort_ratio(ka, kb)) / 2


def puntaje_par(a: dict, b: dict, dist: float, r: dict) -> tuple[float, str] | None:
    """Puntaje 0-1 de "mismo lugar" y la regla que lo produjo. None = no son candidatos."""
    if (a["fuente"], a["capa"]) == (b["fuente"], b["capa"]) or a["categoria"] != b["categoria"]:
        return None
    if dist > r["distancia_max_m"]:
        return None
    ka, kb = a.get("nombre_clave") or "", b.get("nombre_clave") or ""
    if ka and kb:
        sim = similitud_nombres(ka, kb)
        if sim < r["similitud_minima_nombre"]:
            return None
        plena, maxima = r["distancia_plena_m"], r["distancia_max_m"]
        s_dist = 1.0 if dist <= plena else max(0.0, 1 - (dist - plena) / (maxima - plena))
        return round(r["peso_nombre"] * sim / 100 + r["peso_distancia"] * s_dist, 3), "nombre+distancia"
    sn = r["sin_nombre"]
    if dist <= sn["distancia_max_m"] and a.get("subtipo") and a.get("subtipo") == b.get("subtipo"):
        return float(sn["puntaje"]), "sin_nombre+distancia"
    return None


def _elegir_maestro(miembros: list[dict], r: dict) -> dict:
    orden = {f: k for k, f in enumerate(r["prioridad_fuente"])}
    return min(miembros, key=lambda it: (orden.get(it["fuente"], len(orden)),
                                         0 if it.get("nombre") else 1,
                                         it.get("distancia_m") if it.get("distancia_m") is not None else math.inf))


def _marcar_grupo(miembros: list[dict], r: dict) -> None:
    maestro = _elegir_maestro(miembros, r)
    for it in miembros:
        it["entidad_id"] = "E:" + maestro["ref"]
        it["es_maestro"] = int(it is maestro)
        it["coincide_con"] = None if it is maestro else maestro["ref"]


def enlazar(registros: list[dict], p: dict, lat0: float, lng0: float) -> list[dict]:
    """Marca los registros (en sitio) y devuelve la lista de pares que quedan en revisión."""
    r = p["reglas"]["deduplicacion"]
    dmax = r["distancia_max_m"]
    for it in registros:
        it.update(entidad_id="E:" + it["ref"], es_maestro=1, regla_dup=None, puntaje_dup=None, coincide_con=None)
    elegibles = [i for i, it in enumerate(registros)
                 if it.get("dedupe") and not it.get("motivo_exclusion") and it.get("lat") is not None]
    xy = {i: geo.a_local_m(registros[i]["lat"], registros[i]["lng"], lat0, lng0) for i in elegibles}

    # Candidatos por rejilla (puntos) y por recuadro (polígonos, cuyo centroide puede quedar lejos).
    rejilla: dict[tuple[int, int], list[int]] = {}
    for i in elegibles:
        rejilla.setdefault((int(xy[i][0] // dmax), int(xy[i][1] // dmax)), []).append(i)
    pares: set[tuple[int, int]] = set()
    for (cx, cy), lista in rejilla.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in rejilla.get((cx + dx, cy + dy), []):
                    for i in lista:
                        if i < j:
                            pares.add((i, j))
    for i in elegibles:
        anillos = registros[i].get("anillos")
        if not anillos:
            continue
        plano = [geo.a_local_m(pt[1], pt[0], lat0, lng0) for anillo in anillos for pt in anillo]
        x0, x1 = min(q[0] for q in plano) - dmax, max(q[0] for q in plano) + dmax
        y0, y1 = min(q[1] for q in plano) - dmax, max(q[1] for q in plano) + dmax
        for j in elegibles:
            if j != i and x0 <= xy[j][0] <= x1 and y0 <= xy[j][1] <= y1:
                pares.add((min(i, j), max(i, j)))

    padre = {i: i for i in elegibles}

    def raiz(i: int) -> int:
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i

    revision: list[dict] = []
    for i, j in sorted(pares):
        a, b = registros[i], registros[j]
        if (a["fuente"], a["capa"]) == (b["fuente"], b["capa"]) or a["categoria"] != b["categoria"]:
            continue
        dist = _dist_par(a, b, xy[i], xy[j])
        res = puntaje_par(a, b, dist, r)
        if res is None:
            continue
        puntaje, regla = res
        if puntaje >= r["umbral_auto"]:
            padre[raiz(i)] = raiz(j)
            for x in (a, b):
                if x["puntaje_dup"] is None or puntaje > x["puntaje_dup"]:
                    x.update(regla_dup=regla, puntaje_dup=puntaje)
        elif puntaje >= r["umbral_revision"]:
            revision.append({"par": f"{a['ref']}|{b['ref']}", "a": a["ref"], "b": b["ref"],
                             "puntaje": puntaje, "regla": regla, "distancia_m": round(dist, 1)})

    grupos: dict[int, list[int]] = {}
    for i in elegibles:
        grupos.setdefault(raiz(i), []).append(i)
    for miembros in grupos.values():
        if len(miembros) > 1:
            _marcar_grupo([registros[i] for i in miembros], r)
    # Un par que la regla ya unió por otro camino no necesita revisión.
    por_ref = {it["ref"]: it for it in registros}
    return [par for par in revision if por_ref[par["a"]]["entidad_id"] != por_ref[par["b"]]["entidad_id"]]


def fusionar(registros: list[dict], ref_a: str, ref_b: str, r: dict, regla: str, puntaje: float | None) -> bool:
    """Une las entidades de dos registros (decisión de revisión). Devuelve False si no se pudo."""
    por_ref = {it["ref"]: it for it in registros}
    a, b = por_ref.get(ref_a), por_ref.get(ref_b)
    if a is None or b is None or a["entidad_id"] == b["entidad_id"]:
        return False
    miembros = [it for it in registros if it["entidad_id"] in (a["entidad_id"], b["entidad_id"])]
    _marcar_grupo(miembros, r)
    for x in (a, b):
        x.update(regla_dup=regla, puntaje_dup=puntaje)
    return True
