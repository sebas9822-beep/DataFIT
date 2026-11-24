"""Indicadores descriptivos del entorno y resumen por categoría.

Miden qué hay y a qué distancia. No miden conveniencia de compra ni rentabilidad.
La base `abiertas` usa solo fuentes que se pueden guardar para siempre; la base `todas` incluye
Google y por eso expira con la regla de 30 días.
"""
from __future__ import annotations

import math

import pandas as pd


def entidades(df: pd.DataFrame) -> pd.DataFrame:
    """Una fila por lugar real: de cada entidad, el registro más cercano."""
    validos = df[~df["excluido"] & df["distancia_m"].notna()]
    return validos.sort_values("distancia_m", kind="stable").drop_duplicates("entidad_id")


def resumen_categoria(df: pd.DataFrame, saturados: dict[str, bool], expira_google: str | None) -> list[dict]:
    """Conteo, excluidos y distancia mínima por categoría y fuente (todas las capas)."""
    filas = []
    if not df.empty:
        for (cat, fuente), g in df.groupby(["categoria", "fuente"], sort=True):
            ok = g[~g["excluido"]]
            dmin = ok["distancia_m"].min()
            filas.append({"categoria": cat, "fuente": fuente, "conteo": int(len(ok)),
                          "excluidos": int(len(g) - len(ok)),
                          "distancia_min_m": None if pd.isna(dmin) else float(dmin),
                          "saturado": int(bool(saturados.get(cat)) and fuente == "google"),
                          "expira_en": expira_google if fuente == "google" else None})
    presentes = {(f["categoria"], f["fuente"]) for f in filas}
    for cat, sat in saturados.items():
        if sat and (cat, "google") not in presentes:
            filas.append({"categoria": cat, "fuente": "google", "conteo": 0, "excluidos": 0,
                          "distancia_min_m": None, "saturado": 1, "expira_en": expira_google})
    return filas


def _de_base(df: pd.DataFrame, radio: int, p: dict, base: str, expira: str | None) -> list[dict]:
    reglas = p["reglas"]
    anillos = sorted(reglas["captura"]["anillos_m"])
    ind = reglas["indicadores"]
    filas: list[dict] = []

    def agregar(nombre: str, valor, unidad: str, detalle: dict | None = None) -> None:
        filas.append({"indicador": nombre, "base": base, "valor": None if valor is None else float(valor),
                      "unidad": unidad, "detalle": detalle, "expira_en": expira})

    lugares = entidades(df[df["en_mapa"]])
    conteos_radio: dict[str, int] = {}
    for cat in p["taxonomia"]["categorias"]:
        de_cat = lugares[lugares["categoria"] == cat]
        for limite in anillos:
            agregar(f"conteo_{cat}_{limite}m", int((de_cat["distancia_m"] <= limite).sum()), "lugares")
        conteos_radio[cat] = int((de_cat["distancia_m"] <= radio).sum())
        dmin = de_cat["distancia_m"].min()
        agregar(f"dist_min_{cat}_m", None if pd.isna(dmin) else round(float(dmin), 1), "m")
    for cat in ind["categorias_cobertura_basica"]:
        de_cat = lugares[lugares["categoria"] == cat]
        agregar(f"cobertura_basica_{cat}", int((de_cat["distancia_m"] <= ind["cobertura_basica_m"]).any()),
                "si_no", {"umbral_m": ind["cobertura_basica_m"]})
    usados = [conteos_radio.get(c, 0) for c in ind["categorias_diversidad"]]
    total = sum(usados)
    shannon = -sum((n / total) * math.log(n / total) for n in usados if n) if total else None
    agregar("diversidad_shannon", None if shannon is None else round(shannon, 3), "indice",
            {"categorias": dict(zip(ind["categorias_diversidad"], usados))})
    agregar("lugares_unicos", int(len(lugares)), "lugares")
    return filas


def calcular(df: pd.DataFrame, ofertas: list[dict], predio: dict | None, p: dict, radio: int,
             control: dict | None, expira_google: str | None) -> list[dict]:
    """Devuelve las filas de la tabla `indicadores`."""
    abiertas = df[df["fuente"] != "google"]
    filas = _de_base(abiertas, radio, p, "abiertas", None)
    if (df["fuente"] == "google").any():
        filas += _de_base(df, radio, p, "todas", expira_google)

    def agregar(nombre: str, valor, unidad: str, detalle: dict | None = None) -> None:
        filas.append({"indicador": nombre, "base": "abiertas", "valor": None if valor is None else float(valor),
                      "unidad": unidad, "detalle": detalle, "expira_en": None})

    # Capas masivas, que se resumen aparte para no inflar las categorías.
    area_km2 = math.pi * (radio / 1000) ** 2
    registro = df[df["capa"] == "establecimientos"]
    if len(registro):
        activos = registro[~registro["excluido"]]
        agregar("establecimientos_activos", int(len(activos)), "establecimientos",
                {"sin_establecimiento_fisico": int(len(registro) - len(activos)),
                 "por_categoria": {str(k): int(v) for k, v in activos["categoria"].value_counts().items()},
                 "por_subtipo": {str(k): int(v) for k, v in activos["subtipo"].value_counts().head(12).items()}})
        agregar("establecimientos_por_km2", round(len(activos) / area_km2, 1), "establecimientos/km2")
    camaras = df[(df["capa"] == "camaras_cctv") & ~df["excluido"]]
    if len(camaras):
        agregar("camaras_cctv", int(len(camaras)), "camaras")

    if predio and predio.get("estacion_masiva_m") is not None:
        agregar("estacion_masiva_mas_cercana_m", predio["estacion_masiva_m"], "m",
                {"nombre": predio.get("estacion_masiva"), "linea": predio.get("estacion_masiva_linea"),
                 "dentro_del_radio": bool(predio["estacion_masiva_m"] <= radio)})

    # Oferta histórica (precio pedido): referencia descriptiva, nunca precio de cierre.
    if ofertas:
        of = pd.DataFrame(ofertas)
        validas = of[of["motivo_exclusion"].isna()]
        anios = sorted({int(a) for a in validas["anio"].dropna().unique()})
        agregar("ofertas_historicas", int(len(validas)), "ofertas",
                {"excluidas": int(len(of) - len(validas)),
                 "por_operacion": {str(k): int(v) for k, v in validas["operacion"].value_counts().items()},
                 "por_tipo": {str(k): int(v) for k, v in validas["tipo_inmueble"].value_counts().items()},
                 "anios": anios})
        for (oper, tipo), g in validas.groupby(["operacion", "tipo_inmueble"]):
            m2 = pd.to_numeric(g["precio_m2"], errors="coerce").dropna()
            if oper != "venta" or len(m2) < 3:
                continue
            agregar(f"oferta_pedido_m2_mediana_{tipo}", round(float(m2.median())), "COP/m2",
                    {"n": int(len(m2)), "tipo_precio": "pedido", "p25": round(float(m2.quantile(0.25))),
                     "p75": round(float(m2.quantile(0.75))),
                     "anios": sorted({int(a) for a in g["anio"].dropna().unique()}),
                     "aviso": "precio de oferta histórica; no es precio de cierre"})

    if control:
        agregar("control_distancia_9377_max_dif_m", control["max_dif_m"], "m", control)
    return filas
