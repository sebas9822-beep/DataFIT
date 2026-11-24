"""Normalización de textos, barrios, fechas y documentos para conformar las dimensiones.

Todo lo que aquí cambia un valor es reproducible y no depende del orden de carga.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def sin_tildes(texto) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()


def texto_base(texto) -> str:
    """Minúsculas, sin tildes y con espacios simples."""
    return " ".join(sin_tildes(texto).lower().split())


def limpiar_barrio(texto) -> str:
    """Quita las referencias entre paréntesis ('El Salvador (diagonal a la iglesia)' → 'El Salvador')."""
    return " ".join(re.sub(r"\([^)]*\)", " ", str(texto or "")).split())


def clave_barrio(texto) -> str:
    """Clave de comparación: sin tildes, sin signos y sin espacios ('Bomboná No.1' = 'Bombona No1')."""
    return re.sub(r"[^a-z0-9]", "", texto_base(limpiar_barrio(texto)))


def clave_municipio(texto) -> str:
    return re.sub(r"[^a-z0-9]", "", texto_base(texto))


def titulo(texto) -> str:
    """'PATIO BONITO' → 'Patio Bonito'; conserva lo que ya viene con mayúsculas y minúsculas."""
    t = " ".join(str(texto or "").split())
    return t.title() if t.isupper() or t.islower() else t


def a_local(marca_utc: str | None, desfase_horas: float) -> datetime | None:
    """Marca ISO en UTC → fecha y hora local (Colombia no tiene horario de verano)."""
    if not marca_utc:
        return None
    try:
        t = datetime.fromisoformat(str(marca_utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t.astimezone(timezone.utc) + timedelta(hours=desfase_horas)).replace(tzinfo=None)


def a_fecha(valor) -> date | None:
    """'2023-08-22', '2023-08-22T10:00:00' o un datetime → date. Lo ilegible devuelve None."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def fila_fecha(d: date) -> dict:
    iso = d.isocalendar()
    return {"fecha_key": d.year * 10000 + d.month * 100 + d.day, "fecha": d.isoformat(), "anio": d.year,
            "trimestre": (d.month - 1) // 3 + 1, "mes": d.month, "mes_nombre": MESES[d.month],
            "anio_mes": f"{d.year:04d}-{d.month:02d}", "dia": d.day, "semana_iso": iso[1],
            "dia_semana": iso[2], "dia_nombre": DIAS[iso[2] - 1], "es_fin_de_semana": int(iso[2] >= 6)}


def huella(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def enmascarar(documento: str | None, visibles: int) -> str:
    if not documento:
        return ""
    return "*" * max(0, len(documento) - visibles) + documento[-visibles:]


def entero(valor) -> int | None:
    try:
        if valor is None or valor != valor:          # None o NaN
            return None
        return int(valor)
    except (TypeError, ValueError):
        return None


def real(valor) -> float | None:
    try:
        if valor is None or valor != valor:
            return None
        return float(valor)
    except (TypeError, ValueError):
        return None
