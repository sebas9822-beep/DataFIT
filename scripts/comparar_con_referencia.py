"""Compara las fichas de la aplicación con las de la implementación de referencia del reto.

La referencia (`reto_oficial/src/ficha`) corre en un proceso aparte, porque su paquete también se
llama `ficha`. Se comparan todos los campos del esquema oficial; los campos adicionales de la
aplicación se ignoran, y `metadatos.fecha_generacion` también (cambia con el día).

Uso:  python scripts/comparar_con_referencia.py [carpeta_fichas]   (por defecto salidas/fichas)
Sale con código 1 si hay diferencias.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
REFERENCIA = RAIZ / "reto_oficial"
IGNORAR = {("metadatos", "fecha_generacion")}
# El proceso hijo escribe en UTF-8 aunque la consola de Windows use cp1252.
ENTORNO_UTF8 = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from ficha.ficha import cargar_contexto, cargar_inmuebles, generar_ficha
ctx = cargar_contexto()
print(json.dumps([generar_ficha(i, ctx) for i in cargar_inmuebles(ctx["params"])], ensure_ascii=False))
"""


def fichas_referencia() -> dict[str, dict]:
    salida = subprocess.run([sys.executable, "-c", _DRIVER, str(REFERENCIA / "src")], capture_output=True,
                            text=True, encoding="utf-8", check=True, cwd=REFERENCIA, env=ENTORNO_UTF8)
    return {f["id_inmueble"]: f for f in json.loads(salida.stdout)}


def diferencias(esperado, obtenido, ruta=()) -> list[str]:
    """Recorre la estructura de la referencia; las claves extra de la aplicación no cuentan."""
    if ruta in IGNORAR:
        return []
    if isinstance(esperado, dict):
        if not isinstance(obtenido, dict):
            return [f"{'.'.join(ruta)}: esperado objeto, obtenido {obtenido!r}"]
        errores = []
        for k, v in esperado.items():
            if k not in obtenido:
                errores.append(f"{'.'.join(ruta + (k,))}: falta")
            else:
                errores += diferencias(v, obtenido[k], ruta + (k,))
        return errores
    if isinstance(esperado, list):
        if not isinstance(obtenido, list) or len(esperado) != len(obtenido):
            return [f"{'.'.join(ruta)}: listas de distinto largo ({len(esperado)} vs "
                    f"{len(obtenido) if isinstance(obtenido, list) else obtenido!r})"]
        errores = []
        for k, (a, b) in enumerate(zip(esperado, obtenido, strict=True)):
            errores += diferencias(a, b, ruta + (str(k),))
        return errores
    if isinstance(esperado, float) and isinstance(obtenido, (int, float)):
        return [] if abs(esperado - obtenido) < 1e-9 else [f"{'.'.join(ruta)}: {esperado!r} vs {obtenido!r}"]
    return [] if esperado == obtenido else [f"{'.'.join(ruta)}: {esperado!r} vs {obtenido!r}"]


def comparar(carpeta: Path) -> dict[str, list[str]]:
    resultado = {}
    for id_inmueble, ref in fichas_referencia().items():
        ruta = carpeta / f"{id_inmueble}.json"
        if not ruta.exists():
            resultado[id_inmueble] = [f"falta {ruta}"]
            continue
        resultado[id_inmueble] = diferencias(ref, json.loads(ruta.read_text(encoding="utf-8")))
    return resultado


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    carpeta = Path(sys.argv[1]) if len(sys.argv) > 1 else RAIZ / "salidas" / "fichas"
    resultado = comparar(carpeta)
    for id_inmueble, errores in resultado.items():
        print(f"{'OK   ' if not errores else 'DIFF '} {id_inmueble}" + "".join(f"\n      - {e}" for e in errores))
    total = sum(len(e) for e in resultado.values())
    print(f"\n{len(resultado) - sum(1 for e in resultado.values() if e)}/{len(resultado)} fichas idénticas a la referencia "
          "en todos los campos oficiales")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
