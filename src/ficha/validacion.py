"""Validación de entradas, invariantes de trazabilidad y validación contra el cálculo manual.

1. Entradas: esquema de anuncios e inmuebles con pandera. Un inmueble que no cumple no se procesa
   y queda con su `motivo_exclusion`; un anuncio con `origen` repetido detiene el proceso.
2. Invariantes: todo anuncio fuera del cálculo tiene motivo; los conteos cuadran.
3. Cálculo manual: las fichas se comparan con `validacion/esperado.json` usando el validador oficial.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

try:                                    # pandera >= 0.24
    import pandera.pandas as pa
except ImportError:                     # versiones anteriores, o entorno sin pandera
    try:
        import pandera as pa
    except ImportError:                 # pragma: no cover - solo el lote oficial valida con pandera
        pa = None

try:
    from pandera.errors import SchemaErrors
except ImportError:                     # pragma: no cover
    class SchemaErrors(Exception):
        pass

COLUMNAS_EXCLUSION = ["entidad", "id", "fila_origen", "motivo_exclusion", "detalle"]


def validar_anuncios_crudos(df: pd.DataFrame) -> None:
    """La clave única de cada fila es `origen` (archivo:hoja:fila). Sin ella no hay trazabilidad."""
    esquema = pa.DataFrameSchema(
        {"origen": pa.Column(str, pa.Check.str_length(min_value=1), nullable=False, unique=True),
         "zona": pa.Column(str, pa.Check.str_length(min_value=1), nullable=False)},
        strict=False)
    esquema.validate(df)


def _esquema_inmuebles(p: dict) -> pa.DataFrameSchema:
    b = p["crs"]["bbox_area_estudio"]
    return pa.DataFrameSchema(
        {
            "id_inmueble": pa.Column(str, pa.Check.str_length(min_value=1), nullable=False, unique=True),
            "zona": pa.Column(str, pa.Check.str_length(min_value=1), nullable=False),
            "municipio": pa.Column(str, pa.Check.str_length(min_value=1), nullable=False),
            "estado": pa.Column(str, nullable=False),
            "lat": pa.Column(float, pa.Check.in_range(b["lat_min"], b["lat_max"]), nullable=False, coerce=True),
            "lon": pa.Column(float, pa.Check.in_range(b["lon_min"], b["lon_max"]), nullable=False, coerce=True),
            "area_m2": pa.Column(float, pa.Check.gt(0), nullable=False, coerce=True),
        },
        strict=False,
    )


def validar_inmuebles(df: pd.DataFrame, p: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Devuelve (inmuebles válidos, exclusiones con su motivo)."""
    esquema = _esquema_inmuebles(p)
    exclusiones: list[dict] = []
    try:
        validos = esquema.validate(df, lazy=True)
    except SchemaErrors as err:
        fallas = err.failure_cases
        sin_fila = fallas[fallas["index"].isna()]
        if not sin_fila.empty:          # falla estructural (p. ej. columna ausente): no se puede continuar
            detalle = sin_fila[["column", "check"]].to_dict("records")
            raise ValueError(f"Esquema de inmuebles inválido: {detalle}") from err
        motivos: dict = {}
        for _, f in fallas.iterrows():
            motivos.setdefault(f["index"], []).append(f"{f['column']}: {f['check']} (valor={f['failure_case']})")
        for idx, lista in sorted(motivos.items()):
            fila = df.loc[idx]
            exclusiones.append({"entidad": "inmueble", "id": fila.get("id_inmueble"), "fila_origen": fila.get("fila_origen"),
                                "motivo_exclusion": "validacion_esquema", "detalle": " | ".join(lista)})
        validos = esquema.validate(df.drop(index=list(motivos)))
    return validos, pd.DataFrame(exclusiones, columns=COLUMNAS_EXCLUSION)


def verificar_invariantes(anuncios: pd.DataFrame) -> None:
    """Si fallan, hay un error de programación: ningún anuncio puede salir del cálculo sin motivo."""
    esquema = pa.DataFrameSchema(
        {"origen": pa.Column(str, nullable=False, unique=True),
         "es_duplicado": pa.Column(bool),
         "precio_m2": pa.Column(float, nullable=True)},
        checks=[
            pa.Check(lambda d: (d["motivo_exclusion"] != "") == (d["es_duplicado"] | d["motivo_invalido"].notna()),
                     error="todo anuncio fuera del cálculo tiene motivo_exclusion, y solo esos"),
            pa.Check(lambda d: d["es_duplicado"] | d["motivo_invalido"].notna() | (d["precio_m2"] > 0),
                     error="todo anuncio utilizable tiene precio por m2 positivo"),
            pa.Check(lambda d: (d["es_duplicado"] == (d["regla_dup"] != "")),
                     error="todo duplicado tiene regla aplicada"),
        ],
        strict=False,
    )
    esquema.validate(anuncios)


def motivo_exclusion(anuncios: pd.DataFrame) -> pd.Series:
    """'duplicado' para las filas absorbidas; el motivo oficial de invalidez para las únicas inválidas."""
    return pd.Series(["duplicado" if d else (m if isinstance(m, str) else "")
                      for d, m in zip(anuncios["es_duplicado"], anuncios["motivo_invalido"], strict=True)],
                     index=anuncios.index)


def tabla_exclusiones(anuncios: pd.DataFrame, exclusiones_inmuebles: pd.DataFrame) -> pd.DataFrame:
    """Un solo listado de todo registro que no entró al cálculo, con su motivo."""
    filas = []
    for _, a in anuncios[anuncios["motivo_exclusion"] != ""].iterrows():
        if a["es_duplicado"]:
            detalle = f"representante={a['id_representante']}; regla={a['regla_dup']}; coincide_con={a['coincide_con']}"
        else:
            detalle = f"estado_texto='{a['estado_texto']}'" if a["motivo_exclusion"] == "estado_sin_clasificar" else ""
        filas.append({"entidad": "anuncio", "id": a["origen"], "fila_origen": a["fila_origen"],
                      "motivo_exclusion": a["motivo_exclusion"], "detalle": detalle})
    tabla = pd.DataFrame(filas, columns=COLUMNAS_EXCLUSION)
    return pd.concat([tabla, exclusiones_inmuebles], ignore_index=True) if len(exclusiones_inmuebles) else tabla


def cargar_validador(ruta: Path):
    """Importa el validador oficial (validacion/validar.py del repositorio del reto)."""
    spec = importlib.util.spec_from_file_location("validar_oficial", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def validar_contra_esperado(fichas: list[dict], ruta_esperado: Path, ruta_validador: Path) -> pd.DataFrame:
    """Una fila por inmueble del cálculo manual: ok y diferencias según el validador oficial."""
    esperado = json.loads(Path(ruta_esperado).read_text(encoding="utf-8"))
    validador = cargar_validador(ruta_validador)
    por_id = {f["id_inmueble"]: f for f in fichas}
    filas = []
    for id_inmueble, esp in esperado["inmuebles"].items():
        if id_inmueble not in por_id:
            filas.append({"id_inmueble": id_inmueble, "ok": False, "n_diferencias": 1, "diferencias": "falta la ficha"})
            continue
        errores = validador.validar(por_id[id_inmueble], esp, esperado["_tolerancias"])
        filas.append({"id_inmueble": id_inmueble, "ok": not errores, "n_diferencias": len(errores),
                      "diferencias": " | ".join(errores)})
    return pd.DataFrame(filas, columns=["id_inmueble", "ok", "n_diferencias", "diferencias"])
