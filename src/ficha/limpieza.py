"""Etapa 2 · Limpieza y normalización de anuncios.

Sigue el procedimiento oficial del cálculo manual: normalizar el estado (minúsculas, sin tildes,
sin "?", sin espacios extremos) y mapearlo a un estado canónico de parametros.yaml; convertir área
y precio; calcular precio/m²; asignar el primer motivo de invalidez en el orden oficial.

Ningún registro se elimina: los inválidos conservan su `motivo_invalido`, y cada faltante o valor
no reconocido queda como evento en la bitácora.
"""
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

COLUMNAS_BITACORA = ["origen", "fila_origen", "campo", "evento", "accion", "detalle"]
CAMPOS_COMPLETITUD = ["id_anuncio", "fecha", "barrio", "area_m2", "precio", "habitaciones", "banos",
                      "parqueadero", "estado_canonico"]


def normalizar_texto(texto) -> str:
    """Minúsculas, sin tildes, sin "?" y con espacios simples (regla oficial)."""
    if texto is None or (isinstance(texto, float) and np.isnan(texto)):
        return ""
    sin_tildes = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.replace("?", " ").lower().split())


def normalizar_estado(texto, estados: dict[str, list[str]], sin_mapeo: str = "sin_clasificar") -> str:
    """Estado canónico según parametros.yaml; `sin_mapeo` si el texto no tiene mapeo."""
    limpio = normalizar_texto(texto)
    for canonico, variantes in estados.items():
        if limpio in (normalizar_texto(v) for v in variantes):
            return canonico
    return sin_mapeo


def a_numero(texto) -> float:
    """'$ 450.000.000' -> 450000000 ; '85,5 m2' -> 85.5 ; '70.4' -> 70.4 ; '' -> NaN."""
    if texto is None or str(texto).strip() == "":
        return np.nan
    t = re.sub(r"(m2|m²|mts2?|metros?)", "", str(texto).lower())   # quitar unidades antes que el '2' de m2
    t = re.sub(r"[^\d,.\-]", "", t)
    if t.count(".") > 1 or (t.count(".") == 1 and len(t.split(".")[-1]) == 3 and "," not in t):
        t = t.replace(".", "")          # puntos como separador de miles
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return np.nan


def a_positivo(texto) -> float:
    """Número estrictamente positivo; cero, negativo o ilegible quedan NaN (regla oficial)."""
    n = a_numero(texto)
    return n if n == n and n > 0 else np.nan


def a_fecha(serie: pd.Series, formatos: list[str]) -> pd.Series:
    textos = serie.astype(str).str.strip()
    resultado = pd.Series(pd.NaT, index=serie.index, dtype="datetime64[ns]")
    for formato in formatos:
        pendientes = resultado.isna()
        if not pendientes.any():
            break
        resultado[pendientes] = pd.to_datetime(textos[pendientes], format=formato, errors="coerce")
    return resultado


def motivo_invalido(fila: pd.Series, reglas: dict, sin_mapeo: str, exigir_estado: bool = True,
                    validar_rango: bool = True) -> str | None:
    """Primer motivo por el que el anuncio no sirve como comparable, en el orden oficial; None si sirve.

    `exigir_estado=False` y `validar_rango=False` existen para bases de mercado que no informan el
    estado del inmueble o cuyo precio no es de venta (almacen.yaml › bases_mercado). Con los valores
    por defecto la regla es exactamente la oficial.
    """
    if pd.isna(fila["area_m2"]):
        return "sin_area"
    if pd.isna(fila["precio"]):
        return "sin_precio"
    minimo, maximo = reglas["comparables"]["rango_precio_m2_valido"]
    if validar_rango and not minimo <= fila["precio_m2"] <= maximo:
        return "precio_m2_fuera_de_rango"
    if exigir_estado and fila["estado_canonico"] == sin_mapeo:
        return "estado_sin_clasificar"
    if fila["tipo_precio"] not in reglas["comparables"]["tipos_precio"]:
        return "tipo_precio_no_aceptado"
    return None


def limpiar_anuncios(df: pd.DataFrame, p: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Devuelve (anuncios normalizados en el orden del CSV, bitácora de calidad)."""
    reglas, cfg = p["reglas"], p["limpieza"]
    sin_mapeo = cfg["estado_sin_mapeo"]
    crudo = df.copy()
    out = df.copy()

    out["estado_texto"] = crudo["estado"]
    out["estado_canonico"] = crudo["estado"].map(lambda v: normalizar_estado(v, reglas["estados"], sin_mapeo))
    out["tipo_precio"] = crudo["tipo_precio"].map(normalizar_texto)
    out["barrio_normalizado"] = crudo["barrio"].map(normalizar_texto)
    out["fecha"] = a_fecha(crudo["fecha"], cfg["formatos_fecha"])
    out["area_m2"] = crudo["area_m2"].map(a_positivo).astype(float)
    out["precio"] = crudo["precio"].map(a_positivo).astype(float)
    for col in ("habitaciones", "banos", "parqueadero"):
        out[col] = crudo[col].map(a_numero).astype(float)
    out["precio_m2"] = out["precio"] / out["area_m2"]
    # Serie de objetos explícita: sin motivo es None (pandas 3 convertiría None en NaN al inferir el tipo)
    out["motivo_invalido"] = pd.Series([motivo_invalido(f, reglas, sin_mapeo) for _, f in out.iterrows()],
                                       index=out.index, dtype=object)
    out["completitud"] = pd.concat([
        (crudo["id_anuncio"].str.strip() != ""), out["fecha"].notna(),
        (crudo["barrio"].str.strip() != ""), out["area_m2"].notna(), out["precio"].notna(),
        out["habitaciones"].notna(), out["banos"].notna(), out["parqueadero"].notna(),
        out["estado_canonico"] != sin_mapeo,
    ], axis=1).mean(axis=1).round(4)

    eventos: list[dict] = []
    convertido = {"fecha": out["fecha"], "area_m2": out["area_m2"], "precio": out["precio"],
                  "habitaciones": out["habitaciones"], "banos": out["banos"], "parqueadero": out["parqueadero"],
                  "estado": out["estado_canonico"].where(out["estado_canonico"] != sin_mapeo)}
    for campo in cfg["campos_bitacora"]:
        original = crudo[campo].astype(str).str.strip()
        vacio = original == ""
        no_reconocido = ~vacio & convertido[campo].isna() if campo in convertido else pd.Series(False, index=out.index)
        for idx in out.index[vacio]:
            eventos.append({"origen": out.at[idx, "origen"], "fila_origen": out.at[idx, "fila_origen"],
                            "campo": campo, "evento": "faltante", "accion": "se conserva", "detalle": ""})
        for idx in out.index[no_reconocido]:
            eventos.append({"origen": out.at[idx, "origen"], "fila_origen": out.at[idx, "fila_origen"],
                            "campo": campo, "evento": "valor_no_reconocido", "accion": "se conserva",
                            "detalle": original[idx]})
    bitacora = pd.DataFrame(eventos, columns=COLUMNAS_BITACORA)
    return out, bitacora.sort_values(["fila_origen", "campo"], kind="mergesort").reset_index(drop=True)
