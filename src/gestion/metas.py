"""Metas mensuales por comercial. Las fija un supervisor; cada cambio queda en el historial."""
from __future__ import annotations

import re

from captura import bd


class GestionInvalida(ValueError):
    """Dato de gestión que no pasa la validación (mensaje apto para mostrar)."""


def validar_mes(texto) -> str:
    mes = str(texto or "").strip()
    if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", mes):
        raise GestionInvalida("El mes va como AAAA-MM")
    return mes


def _meta(valor, p: dict, nombre: str) -> int | None:
    if valor in (None, ""):
        return None
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise GestionInvalida(f"La meta de {nombre} debe ser un número entero") from None
    if not (0 <= n <= p["reglas"]["supervision"]["meta_max"]):
        raise GestionInvalida(f"La meta de {nombre} está fuera de rango")
    return n


def fijar(p: dict, supervisor: str, comercial: str, anio_mes, meta_cotizaciones, meta_clientes=None) -> dict:
    """Crea o cambia la meta de un comercial para un mes. Devuelve la meta vigente."""
    mes = validar_mes(anio_mes)
    cot, cli = _meta(meta_cotizaciones, p, "cotizaciones"), _meta(meta_clientes, p, "clientes")
    if cot is None and cli is None:
        raise GestionInvalida("Escribe al menos una meta")
    t = bd.ahora()
    with bd.conectar(p["rutas"]["bd"]) as con:
        existe = con.execute("SELECT 1 FROM comerciales WHERE usuario=? AND rol='comercial'", (comercial,)).fetchone()
        if not existe:
            raise GestionInvalida("El comercial no existe")
        previa = con.execute("SELECT * FROM metas WHERE comercial=? AND anio_mes=?", (comercial, mes)).fetchone()
        if previa is None:
            con.execute("INSERT INTO metas VALUES (?,?,?,?,?,?,?)", (comercial, mes, cot, cli, supervisor, t, t))
        else:
            con.execute("UPDATE metas SET meta_cotizaciones=?, meta_clientes=?, definida_por=?, actualizada_en=? "
                        "WHERE comercial=? AND anio_mes=?", (cot, cli, supervisor, t, comercial, mes))
        con.execute("INSERT INTO metas_historial (comercial, anio_mes, meta_cotizaciones, meta_clientes, definida_por, registrada_en) "
                    "VALUES (?,?,?,?,?,?)", (comercial, mes, cot, cli, supervisor, t))
    return {"comercial": comercial, "anio_mes": mes, "meta_cotizaciones": cot, "meta_clientes": cli,
            "definida_por": supervisor, "actualizada_en": t}


def listar(p: dict, anio_mes, comerciales: list[str] | None = None) -> list[dict]:
    mes = validar_mes(anio_mes)
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = [dict(f) for f in con.execute("SELECT * FROM metas WHERE anio_mes=? ORDER BY length(comercial), comercial", (mes,))]
    return [f for f in filas if comerciales is None or f["comercial"] in comerciales]
