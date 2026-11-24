"""Carga de configuración: reglas, params, taxonomía y fuentes (captura), usuarios, almacén y el archivo .env.

Ningún número de negocio vive en el código: todo sale de `config/`.
La huella de la configuración queda en cada captura para poder reproducirla.
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[2]
ARCHIVOS = ("reglas", "params", "taxonomia", "fuentes")


def _leer_env(ruta: Path) -> dict[str, str]:
    """Lector mínimo de .env (CLAVE=valor). No pisa variables ya definidas en el entorno."""
    valores: dict[str, str] = {}
    if not ruta.exists():
        return valores
    for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valores[clave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def _dir_config() -> Path:
    return Path(os.environ.get("CAPTURA_CONFIG_DIR") or RAIZ / "config")


@lru_cache(maxsize=1)
def cargar() -> dict:
    """Devuelve el diccionario `p` que usan todos los módulos."""
    carpeta = _dir_config()
    p: dict = {}
    huella = hashlib.sha256()
    for nombre in ARCHIVOS:
        texto = (carpeta / f"{nombre}.yaml").read_text(encoding="utf-8")
        huella.update(texto.replace("\r\n", "\n").encode("utf-8"))
        p[nombre] = yaml.safe_load(texto) or {}
    p["huella"] = huella.hexdigest()[:16]
    # Comerciales: fuera de la huella, porque no cambian el resultado de una captura.
    arch_usuarios = carpeta / "usuarios.yaml"
    p["usuarios"] = (yaml.safe_load(arch_usuarios.read_text(encoding="utf-8")) or {}) if arch_usuarios.exists() else {}
    # Almacén analítico (ETL y esquema estrella). Su huella es propia: cambia lo que alimenta la ficha,
    # no el resultado de una captura.
    arch_almacen = carpeta / "almacen.yaml"
    texto_almacen = arch_almacen.read_text(encoding="utf-8") if arch_almacen.exists() else ""
    p["almacen"] = yaml.safe_load(texto_almacen) or {}
    p["huella_almacen"] = hashlib.sha256(texto_almacen.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]

    # Inmobiliarias y portales que lee el RPA. Archivo propio: es una lista larga y no cambia una captura.
    arch_inmobiliarias = carpeta / "inmobiliarias.yaml"
    p["inmobiliarias"] = (yaml.safe_load(arch_inmobiliarias.read_text(encoding="utf-8")) or {}) if arch_inmobiliarias.exists() else {}
    p["inmobiliarias"].setdefault("sitios", [])
    p["inmobiliarias"].setdefault("descartadas", [])

    env = _leer_env(RAIZ / ".env")

    def secreto(clave: str) -> str:
        return os.environ.get(clave) or env.get(clave, "")

    p["secretos"] = {
        "google_servidor": secreto("GOOGLE_SERVER_KEY"),
        "google_navegador": secreto("GOOGLE_BROWSER_KEY"),
        "google_map_id": secreto("GOOGLE_MAP_ID") or "DEMO_MAP_ID",
        "anthropic": secreto("ANTHROPIC_API_KEY"),
    }
    contacto = secreto("CONTACTO") or p["params"]["red"]["contacto_por_defecto"]
    p["user_agent"] = p["params"]["red"]["user_agent"].format(contacto=contacto)

    rutas = {k: (RAIZ / v) for k, v in p["params"]["rutas"].items()}
    if os.environ.get("CAPTURA_DB"):
        rutas["bd"] = Path(os.environ["CAPTURA_DB"])
    if os.environ.get("ALMACEN_DB"):
        rutas["almacen"] = Path(os.environ["ALMACEN_DB"])
    rutas["config"] = carpeta                         # el administrador edita aquí el catálogo de inmobiliarias
    p["rutas"] = rutas
    p["raiz"] = RAIZ
    return p


def recargar() -> dict:
    cargar.cache_clear()
    return cargar()
