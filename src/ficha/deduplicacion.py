"""Etapa 3 · Deduplicación con auditoría (regla oficial de parametros.yaml).

Se recorre el CSV en orden. Una fila es duplicada si:
- R1 · su `id_anuncio` (no vacío) ya apareció, o
- R2 · su combinación `deduplicacion.claves_sin_id` (zona, área, precio) ya apareció.
Las filas sin algún valor de la clave solo se deduplican por id. Se conserva la primera fila del
grupo (`representante: primera_fila`, regla oficial).

Nada se borra: cada fila absorbida queda en `auditoria/duplicados.csv` con la regla aplicada, la fila
con la que coincidió y su puntaje (1.0: coincidencia exacta). Un grupo con ids distintos se marca
`ambiguo`: puede ser el mismo inmueble en varias inmobiliarias o unidades iguales del mismo edificio.

Aparte, como control y sin fusionar nada, se listan parejas casi iguales que la regla exacta no
agrupa (`auditoria/duplicados_revision.csv`).
"""
from __future__ import annotations

import pandas as pd

COLUMNAS_PARES = ["grupo_dup", "origen_representante", "origen_duplicado", "coincide_con", "id_representante",
                  "id_duplicado", "fuente_representante", "fuente_duplicado", "regla_aplicada", "puntaje",
                  "grupo_ambiguo"]
COLUMNAS_REVISION = ["origen_a", "origen_b", "zona", "id_a", "id_b", "estado_a", "estado_b", "area_a", "area_b",
                     "precio_a", "precio_b", "dif_area_pct", "dif_precio_pct", "puntaje", "accion"]
R1, R2 = "R1_id_anuncio", "R2_claves_sin_id"


def _clave(fila: pd.Series, columnas: list[str]):
    valores = []
    for c in columnas:
        v = fila[c]
        if v is None or (isinstance(v, float) and v != v) or (isinstance(v, str) and v.strip() == ""):
            return None
        valores.append(v)
    return tuple(valores)


def _agrupar(df: pd.DataFrame, columnas: list[str]) -> tuple[dict, dict, dict]:
    """Recorre en orden. Devuelve (grupo de cada origen, regla de cada origen, fila con la que coincidió)."""
    grupo_de, regla_de, coincide = {}, {}, {}
    por_id: dict[str, tuple[str, str]] = {}
    por_clave: dict[tuple, tuple[str, str]] = {}
    for _, fila in df.iterrows():
        origen, ident = fila["origen"], str(fila["id_anuncio"]).strip()
        clave = _clave(fila, columnas)
        destino, regla = None, ""
        if ident and ident in por_id:
            destino, regla = por_id[ident], R1
        elif clave is not None and clave in por_clave:
            destino, regla = por_clave[clave], R2
        if destino is None:
            grupo_de[origen], regla_de[origen], coincide[origen] = origen, "", ""
            registro = (origen, origen)
        else:
            grupo_de[origen], regla_de[origen], coincide[origen] = destino[0], regla, destino[1]
            registro = (destino[0], origen)
        if ident:
            por_id.setdefault(ident, registro)
        if clave is not None:
            por_clave.setdefault(clave, registro)
    return grupo_de, regla_de, coincide


def _elegir_representantes(df: pd.DataFrame, modo: str) -> dict[str, str]:
    """Representante de cada grupo. `primera_fila` es la regla oficial."""
    if modo == "primera_fila":
        return df.drop_duplicates("grupo_dup", keep="first").set_index("grupo_dup")["origen"].to_dict()
    if modo == "mas_completo":
        orden = df.assign(_valido=df["motivo_invalido"].isna(), _pos=range(len(df))).sort_values(
            ["grupo_dup", "_valido", "completitud", "_pos"], ascending=[True, False, False, True], kind="mergesort")
        return orden.drop_duplicates("grupo_dup", keep="first").set_index("grupo_dup")["origen"].to_dict()
    raise ValueError(f"deduplicacion.representante no soportado: {modo}")


def _revision_tolerancias(unicos: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Parejas casi iguales entre filas únicas de la misma zona. Solo se listan; no se fusionan."""
    if not cfg.get("activa"):
        return pd.DataFrame(columns=COLUMNAS_REVISION)
    tol_a, tol_p, pesos = cfg["tolerancia_area_pct"], cfg["tolerancia_precio_pct"], cfg["pesos_puntaje"]
    filas = []
    base = unicos.dropna(subset=["area_m2", "precio"])
    for zona, bloque in base.groupby("zona", sort=True):
        registros = bloque.sort_values(["area_m2", "origen"], kind="mergesort").to_dict("records")
        for i, a in enumerate(registros):
            for b in registros[i + 1:]:
                dif_a = abs(a["area_m2"] - b["area_m2"]) / max(a["area_m2"], b["area_m2"])
                if dif_a > tol_a:
                    break
                dif_p = abs(a["precio"] - b["precio"]) / max(a["precio"], b["precio"])
                if dif_p > tol_p:
                    continue
                cerc_a = 1 - dif_a / tol_a if tol_a else float(dif_a == 0)
                cerc_p = 1 - dif_p / tol_p if tol_p else float(dif_p == 0)
                puntaje = (pesos["area"] * cerc_a + pesos["precio"] * cerc_p) / (pesos["area"] + pesos["precio"])
                x, y = sorted([a, b], key=lambda r: r["fila_origen"])
                filas.append({"origen_a": x["origen"], "origen_b": y["origen"], "zona": zona,
                              "id_a": x["id_anuncio"], "id_b": y["id_anuncio"],
                              "estado_a": x["estado_canonico"], "estado_b": y["estado_canonico"],
                              "area_a": x["area_m2"], "area_b": y["area_m2"], "precio_a": x["precio"],
                              "precio_b": y["precio"], "dif_area_pct": round(dif_a, 4), "dif_precio_pct": round(dif_p, 4),
                              "puntaje": round(puntaje, 4), "accion": "revisar (no se fusiona)"})
    return pd.DataFrame(filas, columns=COLUMNAS_REVISION)


def deduplicar(df: pd.DataFrame, p: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Devuelve (anuncios con marcas de duplicado, parejas absorbidas, candidatos a revisión)."""
    columnas = p["reglas"]["deduplicacion"]["claves_sin_id"]
    cfg = p["deduplicacion"]
    out = df.copy()
    grupo_de, regla_de, coincide = _agrupar(out, columnas)
    out["grupo_dup"] = out["origen"].map(grupo_de)
    representante = _elegir_representantes(out, cfg["representante"])
    out["id_representante"] = out["grupo_dup"].map(representante)
    out["es_duplicado"] = out["origen"] != out["id_representante"]
    out["regla_dup"] = out["origen"].map(regla_de)
    out.loc[out["es_duplicado"] & (out["regla_dup"] == ""), "regla_dup"] = "reasignado_por_completitud"
    out.loc[~out["es_duplicado"], "regla_dup"] = ""
    out["coincide_con"] = out["origen"].map(coincide).where(out["es_duplicado"], "")
    out["puntaje_dup"] = out["es_duplicado"].map({True: 1.0, False: None})

    ids = out.loc[out["id_anuncio"].str.strip() != ""].groupby("grupo_dup")["id_anuncio"].agg(lambda s: sorted(set(s)))
    out["ids_grupo"] = out["grupo_dup"].map(ids).map(lambda v: "; ".join(v) if isinstance(v, list) else "")
    out["grupo_ambiguo"] = out["grupo_dup"].map(ids).map(lambda v: isinstance(v, list) and len(v) > 1)
    out["n_miembros"] = out.groupby("grupo_dup")["origen"].transform("size")
    out["origenes_grupo"] = out.groupby("grupo_dup")["origen"].transform(lambda s: "; ".join(s))

    rep = out.set_index("origen")
    filas = []
    for _, d in out[out["es_duplicado"]].iterrows():
        r = rep.loc[d["id_representante"]]
        filas.append({"grupo_dup": d["grupo_dup"], "origen_representante": d["id_representante"],
                      "origen_duplicado": d["origen"], "coincide_con": d["coincide_con"],
                      "id_representante": r["id_anuncio"], "id_duplicado": d["id_anuncio"],
                      "fuente_representante": r["fuente"], "fuente_duplicado": d["fuente"],
                      "regla_aplicada": d["regla_dup"], "puntaje": 1.0, "grupo_ambiguo": bool(d["grupo_ambiguo"])})
    pares = pd.DataFrame(filas, columns=COLUMNAS_PARES)
    revision = _revision_tolerancias(out[~out["es_duplicado"]], cfg["revision_tolerancias"])
    return out, pares, revision
