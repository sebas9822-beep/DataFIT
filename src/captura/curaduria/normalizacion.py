"""Normalización de textos: nombre legible y clave de comparación entre fuentes."""
from __future__ import annotations

import re
import unicodedata

_VACIOS = {"", "none", "null", "nan", "no aplica", "n/a", "na", "sin nombre", "sin dato", "0", "-"}


def sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def es_vacio(valor) -> bool:
    return valor is None or str(valor).strip().lower() in _VACIOS


def limpiar_nombre(valor, tax: dict) -> str | None:
    """Nombre legible: espacios colapsados y, si venía todo en mayúsculas o minúsculas,
    mayúscula inicial respetando siglas y conectores. Nunca inventa texto."""
    if es_vacio(valor):
        return None
    texto = unicodedata.normalize("NFC", str(valor))
    texto = re.sub(r"\s+", " ", texto).strip(" .,;-_")
    if not texto:
        return None
    letras = [c for c in texto if c.isalpha()]
    if letras and not (all(c.isupper() for c in letras) or all(c.islower() for c in letras)):
        return texto                          # ya trae mayúsculas y minúsculas mezcladas: se respeta
    siglas = {s.upper() for s in tax.get("siglas", [])}
    menores = set(tax.get("minusculas", []))
    articulos = set(tax.get("articulos", []))
    partes: list[str] = []
    for i, palabra in enumerate(texto.split(" ")):
        base = sin_tildes(palabra).upper().strip(".,()")
        baja = palabra.lower()
        if base.replace(".", "") in siglas:
            partes.append(palabra.upper())
        elif i > 0 and (baja in menores or (baja in articulos and partes[-1] in menores)):
            partes.append(baja)                   # "de la", "en el": el artículo va en minúscula
        else:
            partes.append(palabra[:1].upper() + palabra[1:].lower())
    return " ".join(partes)


def nombre_clave(valor, tax: dict) -> str:
    """Clave para comparar nombres: sin tildes, sin signos y sin palabras genéricas."""
    if es_vacio(valor):
        return ""
    t = sin_tildes(str(valor)).lower()
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    genericos = set(tax.get("genericos_clave", []))
    fichas = [f for f in t.split() if f not in genericos]
    return " ".join(fichas)


def a_etiqueta(valor) -> str | None:
    """Subtipo en forma canónica: minúsculas, sin tildes, palabras unidas con guion bajo."""
    if es_vacio(valor):
        return None
    t = sin_tildes(str(valor)).lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return t[:60] or None
