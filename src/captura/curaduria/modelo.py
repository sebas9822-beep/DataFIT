"""Modelo de curaduría por reglas (determinista y reproducible).

Recibe los registros crudos de todas las fuentes y devuelve los mismos registros, nunca menos,
con columnas nuevas: nombre limpio, clave de comparación, coordenadas métricas, anillo, entidad,
maestro, completitud y banderas de calidad. Cada cambio queda en la bitácora.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from .. import geo
from . import deduplicacion
from .normalizacion import limpiar_nombre, nombre_clave


def _ref(it: dict) -> str:
    return f"{it['fuente']}:{it['capa']}:{it['id_externo']}"


def curar(items: list[dict], p: dict, lat0: float, lng0: float) -> dict:
    """Devuelve {registros, bitacora, revision, exactos_repetidos}."""
    tax, reglas = p["taxonomia"], p["reglas"]
    anillos = reglas["captura"]["anillos_m"]
    tope_detalle = p["params"]["bitacora"]["max_detalle_por_capa"]
    bitacora: list[dict] = []

    # 1. Repetidos exactos (misma fuente, capa e id): se conserva el primero y se deja constancia.
    registros, vistos, repetidos = [], set(), {}
    for it in items:
        it["ref"] = _ref(it)
        if it["ref"] in vistos:
            repetidos[it["capa"]] = repetidos.get(it["capa"], 0) + 1
            continue
        vistos.add(it["ref"])
        registros.append(it)
    for capa, n in repetidos.items():
        bitacora.append({"item_ref": f"capa:{capa}", "etapa": "carga", "campo": "id_externo",
                         "valor_antes": f"{n} filas repetidas", "valor_despues": "se conserva la primera",
                         "regla": "repetido_exacto_misma_capa", "origen": "regla"})

    # 2. Normalización de nombres y coordenadas métricas.
    cambios: dict[str, list[dict]] = {}
    for it in registros:
        original = it.get("nombre_original")
        it["nombre"] = limpiar_nombre(original, tax)
        it["nombre_clave"] = nombre_clave(it["nombre"], tax)
        if it["nombre"] != original and original is not None:
            cambios.setdefault(it["capa"], []).append({
                "item_ref": it["ref"], "etapa": "normalizacion", "campo": "nombre",
                "valor_antes": original, "valor_despues": it["nombre"],
                "regla": "espacios_y_mayusculas", "origen": "regla"})
        if it.get("lat") is not None:
            it["x_9377"], it["y_9377"] = geo.a_9377(it["lng"], it["lat"])
        else:
            it["x_9377"] = it["y_9377"] = None
        it["anillo_m"] = geo.anillo_de(it.get("distancia_m"), anillos)
        it.setdefault("curado_por", "regla")
    for capa, lista in cambios.items():
        if len(lista) > tope_detalle:             # capa masiva: una sola línea de resumen
            bitacora.append({"item_ref": f"capa:{capa}", "etapa": "normalizacion", "campo": "nombre",
                             "valor_antes": f"{len(lista)} nombres", "valor_despues": "normalizados",
                             "regla": "espacios_y_mayusculas", "origen": "regla"})
        else:
            bitacora.extend(lista)

    # 3. Duplicados entre fuentes.
    revision = deduplicacion.enlazar(registros, p, lat0, lng0)

    # 4. Calidad: completitud y banderas (se marca, no se borra).
    campos = reglas["calidad"]["campos_completitud"]
    limite_anio = date.today().year - reglas["calidad"]["anios_dato_antiguo"]
    for it in registros:
        presentes = sum(1 for c in campos if it.get(c) not in (None, ""))
        it["completitud"] = round(presentes / len(campos), 2)
        banderas = list(it.get("banderas") or [])
        if not it.get("nombre"):
            banderas.append("sin_nombre")
        if it.get("confianza_clasificacion") == "baja":
            banderas.append("clasificacion_dudosa")
        fecha = str((it.get("atributos") or {}).get("fecha_dato") or "")
        if fecha[:4].isdigit() and int(fecha[:4]) < limite_anio:
            banderas.append("dato_antiguo")
        it["banderas"] = sorted(set(banderas))
    return {"registros": registros, "bitacora": bitacora, "revision": revision,
            "exactos_repetidos": sum(repetidos.values())}


COLUMNAS = ["ref", "fuente", "capa", "id_externo", "categoria", "subtipo", "nombre", "lat", "lng",
            "distancia_m", "anillo_m", "entidad_id", "es_maestro", "motivo_exclusion", "en_mapa",
            "completitud", "curado_por", "confianza_clasificacion", "expira_en"]


def a_tabla(registros: list[dict]) -> pd.DataFrame:
    """Tabla de trabajo para el análisis (una fila por registro, sin geometrías)."""
    df = pd.DataFrame([{c: it.get(c) for c in COLUMNAS} for it in registros], columns=COLUMNAS)
    df["distancia_m"] = pd.to_numeric(df["distancia_m"], errors="coerce")
    df["en_mapa"] = df["en_mapa"].fillna(True).astype(bool)
    df["excluido"] = df["motivo_exclusion"].notna()
    return df


def control_distancia(registros: list[dict], lat0: float, lng0: float, tope: int | None = None) -> dict | None:
    """Control alterno: diferencia entre haversine y la distancia plana en EPSG:9377 (puntos)."""
    difs = []
    for it in registros:
        if it.get("geometria") != "punto" or it.get("lat") is None or it.get("distancia_m") is None:
            continue
        d = geo.distancia_9377_m(lat0, lng0, it["lat"], it["lng"])
        if d is None:
            return None
        difs.append(abs(d - it["distancia_m"]))
        if tope and len(difs) >= tope:
            break
    if not difs:
        return None
    return {"n": len(difs), "max_dif_m": round(max(difs), 2), "media_dif_m": round(sum(difs) / len(difs), 3)}
