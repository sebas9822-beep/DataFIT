"""Normalizador de direcciones de Medellín (nomenclatura CL, CR, TV, DG, CQ, AV).

Devuelve componentes, no solo texto, para cruzar contra la nomenclatura domiciliaria del catastro
sin depender de cómo esté escrita. Una dirección incompleta nunca se convierte en un punto exacto.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TIPOS_VIA = {
    "CL": ["calle", "cll", "clle", "cl", "call"],
    "CR": ["carrera", "cra", "kra", "kr", "cr", "carr", "crr", "kar"],
    "TV": ["transversal", "transv", "trans", "tv", "tr"],
    "DG": ["diagonal", "diag", "dg"],
    "CQ": ["circular", "circ", "cq"],
    "AV": ["avenida", "avda", "av"],
}
_ALIAS = {alias: codigo for codigo, lista in TIPOS_VIA.items() for alias in lista}

_INTERIOR = (r"\b(apto|apartamento|ap|int|interior|of|oficina|local|lc|torre|casa|piso|bloque|bl|"
             r"unidad|urb|urbanizacion|edificio|ed|conjunto)\b")

_PATRON = re.compile(
    r"^(?P<tipo>[a-z]+)\.?\s*"
    r"(?P<via>\d{1,3})\s*(?P<ap_via>[a-z]{0,2})\s*(?P<sur_via>sur)?\s*"
    r"#?\s*"
    r"(?P<cruce>\d{1,3})\s*(?P<ap_cruce>[a-z]{0,2})\s*(?P<sur_cruce>sur)?\s*"
    r"[-\s]\s*(?P<placa>\d{1,3})\b"
)


@dataclass
class Direccion:
    original: str
    tipo_via: str | None = None
    num_via: int | None = None
    ap_via: str = ""
    sur_via: bool = False
    num_cruce: int | None = None
    ap_cruce: str = ""
    sur_cruce: bool = False
    placa: int | None = None
    interior: str = ""
    valida: bool = False

    @property
    def canonica(self) -> str | None:
        if not self.valida:
            return None
        sv = " SUR" if self.sur_via else ""
        sc = " SUR" if self.sur_cruce else ""
        return f"{self.tipo_via} {self.num_via}{self.ap_via}{sv} # {self.num_cruce}{self.ap_cruce}{sc} - {self.placa}"


def _limpiar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\b(no|nro|num|numero|n)\s*[\.:]?\s*(?=\d)", " # ", t)
    t = re.sub(r",?\s*(medellin|antioquia|colombia)\b.*$", "", t)
    t = re.sub(r"[,;]", " ", t)
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1 - \2", t)
    return re.sub(r"\s+", " ", t).strip()


def normalizar(texto: str) -> Direccion:
    d = Direccion(original=texto or "")
    t = _limpiar(texto or "")
    m_int = re.search(_INTERIOR, t)
    if m_int and m_int.start() > 0:
        d.interior = t[m_int.start():].strip().upper()
        t = t[:m_int.start()].strip()
    m = _PATRON.match(t)
    if not m:
        return d
    tipo = _ALIAS.get(m["tipo"].rstrip("."))
    if not tipo:
        return d
    d.tipo_via = tipo
    d.num_via, d.ap_via = int(m["via"]), m["ap_via"].upper()
    d.num_cruce, d.ap_cruce = int(m["cruce"]), m["ap_cruce"].upper()
    d.placa = int(m["placa"])
    d.sur_via, d.sur_cruce = bool(m["sur_via"]), bool(m["sur_cruce"])
    d.valida = True
    return d
