"""Seguimiento comercial: notas, compromisos, reconocimientos y alertas de un supervisor a un comercial."""
from __future__ import annotations

import re
from datetime import date

from captura import bd

from .metas import GestionInvalida


def crear(p: dict, supervisor: str, comercial: str, tipo, nota, cotizacion_id: str | None = None,
          fecha_compromiso: str | None = None) -> dict:
    r = p["reglas"]["supervision"]
    tipo = str(tipo or "").strip().lower()
    if tipo not in r["tipos_seguimiento"]:
        raise GestionInvalida("Tipo de seguimiento no reconocido")
    nota = re.sub(r"\s+", " ", str(nota or "")).strip()
    if not (r["nota_min"] <= len(nota) <= r["nota_max"]):
        raise GestionInvalida(f"La nota tiene entre {r['nota_min']} y {r['nota_max']} caracteres")
    if fecha_compromiso:
        try:
            fecha_compromiso = date.fromisoformat(str(fecha_compromiso)[:10]).isoformat()
        except ValueError:
            raise GestionInvalida("La fecha del compromiso va como AAAA-MM-DD") from None
    t = bd.ahora()
    with bd.conectar(p["rutas"]["bd"]) as con:
        if not con.execute("SELECT 1 FROM comerciales WHERE usuario=? AND rol='comercial'", (comercial,)).fetchone():
            raise GestionInvalida("El comercial no existe")
        if cotizacion_id:
            fila = con.execute("SELECT comercial FROM cotizaciones WHERE cotizacion_id=?", (cotizacion_id,)).fetchone()
            if fila is None or fila[0] != comercial:
                raise GestionInvalida("La cotización no es de ese comercial")
        sid = con.execute(
            "INSERT INTO seguimientos (supervisor, comercial, cotizacion_id, tipo, nota, estado, fecha_compromiso, creado_en) "
            "VALUES (?,?,?,?,?,?,?,?)", (supervisor, comercial, cotizacion_id or None, tipo, nota, "abierto",
                                         fecha_compromiso or None, t)).lastrowid
    return {"seguimiento_id": sid, "supervisor": supervisor, "comercial": comercial, "cotizacion_id": cotizacion_id or None,
            "tipo": tipo, "nota": nota, "estado": "abierto", "fecha_compromiso": fecha_compromiso or None, "creado_en": t}


def cerrar(p: dict, quien: str, seguimiento_id, comerciales: list[str] | None = None) -> bool:
    """Cierra un seguimiento abierto. `comerciales` limita a los que el supervisor tiene a cargo."""
    try:
        sid = int(seguimiento_id)
    except (TypeError, ValueError):
        raise GestionInvalida("Seguimiento no reconocido") from None
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute("SELECT comercial, estado FROM seguimientos WHERE seguimiento_id=?", (sid,)).fetchone()
        if fila is None or (comerciales is not None and fila["comercial"] not in comerciales):
            return False
        if fila["estado"] == "abierto":
            con.execute("UPDATE seguimientos SET estado='cerrado', cerrado_en=?, cerrado_por=? WHERE seguimiento_id=?",
                        (bd.ahora(), quien, sid))
    return True


def listar(p: dict, comerciales: list[str] | None = None, estado: str | None = None, limite: int | None = None) -> list[dict]:
    donde, args = ["1=1"], []
    limite = limite or p["reglas"]["supervision"]["max_en_lista"]
    if comerciales is not None:
        if not comerciales:
            return []
        donde.append(f"s.comercial IN ({', '.join('?' for _ in comerciales)})")
        args += list(comerciales)
    if estado:
        donde.append("s.estado = ?")
        args.append(estado)
    with bd.conectar(p["rutas"]["bd"]) as con:
        return [dict(f) for f in con.execute(
            "SELECT s.*, co.nombre AS comercial_nombre, su.nombre AS supervisor_nombre FROM seguimientos s "
            "JOIN comerciales co ON co.usuario = s.comercial JOIN comerciales su ON su.usuario = s.supervisor "
            f"WHERE {' AND '.join(donde)} ORDER BY s.estado, s.creado_en DESC LIMIT ?", (*args, limite))]
