"""Carga de parámetros.

Dos archivos, una sola fuente por tema:
- `config/parametros.yaml`: reglas de negocio oficiales del reto (Venture One). Quedan en `p["reglas"]`.
- `config/ficha.yaml`: parámetros técnicos del motor de ficha (rutas, CRS, trazabilidad, controles).

Ninguna regla de negocio vive en el código. La huella de ambos archivos queda en cada ficha.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[2]
RUTA_PARAMS = RAIZ / "config" / "ficha.yaml"     # params.yaml es de la captura (Momento 1)

SECCIONES = ("reglas_negocio", "ejecucion", "rutas", "esquema", "crs", "limpieza", "deduplicacion",
             "comparables", "precios", "entorno", "ficha")
SECCIONES_REGLAS = ("version", "comparables", "deduplicacion", "estados", "confianza", "entorno", "norma")


def _huella(datos: dict[str, Any]) -> str:
    texto = json.dumps(datos, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def hash_archivo(ruta: Path) -> str | None:
    """sha256 del archivo tal como está en disco (None si no existe)."""
    ruta = Path(ruta)
    return hashlib.sha256(ruta.read_bytes()).hexdigest() if ruta.exists() else None


def _leer_yaml(ruta: Path) -> dict[str, Any]:
    with open(ruta, encoding="utf-8") as f:
        return yaml.safe_load(f)


def cargar_params(ruta: str | Path | None = None, raiz: str | Path | None = None) -> dict[str, Any]:
    ruta = Path(ruta) if ruta else RUTA_PARAMS
    raiz = Path(raiz) if raiz else RAIZ
    params = _leer_yaml(ruta)
    faltan = [s for s in SECCIONES if s not in params]
    if faltan:
        raise KeyError(f"Faltan secciones en {ruta}: {faltan}")
    ruta_reglas = raiz / params["reglas_negocio"]
    reglas = _leer_yaml(ruta_reglas)
    faltan = [s for s in SECCIONES_REGLAS if s not in reglas]
    if faltan:
        raise KeyError(f"Faltan secciones en {ruta_reglas}: {faltan}")
    params["reglas"] = reglas
    params["_meta"] = {
        "hash_params": _huella({k: v for k, v in params.items() if not k.startswith("_")}),
        "version_parametros": str(reglas["version"]),
        "rutas_relativas": dict(params["rutas"]),
        "raiz": raiz,
    }
    params["rutas"] = {k: (raiz / v) for k, v in params["rutas"].items()}
    return params
