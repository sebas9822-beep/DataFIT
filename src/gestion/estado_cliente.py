"""Estado de la ficha con el cliente: lo que el comercial va registrando después de armar la ficha.

- Los estados salen de reglas.yaml › gestion_cliente (por entregar, entregada, aceptada, rechazada…).
- Una ficha recién hecha está en el estado inicial hasta que el comercial registre otro.
- Solo se agregan filas: el estado vigente es el último y el historial completo queda en la historia del cliente.
- Un estado que lo exige (rechazada) pide el motivo: su tipo y, en palabras del comercial, qué pasó.
"""
from __future__ import annotations

import re

from captura import bd

from .metas import GestionInvalida


def _cfg(p: dict) -> dict:
    return p["reglas"]["gestion_cliente"]


def catalogo(p: dict) -> dict:
    """Lo que necesita la pantalla: estados con su etiqueta, motivos de rechazo y topes de texto."""
    cfg = _cfg(p)
    return {"estado_inicial": cfg["estado_inicial"],
            "estados": [{"id": k, "etiqueta": v["etiqueta"], "ayuda": v.get("ayuda", ""), "exige_motivo": bool(v.get("exige_motivo"))}
                        for k, v in cfg["estados"].items()],
            "motivos_rechazo": [{"id": k, "etiqueta": v} for k, v in cfg["motivos_rechazo"].items()],
            "motivo_min_letras": cfg["motivo_min_letras"], "motivo_max": cfg["motivo_max"], "nota_max": cfg["nota_max"]}


def _texto(valor, tope: int, campo: str) -> str | None:
    texto = re.sub(r"\s+", " ", str(valor or "")).strip()
    if len(texto) > tope:
        raise GestionInvalida(f"{campo}: hasta {tope} letras")
    return texto or None


def _publico(f, p: dict) -> dict:
    cfg = _cfg(p)
    return {"estado": f["estado"], "etiqueta": (cfg["estados"].get(f["estado"]) or {}).get("etiqueta", f["estado"]),
            "motivo_tipo": f["motivo_tipo"], "motivo_etiqueta": cfg["motivos_rechazo"].get(f["motivo_tipo"]) if f["motivo_tipo"] else None,
            "motivo": f["motivo"], "nota": f["nota"], "ficha_version": f["ficha_version"], "registrado_en": f["registrado_en"],
            "comercial": f["comercial"]}


def inicial(p: dict, desde: str | None = None) -> dict:
    """El estado de una ficha sobre la que el comercial todavía no ha registrado nada."""
    cfg = _cfg(p)
    estado = cfg["estado_inicial"]
    return {"estado": estado, "etiqueta": cfg["estados"][estado]["etiqueta"], "motivo_tipo": None, "motivo_etiqueta": None,
            "motivo": None, "nota": None, "ficha_version": None, "registrado_en": desde, "comercial": None, "sin_registrar": True}


def historial(p: dict, cotizacion_id: str) -> list[dict]:
    """Del más reciente al más antiguo."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute("SELECT * FROM gestion_cliente WHERE cotizacion_id=? ORDER BY gestion_id DESC LIMIT ?",
                            (cotizacion_id, _cfg(p)["historial_en_lista"])).fetchall()
    return [_publico(f, p) for f in filas]


def de_cotizacion(p: dict, cotizacion_id: str, ficha: dict | None) -> dict | None:
    """Estado vigente e historial de una cotización. None si todavía no tiene ficha: no hay nada que entregar."""
    if not ficha:
        return None
    pasos = historial(p, cotizacion_id)
    return {"vigente": pasos[0] if pasos else inicial(p, ficha.get("generada_en")), "historial": pasos}


def registrar(p: dict, cotizacion: dict, comercial: str, datos: dict) -> dict:
    """Agrega un estado al historial de la cotización. `cotizacion` es la fila completa (ya se validó quién puede)."""
    cfg = _cfg(p)
    datos = datos or {}
    estado = datos.get("estado")
    if estado not in cfg["estados"]:
        raise GestionInvalida("Elige el estado de la ficha con el cliente")
    with bd.conectar(p["rutas"]["bd"]) as con:
        ficha = con.execute("SELECT version FROM fichas WHERE cotizacion_id=? AND vigente=1", (cotizacion["cotizacion_id"],)).fetchone()
        if ficha is None:
            raise GestionInvalida("Esta cotización todavía no tiene ficha: primero genera la ficha")
        motivo_tipo, motivo = datos.get("motivo_tipo") or None, _texto(datos.get("motivo"), cfg["motivo_max"], "Motivo")
        if cfg["estados"][estado].get("exige_motivo"):
            if motivo_tipo not in cfg["motivos_rechazo"]:
                raise GestionInvalida("Elige el motivo")
            if not motivo or len(motivo) < cfg["motivo_min_letras"]:
                raise GestionInvalida("Escribe qué dijo el cliente: el motivo queda en su historia")
        else:
            motivo_tipo = motivo = None                  # el motivo solo acompaña a un estado que lo exige
        nota = _texto(datos.get("nota"), cfg["nota_max"], "Nota")
        ultimo = con.execute("SELECT estado, motivo_tipo, motivo, nota FROM gestion_cliente WHERE cotizacion_id=? "
                             "ORDER BY gestion_id DESC LIMIT 1", (cotizacion["cotizacion_id"],)).fetchone()
        if ultimo and (ultimo["estado"], ultimo["motivo_tipo"], ultimo["motivo"], ultimo["nota"]) == (estado, motivo_tipo, motivo, nota):
            raise GestionInvalida("Ese ya es el estado registrado")
        con.execute("INSERT INTO gestion_cliente (cotizacion_id, cliente_id, comercial, estado, motivo_tipo, motivo, nota, "
                    "ficha_version, registrado_en) VALUES (?,?,?,?,?,?,?,?,?)",
                    (cotizacion["cotizacion_id"], cotizacion["cliente_id"], comercial, estado, motivo_tipo, motivo, nota,
                     ficha["version"], bd.ahora()))
    return de_cotizacion(p, cotizacion["cotizacion_id"], {"generada_en": None})


def historia_del_cliente(p: dict, cliente_id: int, comercial: str | None) -> list[dict]:
    """Las cotizaciones de un cliente con su estado vigente. Con `comercial`, solo las suyas."""
    cfg = _cfg(p)
    donde, args = "q.cliente_id=?", [cliente_id]
    if comercial is not None:
        donde += " AND q.comercial=?"
        args.append(comercial)
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute(
            "SELECT q.cotizacion_id, q.creada_en, q.comercial, q.inm_tipo, q.inm_area_m2, q.direccion_ingresada, "
            "ca.direccion_catastral, pr.barrio_nombre, fi.version AS ficha_version, fi.estado AS ficha_estado, fi.generada_en, "
            "(SELECT COUNT(*) FROM gestion_cliente g WHERE g.cotizacion_id = q.cotizacion_id) AS pasos "
            "FROM cotizaciones q LEFT JOIN capturas ca ON ca.captura_id = q.captura_id LEFT JOIN predios pr ON pr.captura_id = q.captura_id "
            f"LEFT JOIN fichas fi ON fi.cotizacion_id = q.cotizacion_id AND fi.vigente = 1 WHERE {donde} "
            "ORDER BY q.creada_en DESC LIMIT ?", (*args, cfg["historial_en_lista"])).fetchall()
        salida = []
        for f in filas:
            ultimo = con.execute("SELECT * FROM gestion_cliente WHERE cotizacion_id=? ORDER BY gestion_id DESC LIMIT 1",
                                 (f["cotizacion_id"],)).fetchone()
            estado = None if not f["ficha_version"] else (_publico(ultimo, p) if ultimo else inicial(p, f["generada_en"]))
            salida.append({"cotizacion_id": f["cotizacion_id"], "creada_en": f["creada_en"], "comercial": f["comercial"],
                           "direccion": f["direccion_catastral"] or f["direccion_ingresada"], "barrio": f["barrio_nombre"],
                           "tipo": f["inm_tipo"], "area_m2": f["inm_area_m2"], "ficha_version": f["ficha_version"],
                           "ficha_estado": f["ficha_estado"], "estado_cliente": estado, "cambios_de_estado": f["pasos"]})
    return salida
