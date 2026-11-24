"""Clasificación determinista a la taxonomía propia.

Cada función devuelve (categoria, subtipo, confianza). La confianza dice qué tan directa es la
regla: `alta` (mapeo exacto de la fuente), `media` (por prefijo o por omisión razonable) o `baja`
(no se reconoció: candidato a revisión con Claude).
"""
from __future__ import annotations

from .normalizacion import a_etiqueta, sin_tildes

OTROS = "otros_equipamientos"


def _clave(texto) -> str:
    return sin_tildes(str(texto or "")).strip().lower()


def geomed_equipamiento(tipo, categoria_cod, tax: dict) -> tuple[str, str | None, str]:
    """Inventario de equipamientos de GeoMedellín (campo `tipo` y código `categoria`)."""
    sub = a_etiqueta(tipo)
    indice = {_clave(k): v for k, v in tax["geomed_tipo"].items()}
    k = _clave(tipo)
    try:
        if int(categoria_cod) == int(tax["geomed_categoria_seguridad"]):
            return "seguridad", (indice[k][1] if k in indice else sub), "alta"
    except (TypeError, ValueError):
        pass
    if k in indice:
        cat, st = indice[k]
        return cat, st, "alta"
    for prefijo, (cat, st) in tax["geomed_tipo_prefijos"].items():
        if k.startswith(_clave(prefijo)):
            return cat, st, "media"
    return OTROS, sub, "baja"


def toponimia(codigo, tax: dict) -> tuple[str, str | None, str]:
    c = str(codigo).strip() if codigo is not None else ""
    if c.isdigit():
        c = c.zfill(2)
    if c in tax["toponimia"]:
        cat, st = tax["toponimia"][c]
        return cat, st, "alta"
    cat, st = tax["toponimia_defecto"]
    return cat, st, "baja"


def ciiu(codigo, grupo, tax: dict) -> tuple[str, str | None, str]:
    """Establecimientos de industria y comercio: gana el prefijo CIIU más largo."""
    c = "".join(ch for ch in str(codigo or "") if ch.isdigit())
    mejor = None
    for prefijo, destino in tax["ciiu"].items():
        if c.startswith(str(prefijo)) and (mejor is None or len(str(prefijo)) > len(mejor[0])):
            mejor = (str(prefijo), destino)
    if mejor:
        return mejor[1][0], mejor[1][1], "alta"
    cat, st = tax["ciiu_defecto"]
    etiqueta = tax["grupos_actividad"].get(str(grupo).zfill(2)) if grupo else None
    return cat, etiqueta or st, "media" if c else "baja"


def estacion(linea, tipo_est, tax: dict) -> tuple[str, str | None, str]:
    indice = {_clave(k): v for k, v in tax["lineas_masivo"].items()}
    modo = indice.get(_clave(linea))
    if modo:
        return "movilidad", modo, "alta"
    return "movilidad", tax["linea_masivo_defecto"], "media"


def osm(tags: dict, tax: dict) -> tuple[str, str | None, str] | None:
    """Etiquetas de OpenStreetMap. Devuelve None si el elemento no pertenece a la taxonomía."""
    for clave, valor, cat, sub in tax["osm"]:
        if clave not in tags:
            continue
        if valor == "*":
            detalle = sub if tags[clave] in ("yes", "") else a_etiqueta(f"{sub}_{tags[clave]}")
            return cat, detalle, "media"
        if tags[clave] == valor:
            return cat, sub, "alta"
    if tags.get("name"):
        for clave, valor, cat, sub in tax["osm_residencial"]:
            if tags.get(clave) == valor:
                return cat, sub, "alta"
    return None


def google(tipo_primario, categoria_grupo: str) -> tuple[str, str | None, str]:
    """Google ya llega con la categoría del grupo de tipos con que se consultó."""
    return categoria_grupo, a_etiqueta(tipo_primario), "alta"
