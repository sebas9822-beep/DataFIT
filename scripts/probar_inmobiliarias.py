"""Prueba de lectura de las inmobiliarias con el motor real de la aplicación (Claude Code).

Lee cada sitio permitido del catálogo, también los apagados por defecto, con una consulta de prueba, y deja el
informe en salidas/prueba_inmobiliarias_<fecha>.md. No toca la base de datos.

    python scripts/probar_inmobiliarias.py                         apartamento en venta en Laureles, 80 m²
    python scripts/probar_inmobiliarias.py --barrio "El Poblado" --tipo casa --area 150
    python scripts/probar_inmobiliarias.py --solo fincaraiz,acrecer
    python scripts/probar_inmobiliarias.py --guardar               anota `lectura` de cada sitio en config/inmobiliarias.yaml
    python scripts/probar_inmobiliarias.py --guardar --apagar      además deja apagados por defecto los que no se dejaron leer

Consume la sesión del plan Claude: una lectura por sitio.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from almacen import etl                      # noqa: E402
from captura import config                   # noqa: E402
from captura.fuentes import portales         # noqa: E402

LEGIBLE = ("ok", "sin_resultados")


def anotar(ruta: Path, por_portal: list[dict], apagar: bool, fecha: str) -> int:
    """Actualiza `lectura` (y, si se pide, `por_defecto`) de cada sitio probado, sin tocar el resto del archivo."""
    lineas = ruta.read_text(encoding="utf-8").split("\n")
    estados = {x["portal"]: x["estado"] for x in por_portal}
    actual, cambios = None, 0
    for i, linea in enumerate(lineas):
        m = re.match(r"  - id: (\S+)", linea)
        if m:
            actual = m.group(1)
            continue
        if actual not in estados:
            continue
        if linea.startswith("    lectura:"):
            lineas[i] = f"    lectura: {estados[actual]}        # probado el {fecha}"
            cambios += 1
        elif apagar and linea.startswith("    por_defecto: true") and estados[actual] not in LEGIBLE:
            lineas[i] = "    por_defecto: false       # no se dejó leer en la prueba; se puede encender a mano"
    ruta.write_text("\n".join(lineas), encoding="utf-8", newline="\n")
    return cambios


def main() -> None:
    a = argparse.ArgumentParser(description="Prueba de lectura de las inmobiliarias")
    a.add_argument("--barrio", default="Laureles")
    a.add_argument("--tipo", default="apartamento")
    a.add_argument("--area", type=float, default=80.0)
    a.add_argument("--solo", default="")
    a.add_argument("--guardar", action="store_true")
    a.add_argument("--apagar", action="store_true")
    args = a.parse_args()
    p = config.cargar()
    if not p["params"]["rpa"]["activo"]:
        sys.exit("El RPA está desactivado en params.yaml › rpa")
    sitios = [x for x in portales.catalogo(p) if x.get("permitida", True)]
    if args.solo:
        pedidos = {x.strip() for x in args.solo.split(",")}
        sitios = [x for x in sitios if x["id"] in pedidos]
    tolerancia = etl.params_ficha(p)["reglas"]["comparables"]["tolerancia_area"]
    hoy = datetime.now()
    contexto = {"fecha": hoy.date().isoformat(), "operacion": p["almacen"]["bases_mercado"][portales.FUENTE]["operacion_comparable"],
                "municipio": p["almacen"]["municipio_por_defecto"], "barrio": args.barrio, "comuna": None, "tipos": [args.tipo],
                "tipo_cotizado": args.tipo,
                "rango_area": [round(args.area * (1 - tolerancia), 1), round(args.area * (1 + tolerancia), 1)]}
    print(f"Probando {len(sitios)} sitios: {args.tipo} en venta en {args.barrio}, {contexto['rango_area'][0]:g} a {contexto['rango_area'][1]:g} m²")
    r = portales.buscar(p, contexto, sitios, avance=lambda h, t: print(f"  grupo {h} de {t} terminado", flush=True))
    por_estado: dict[str, list] = {}
    for x in r["por_portal"]:
        por_estado.setdefault(x["estado"], []).append(x)
    L = [f"# Prueba de lectura de inmobiliarias · {hoy:%Y-%m-%d %H:%M}", "",
         f"Consulta: {args.tipo} en venta en {args.barrio} (Medellín), área {contexto['rango_area'][0]:g} a {contexto['rango_area'][1]:g} m².",
         f"Motor: Claude Code ({r.get('modelo') or p['params']['rpa']['modelo_cli']}) con lectura web limitada al dominio de cada sitio.",
         f"Resultado: {r['mensaje']} · {r['duracion_s']} s.", "",
         "| Estado | Sitios |", "|---|---:|"] + [f"| {k} | {len(v)} |" for k, v in sorted(por_estado.items())] + [
         "", "| Sitio | Dominio | Estado | Anuncios útiles | Descartados | Detalle |", "|---|---|---|---:|---:|---|"]
    for x in sorted(r["por_portal"], key=lambda x: (x["estado"] not in LEGIBLE, -x["utiles"], x["nombre"])):
        L.append(f"| {x['nombre']} | {x['dominio']} | {x['estado']} | {x['utiles']} | {x['descartados']} | {(x['detalle'] or '').replace('|', '/')} |")
    utiles = [a for a in r["anuncios"] if a["motivo_exclusion"] is None]
    L += ["", f"## Anuncios útiles ({len(utiles)})", "", "| Sitio | Tipo | Área m² | Precio COP | Barrio | Fecha de publicación | Enlace |", "|---|---|---:|---:|---|---|---|"]
    for x in utiles:
        L.append(f"| {x['portal_nombre']} | {x['tipo_inmueble']} | {x['area_m2']:g} | {x['precio']:,.0f} | {x['barrio'] or ''} | "
                 f"{x['fecha_publicacion'] or 'no la muestra'} | {x['url']} |")
    salida = Path(p["rutas"]["exportaciones"]) / f"prueba_inmobiliarias_{hoy:%Y%m%d_%H%M}.md"
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(r["mensaje"], "·", r["duracion_s"], "s")
    print({k: len(v) for k, v in sorted(por_estado.items())})
    print("Informe:", salida)
    if args.guardar:
        n = anotar(RAIZ / "config" / "inmobiliarias.yaml", r["por_portal"], args.apagar, hoy.date().isoformat())
        print(f"config/inmobiliarias.yaml: {n} sitios anotados" + (" · apagados por defecto los que no se dejaron leer" if args.apagar else ""))


if __name__ == "__main__":
    main()
