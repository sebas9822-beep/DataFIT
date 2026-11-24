"""Etapa 4 · Comparables y estadísticas de precio por m².

Definición oficial: un comparable es un anuncio válido (no duplicado) de la misma zona y el mismo
estado canónico que el inmueble, con área dentro de ±tolerancia_area. Se excluye el grupo de
duplicados que contiene el anuncio del propio inmueble.

Reglas del proyecto que viven aquí:
- Pedido y cierre se calculan y reportan por separado. La referencia usa cierres si hay al menos
  `precios.min_comparables_cierre`; si no, pedidos. Solo se mezclan con
  `precios.permitir_mezcla_pedido_cierre: true`, y la ficha lo marca.
- Si se amplía la tolerancia de área para alcanzar el mínimo, cada paso queda en `flexibilizacion`.
"""
from __future__ import annotations

import statistics

import pandas as pd

VACIO = {"n": 0, "mediana": None, "promedio": None, "minimo": None, "maximo": None,
         "desviacion": None, "coef_variacion": None}


def validos(anuncios: pd.DataFrame) -> pd.DataFrame:
    """Filas únicas (representantes) sin motivo de invalidez."""
    return anuncios[~anuncios["es_duplicado"] & anuncios["motivo_invalido"].isna()]


def grupos_propios(inmueble: pd.Series, anuncios: pd.DataFrame) -> list[str]:
    """Grupos de duplicados que contienen el anuncio del inmueble (por origen o por id_anuncio)."""
    ident = str(inmueble.get("id_anuncio") or "").strip()
    propio = anuncios["origen"] == inmueble.get("origen")
    if ident:
        propio |= anuncios["id_anuncio"].str.strip() == ident
    return sorted(anuncios.loc[propio, "grupo_dup"].unique())


def estadisticas(precios_m2: list[float]) -> dict:
    """COP redondeados a peso entero; desviación muestral (n − 1); CV a 4 decimales (regla oficial)."""
    n = len(precios_m2)
    if n == 0:
        return dict(VACIO)
    promedio = statistics.mean(precios_m2)
    desviacion = statistics.stdev(precios_m2) if n >= 2 else None
    return {
        "n": n,
        "mediana": round(statistics.median(precios_m2)),
        "promedio": round(promedio),
        "minimo": round(min(precios_m2)),
        "maximo": round(max(precios_m2)),
        "desviacion": round(desviacion) if desviacion is not None else None,
        "coef_variacion": round(desviacion / promedio, 4) if desviacion is not None else None,
    }


COINCIDIR_OFICIAL = ("zona", "estado_canonico")


def columnas_coincidentes(p: dict) -> list[str]:
    """Columnas que deben ser iguales entre el inmueble y el comparable. Por defecto, la regla oficial."""
    return list(p["comparables"].get("coincidir") or COINCIDIR_OFICIAL)


def _filtrar(inmueble: pd.Series, base: pd.DataFrame, tolerancia: float, p: dict) -> pd.DataFrame:
    area = float(inmueble["area_m2"])
    mascara = (base["area_m2"] - area).abs() <= tolerancia * area
    for columna in columnas_coincidentes(p):
        mascara &= base[columna] == inmueble[columna]
    for columna, valor in (p["comparables"].get("filtros") or {}).items():
        mascara &= base[columna] == valor
    return base[mascara]


def seleccionar_comparables(inmueble: pd.Series, anuncios: pd.DataFrame, p: dict) -> tuple[pd.DataFrame, dict]:
    """Devuelve (comparables en el orden del CSV, registro del criterio y de la flexibilización)."""
    tolerancia = p["reglas"]["comparables"]["tolerancia_area"]
    excluidos = grupos_propios(inmueble, anuncios) if p["comparables"]["excluir_grupo_propio"] else []
    base = validos(anuncios)
    base = base[~base["grupo_dup"].isin(excluidos)]
    comps = _filtrar(inmueble, base, tolerancia, p)
    flex_cfg = p["comparables"]["flexibilizacion"]
    registro = {"aplicada": False, "activa": bool(flex_cfg["activa"]), "minimo": flex_cfg["minimo"],
                "tolerancia_area_base": tolerancia, "n_base": int(len(comps)), "pasos_aplicados": [],
                "tolerancia_area_final": tolerancia, "n_final": int(len(comps))}
    if flex_cfg["activa"] and len(comps) < flex_cfg["minimo"]:
        for paso in flex_cfg["pasos"]:
            tolerancia = paso["tolerancia_area"]
            comps = _filtrar(inmueble, base, tolerancia, p)
            registro["pasos_aplicados"].append({"nombre": paso["nombre"], "tolerancia_area": tolerancia,
                                                "n": int(len(comps))})
            if len(comps) >= flex_cfg["minimo"]:
                break
        registro.update(aplicada=bool(registro["pasos_aplicados"]), tolerancia_area_final=tolerancia,
                        n_final=int(len(comps)))
    area = float(inmueble["area_m2"])
    registro["rango_area_m2"] = [round(area * (1 - tolerancia), 2), round(area * (1 + tolerancia), 2)]
    registro["grupos_propios_excluidos"] = excluidos
    registro["columnas_coincidentes"] = columnas_coincidentes(p)
    registro["filtros"] = dict(p["comparables"].get("filtros") or {})
    return comps, registro


def _por_tipo(comps: pd.DataFrame, p: dict) -> dict:
    tipos = p["reglas"]["comparables"]["tipos_precio"]
    salida = {t: estadisticas(comps.loc[comps["tipo_precio"] == t, "precio_m2"].tolist()) for t in tipos}
    if p["precios"]["permitir_mezcla_pedido_cierre"]:
        salida["pedido+cierre"] = estadisticas(comps.loc[comps["tipo_precio"].isin(tipos), "precio_m2"].tolist())
    return salida


def elegir_base(por_tipo: dict, p: dict) -> tuple[str | None, bool]:
    """(base, mezcla). Nunca mezcla pedido y cierre sin autorización explícita en params.yaml."""
    cfg = p["precios"]
    if por_tipo.get("cierre", VACIO)["n"] >= cfg["min_comparables_cierre"]:
        return "cierre", False
    if cfg["permitir_mezcla_pedido_cierre"] and por_tipo.get("cierre", VACIO)["n"] > 0:
        return "pedido+cierre", True
    for base in cfg["preferencia_base"][::-1]:          # sin cierres suficientes: pedidos
        if por_tipo.get(base, VACIO)["n"] > 0:
            return base, False
    return None, False


def resumen_por_estado(inmueble: pd.Series, anuncios: pd.DataFrame, p: dict, excluidos: list[str]) -> dict:
    """Mediana de precio/m² por estado en toda la zona (sin filtro de área), como contexto."""
    base = validos(anuncios)
    base = base[(base["zona"] == inmueble["zona"]) & ~base["grupo_dup"].isin(excluidos)]
    resumen = {}
    for estado in p["reglas"]["estados"]:
        sub = base[base["estado_canonico"] == estado]
        tipo, _ = elegir_base(_por_tipo(sub, p), p)
        valores = sub.loc[sub["tipo_precio"] == tipo, "precio_m2"].tolist() if tipo and tipo != "pedido+cierre" \
            else sub["precio_m2"].tolist()
        e = estadisticas(valores)
        resumen[estado] = {"n": e["n"], "mediana": e["mediana"]}
    return resumen


def bloque_mercado(inmueble: pd.Series, anuncios: pd.DataFrame, p: dict) -> dict:
    comps, criterio = seleccionar_comparables(inmueble, anuncios, p)
    por_tipo = _por_tipo(comps, p)
    base, mezcla = elegir_base(por_tipo, p)
    if base is None:
        referencia = dict(VACIO)
    else:
        referencia = por_tipo[base]
    en_referencia = comps["tipo_precio"].isin(["pedido", "cierre"] if mezcla else [base])
    lista = comps.assign(_en_ref=en_referencia).sort_values(["precio_m2", "fila_origen"], kind="mergesort")
    return {
        "estado_referencia": inmueble["estado_canonico"],
        "precio_m2": referencia,
        "comparables": [
            {"origen": c["origen"], "id_anuncio": c["id_anuncio"], "fuente": c["fuente"],
             "area_m2": float(c["area_m2"]), "precio": round(c["precio"]), "precio_m2": round(c["precio_m2"]),
             "tipo_precio": c["tipo_precio"], "en_referencia": bool(c["_en_ref"])}
            for _, c in lista.iterrows()
        ],
        "precio_m2_por_estado_en_zona": resumen_por_estado(inmueble, anuncios, p, criterio["grupos_propios_excluidos"]),
        "base_referencia": base,
        "mezcla_pedido_cierre": mezcla,
        "precio_m2_por_tipo": {t: v for t, v in por_tipo.items() if t != "pedido+cierre"},
        "criterio_comparables": criterio,
        "_comparables_df": comps,
    }
