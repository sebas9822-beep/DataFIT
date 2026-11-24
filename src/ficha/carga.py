"""Etapa 1 · Ingesta: lee CSV y GeoJSON sin transformar su contenido.

- Los encabezados se llevan a su nombre canónico con el bloque `esquema` de params.yaml.
- Todo se lee como texto: la conversión y la validación ocurren después, con registro.
- El CRS de cada capa se verifica y se registra. Nunca se asigna un CRS en silencio.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

try:                                    # solo las usa el lote oficial (leer_capa); el almacén no las necesita
    import geopandas as gpd
    from shapely.geometry import box
except ImportError:                     # pragma: no cover - entorno sin pila geográfica
    gpd = None
    box = None


def _norm_col(nombre: str) -> str:
    return str(nombre).strip().lower()


def _aplicar_esquema(df: pd.DataFrame, mapa: dict[str, str], origen: str) -> pd.DataFrame:
    """Renombra columnas del archivo a su nombre canónico y exige las del esquema."""
    df = df.copy()
    df.columns = [_norm_col(c) for c in df.columns]
    faltan = sorted(_norm_col(v) for v in mapa.values() if _norm_col(v) not in df.columns)
    if faltan:
        raise ValueError(f"Columnas faltantes en {origen}: {faltan}")
    renombrar = {_norm_col(real): canon for canon, real in mapa.items() if _norm_col(real) != canon}
    return df.rename(columns=renombrar)


def _leer_csv_texto(ruta: Path) -> pd.DataFrame:
    return pd.read_csv(ruta, dtype=str, keep_default_na=False, encoding="utf-8")


def leer_anuncios(ruta: Path, p: dict) -> pd.DataFrame:
    """Anuncios como texto, en el orden del CSV (la deduplicación oficial depende de ese orden)."""
    df = _aplicar_esquema(_leer_csv_texto(ruta), p["esquema"]["anuncios"], "anuncios")
    df.insert(0, "fila_origen", range(2, len(df) + 2))      # fila en el CSV (la 1 es el encabezado)
    return df


def leer_inmuebles(ruta: Path, p: dict) -> pd.DataFrame:
    df = _aplicar_esquema(_leer_csv_texto(ruta), p["esquema"]["inmuebles"], "inmuebles")
    df.insert(0, "fila_origen", range(2, len(df) + 2))
    return df


def _crs_declarado(ruta: Path) -> str:
    """Cómo declara el archivo su CRS. Un GeoJSON sin miembro 'crs' es WGS84 por RFC 7946."""
    if ruta.suffix.lower() in {".geojson", ".json"}:
        with open(ruta, encoding="utf-8") as f:
            miembro = json.load(f).get("crs")
        return f"declarado: {miembro}" if miembro else "implícito WGS84 (RFC 7946, sin miembro 'crs')"
    return "según el archivo"


def leer_metadata_geojson(ruta: Path) -> dict:
    with open(ruta, encoding="utf-8") as f:
        return json.load(f).get("metadata") or {}


def leer_capa(nombre: str, ruta: Path, p: dict, obligatoria: bool = True) -> tuple[gpd.GeoDataFrame | None, dict]:
    """Devuelve (capa, registro de auditoría). Una capa opcional ausente devuelve (None, registro)."""
    cfg = p["crs"]
    base = {"capa": nombre, "archivo": ruta.name, "crs_calculo": cfg["metrico"]}
    if not ruta.exists():
        if obligatoria:
            raise FileNotFoundError(f"Falta la capa obligatoria '{nombre}': {ruta}")
        return None, {**base, "estado": "pendiente", "crs_declarado": None, "crs_usado": None,
                      "crs_asumido": False, "registros": 0, "geometrias": "", "limites_wgs84": None,
                      "dentro_area_estudio": None}
    if gpd is None:
        raise RuntimeError("Leer capas GeoJSON como GeoDataFrame requiere geopandas (entorno conda venture-one).")
    gdf = gpd.read_file(ruta)
    asumido = False
    if gdf.crs is None:
        if not cfg["asumir_entrada_si_falta"]:
            raise ValueError(f"La capa '{nombre}' ({ruta.name}) no tiene CRS. Verifique su CRS nativo y "
                             "declárelo, o active crs.asumir_entrada_si_falta en params.yaml.")
        gdf = gdf.set_crs(cfg["entrada"])
        asumido = True
    b = cfg["bbox_area_estudio"]
    limites, en_zona = None, None
    if not gdf.empty:
        limites = [round(float(v), 6) for v in gdf.to_crs("EPSG:4326").total_bounds]
        en_zona = bool(box(*limites).intersects(box(b["lon_min"], b["lat_min"], b["lon_max"], b["lat_max"])))
        if not en_zona:
            raise ValueError(f"La capa '{nombre}' ({ruta.name}) usa {gdf.crs.to_string()} pero sus coordenadas "
                             f"{limites} quedan fuera del área de estudio. Revise el CRS nativo.")
    return gdf, {**base, "estado": "cargada", "crs_declarado": _crs_declarado(ruta),
                 "crs_usado": gdf.crs.to_string(), "crs_asumido": asumido, "registros": int(len(gdf)),
                 "geometrias": ",".join(sorted(set(gdf.geom_type.dropna()))), "limites_wgs84": limites,
                 "dentro_area_estudio": en_zona}
