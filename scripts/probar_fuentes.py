"""Prueba de humo EN VIVO de las fuentes definidas en config/fuentes.yaml.

Sale a internet (servidor de mapas de la Alcaldía y Overpass). No escribe en la base.
Por cada capa informa: si responde, el sistema de coordenadas nativo que declara el servicio
(nunca se asume), cuántos registros devuelve cerca del punto de prueba y si los campos que usa
la aplicación existen todavía. Úsala cuando una capa empiece a fallar o cambie de estructura.

Uso:   python scripts/probar_fuentes.py [--lat 6.2450 --lng -75.5905 --radio 300]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from captura import config  # noqa: E402
from captura.fuentes import arcgis, base, osm  # noqa: E402


async def _capa(cli, p, nombre: str, capa: dict, punto, radio) -> dict:
    fila = {"capa": nombre, "estado": "ok", "crs": "?", "registros": 0, "faltan": [], "s": 0.0, "nota": ""}
    t0 = time.perf_counter()
    try:
        meta = (await base.pedir(cli, p, "GET", arcgis.url_capa(p, capa["servicio"], capa["capa"]).removesuffix("/query"),
                                 params={"f": "json"})).json()
        if "error" in meta:
            raise RuntimeError(str(meta["error"].get("message") or meta["error"])[:80])
        ref = (meta.get("extent") or {}).get("spatialReference") or meta.get("sourceSpatialReference") or {}
        fila["crs"] = f"EPSG:{ref.get('latestWkid') or ref.get('wkid') or '?'}"
        existentes = {c["name"].lower() for c in meta.get("fields") or []}
        usados = [c for clave in ("nombre", "subtipo", "atributos") for c in capa.get(clave) or []]
        pedidos = capa.get("campos") or []
        if isinstance(pedidos, str):
            pedidos = pedidos.split(",")
        usados += [c.strip() for c in pedidos if c.strip() and c.strip() != "*"]
        if capa.get("fecha"):
            usados.append(capa["fecha"])
        fila["faltan"] = sorted({c for c in usados if c.lower() not in existentes})
        feats, _, _ = await arcgis.consultar(cli, p, capa["servicio"], capa["capa"], punto=punto,
                                             distancia_m=radio, donde=capa.get("donde") or "1=1",
                                             campos="objectid", geometria=False, max_registros=50)
        fila["registros"] = len(feats)
        if fila["faltan"]:
            fila["estado"] = "fallo_estructura"
    except base.FuenteBloqueada as e:
        fila.update(estado="bloqueado", nota=str(e)[:70])
    except Exception as e:
        fila.update(estado="error", nota=f"{type(e).__name__}: {str(e)[:60]}")
    fila["s"] = round(time.perf_counter() - t0, 1)
    return fila


async def correr(lat: float, lng: float, radio: int) -> int:
    p = config.cargar()
    filas = []
    async with base.nuevo_cliente(p) as cli:
        sem = asyncio.Semaphore(p["params"]["arcgis"]["concurrencia"])

        async def con_turno(nombre, capa, r):
            async with sem:
                return await _capa(cli, p, nombre, capa, (lat, lng), r)

        tareas = [con_turno(f"predio · {k}", v, radio if k != "estaciones" else 1500)
                  for k, v in p["fuentes"]["capas_predio"].items()]
        tareas += [con_turno(f"entorno · {c['id']}", c, radio) for c in p["fuentes"]["capas_entorno"]
                   if c.get("activa", True)]
        filas = list(await asyncio.gather(*tareas))
        t0 = time.perf_counter()
        try:
            items, _, ej = await osm.barrer(cli, p, lat, lng, radio)
            filas.append({"capa": "osm · overpass", "estado": ej.estado, "crs": "EPSG:4326", "registros": len(items),
                          "faltan": [], "s": round(time.perf_counter() - t0, 1), "nota": ej.mensaje or ""})
        except Exception as e:
            filas.append({"capa": "osm · overpass", "estado": "error", "crs": "-", "registros": 0, "faltan": [],
                          "s": round(time.perf_counter() - t0, 1), "nota": f"{type(e).__name__}: {str(e)[:60]}"})

    print(f"\nPunto de prueba: {lat}, {lng} · radio {radio} m · {time.strftime('%Y-%m-%d %H:%M')}\n")
    print(f"{'capa':<34}{'estado':<18}{'CRS nativo':<12}{'reg.':>5}{'  s':>6}  nota")
    print("-" * 100)
    for f in filas:
        nota = f["nota"] or (("faltan campos: " + ", ".join(f["faltan"])) if f["faltan"] else "")
        print(f"{f['capa']:<34}{f['estado']:<18}{f['crs']:<12}{f['registros']:>5}{f['s']:>6}  {nota}")
    malas = [f for f in filas if f["estado"] != "ok"]
    print("-" * 100)
    print(f"{len(filas) - len(malas)} de {len(filas)} capas en orden." + ("" if not malas else
          " Revisar: " + ", ".join(f["capa"] for f in malas)))
    return 1 if malas else 0


if __name__ == "__main__":
    a = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    a.add_argument("--lat", type=float, default=6.2450)
    a.add_argument("--lng", type=float, default=-75.5905)
    a.add_argument("--radio", type=int, default=300)
    args = a.parse_args()
    sys.exit(asyncio.run(correr(args.lat, args.lng, args.radio)))
