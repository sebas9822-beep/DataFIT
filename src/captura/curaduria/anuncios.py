"""Depuración de anuncios de inmobiliarias: el paso que sigue a la búsqueda y va antes del ETL.

Qué hace sobre los anuncios válidos de una captura (base `portales`):

1. Enlaza el mismo inmueble publicado en dos sitios distintos: misma operación y tipo, mismo barrio, y área y
   precio iguales o dentro de la tolerancia de almacen.yaml › bases_mercado.portales.duplicados_entre_sitios.
   El repetido no se borra: queda con `regla_dup`, `puntaje_dup` y `coincide_con`, y el ETL lo lleva al almacén
   como duplicado. Se conserva el anuncio que trae fecha de publicación y, si ninguno o los dos la traen, el
   que se vio primero.
2. Mide cómo queda cada anuncio frente a la regla de vigencia (almacen.yaml › vigencia). La exclusión la
   aplica la ficha con su propia fecha; aquí queda contada y a la vista.

Qué no hace: no enlaza anuncios del mismo sitio (pueden ser unidades iguales de un edificio), no cambia precios
ni áreas y no usa a Claude. Son reglas fijas: la misma entrada da siempre la misma salida.
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date

from .. import bd

BASE = "portales"
REGLA = "R3_entre_sitios"


def _plano(texto) -> str:
    """Barrio comparable: sin tildes, en minúsculas y solo letras y números."""
    sin = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", sin).strip()


def restar_meses(dia: date, meses: int) -> date:
    """El mismo día `meses` atrás; si ese mes es más corto, su último día (igual que la regla de la ficha)."""
    total = dia.year * 12 + (dia.month - 1) - int(meses)
    anio, mes = divmod(total, 12)
    return date(anio, mes + 1, min(dia.day, calendar.monthrange(anio, mes + 1)[1]))


def _cercania(a: float, b: float, tolerancia: float) -> float | None:
    """1.0 = iguales; 0.0 = en el borde de la tolerancia; None = fuera."""
    diferencia = abs(a - b) / max(a, b)
    if diferencia > tolerancia:
        return None
    return 1 - diferencia / tolerancia if tolerancia else 1.0


def emparejar(anuncios: list[dict], cfg: dict) -> dict[int, dict]:
    """Repetidos entre sitios. Devuelve {oferta_id del repetido: {regla_dup, puntaje_dup, coincide_con}}.

    Cada anuncio trae: oferta_id, nk, capa (el sitio), operacion, tipo_inmueble, area, precio, barrio,
    fecha_publicacion y los campos de `iguales_si_existen`.
    """
    tol_a, tol_p = float(cfg["tolerancia_area_pct"]), float(cfg["tolerancia_precio_pct"])
    pesos = cfg["pesos_puntaje"]
    iguales = list(cfg.get("iguales_si_existen") or [])
    orden = sorted(anuncios, key=lambda a: (0 if a.get("fecha_publicacion") else 1, a["oferta_id"]))
    conservados: list[dict] = []
    marcas: dict[int, dict] = {}
    for a in orden:
        if not (a.get("area") and a.get("precio")):
            continue
        mejor, puntaje_mejor = None, -1.0
        barrio_a = _plano(a.get("barrio"))
        for r in conservados:
            if r["capa"] == a["capa"] or r["operacion"] != a["operacion"] or r["tipo_inmueble"] != a["tipo_inmueble"]:
                continue
            barrio_r = _plano(r.get("barrio"))
            if barrio_a and barrio_r:
                if barrio_a != barrio_r:
                    continue
            elif cfg.get("exigir_mismo_barrio", True):
                continue
            if any(a.get(k) is not None and r.get(k) is not None and a[k] != r[k] for k in iguales):
                continue
            c_area, c_precio = _cercania(a["area"], r["area"], tol_a), _cercania(a["precio"], r["precio"], tol_p)
            if c_area is None or c_precio is None:
                continue
            puntaje = (pesos["area"] * c_area + pesos["precio"] * c_precio) / (pesos["area"] + pesos["precio"])
            if puntaje > puntaje_mejor:
                mejor, puntaje_mejor = r, puntaje
        if mejor is None:
            conservados.append(a)
            continue
        marcas[a["oferta_id"]] = {"regla_dup": REGLA, "puntaje_dup": round(puntaje_mejor, 4), "coincide_con": mejor["nk"]}
    return marcas


def clasificar_vigencia(anuncios: list[dict], cfg_v: dict, hoy: date) -> dict:
    """Cuántos anuncios quedan vigentes hoy y por qué (fecha de publicación o día en que se vieron)."""
    corte = restar_meses(hoy, int(cfg_v["max_meses_publicacion"])).isoformat()
    por_observacion = cfg_v["sin_fecha_publicacion"] == "fecha_observacion"
    cuenta = {"con_fecha_vigentes": 0, "sin_fecha_vigentes": 0, "fuera_de_plazo": 0, "sin_fecha_excluidos": 0}
    for a in anuncios:
        publicada = a.get("fecha_publicacion")
        if publicada:
            cuenta["con_fecha_vigentes" if publicada >= corte else "fuera_de_plazo"] += 1
        elif not por_observacion:
            cuenta["sin_fecha_excluidos"] += 1
        else:
            cuenta["sin_fecha_vigentes" if str(a.get("obtenido_en") or "")[:10] >= corte else "fuera_de_plazo"] += 1
    return dict(cuenta, corte=corte, max_meses_publicacion=int(cfg_v["max_meses_publicacion"]),
                regla_sin_fecha=cfg_v["sin_fecha_publicacion"],
                vigentes=cuenta["con_fecha_vigentes"] + cuenta["sin_fecha_vigentes"])


def depurar(p: dict, captura_id: str, hoy: date) -> dict:
    """Marca en la base los repetidos entre sitios de la captura y devuelve el resumen del paso.

    Se puede repetir: cada corrida recalcula las marcas sobre todos los anuncios válidos de la captura.
    """
    cfg_base = p["almacen"]["bases_mercado"][BASE]
    cfg = cfg_base.get("duplicados_entre_sitios") or {}
    activa, accion = bool(cfg.get("activa")), cfg.get("accion", "enlazar")
    with bd.conectar(p["rutas"]["bd"]) as con:
        con.execute("UPDATE ofertas SET regla_dup=NULL, puntaje_dup=NULL, coincide_con=NULL WHERE captura_id=? AND fuente=?",
                    (captura_id, BASE))
        filas = [dict(f) for f in con.execute(
            "SELECT oferta_id, fuente || ':' || id_externo AS nk, capa, operacion, tipo_inmueble, area_usada AS area, precio, "
            "barrio_publicado AS barrio, habitaciones, banos, parqueaderos, fecha_publicacion, obtenido_en, url, "
            "COALESCE(origen_publicacion, capa) AS sitio "
            "FROM ofertas WHERE captura_id=? AND fuente=? AND motivo_exclusion IS NULL ORDER BY oferta_id", (captura_id, BASE))]
        descartados = con.execute("SELECT COUNT(*) FROM ofertas WHERE captura_id=? AND fuente=? AND motivo_exclusion IS NOT NULL",
                                  (captura_id, BASE)).fetchone()[0]
        marcas = emparejar(filas, cfg) if activa else {}
        if accion == "enlazar":
            con.executemany("UPDATE ofertas SET regla_dup=?, puntaje_dup=?, coincide_con=? WHERE oferta_id=?",
                            [(m["regla_dup"], m["puntaje_dup"], m["coincide_con"], oid) for oid, m in marcas.items()])
    por_nk = {f["nk"]: f for f in filas}
    pares = [{"repetido": f["url"], "sitio_repetido": f["sitio"], "conservado": por_nk[marcas[f["oferta_id"]]["coincide_con"]]["url"],
              "sitio_conservado": por_nk[marcas[f["oferta_id"]]["coincide_con"]]["sitio"],
              "puntaje": marcas[f["oferta_id"]]["puntaje_dup"]} for f in filas if f["oferta_id"] in marcas]
    quedan = [f for f in filas if not (accion == "enlazar" and f["oferta_id"] in marcas)]
    vigencia = clasificar_vigencia(quedan, p["almacen"]["vigencia"], hoy)
    return {"estado": "ok", "anuncios_validos": len(filas), "descartados_al_leer": descartados,
            "repetidos_entre_sitios": len(marcas), "accion": accion if activa else "desactivada",
            "unicos": len(quedan), "pares": pares, "vigencia": vigencia,
            "regla": None if not activa else {
                "nombre": REGLA, "tolerancia_area_pct": cfg["tolerancia_area_pct"], "tolerancia_precio_pct": cfg["tolerancia_precio_pct"],
                "exigir_mismo_barrio": bool(cfg.get("exigir_mismo_barrio", True)),
                "iguales_si_existen": list(cfg.get("iguales_si_existen") or [])}}


def texto(resumen: dict) -> str:
    """Una línea para la pantalla de avance."""
    if not resumen["anuncios_validos"]:
        return "sin anuncios válidos que depurar"
    v = resumen["vigencia"]
    partes = [f"{resumen['anuncios_validos']} anuncios válidos"]
    if resumen["accion"] == "desactivada":
        partes.append("repetidos entre sitios: regla desactivada")
    else:
        verbo = "enlazados" if resumen["accion"] == "enlazar" else "marcados, sin sacar"
        partes.append(f"{resumen['repetidos_entre_sitios']} repetidos entre sitios {verbo}")
    partes.append(f"{v['vigentes']} vigentes ({v['con_fecha_vigentes']} con fecha, {v['sin_fecha_vigentes']} por el día en que se vieron)")
    fuera = v["fuera_de_plazo"] + v["sin_fecha_excluidos"]
    if fuera:
        partes.append(f"{fuera} fuera del plazo de {v['max_meses_publicacion']} meses")
    return " · ".join(partes)
