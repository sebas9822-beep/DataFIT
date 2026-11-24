"""Descarga la capa de tratamientos del POT de Medellín (Acuerdo 48 de 2014) en GeoJSON WGS84.

Fuente: servidor de mapas de la Alcaldía (ArcGIS REST), capa "Tratamiento POT vigente", la misma que
indica el README del reto. Solo biblioteca estándar más los parámetros de config/params.yaml
(bloque `pot_descarga`). Pide los objetos por lotes y reintenta con espera creciente si el servidor
responde 5xx (el 15-nov-2026 respondía 503 de forma sostenida).

Uso:  python scripts/descargar_pot.py              # escribe rutas.pot_geojson (data/geo/pot_tratamientos.geojson)
      python scripts/descargar_pot.py --probar      # solo consulta la capa y cuenta los objetos

El archivo lleva un bloque `metadata` con el servicio, la fecha de descarga y la atribución.
No reemplaza el archivo existente si la descarga queda incompleta.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from ficha.config import cargar_params  # noqa: E402

AGENTE = "ficha-venture-one/0.3 (reto EstudIA 13; descarga de datos abiertos)"


class ErrorServicio(RuntimeError):
    """Respuesta definitiva del servicio (no se reintenta)."""


def _pedir(url: str, datos: dict | None, cfg: dict, abrir=urllib.request.urlopen) -> dict:
    """GET (o POST si hay datos) con reintentos. Devuelve el JSON. `abrir` se reemplaza en las pruebas."""
    cuerpo = urllib.parse.urlencode(datos).encode() if datos else None
    espera = cfg["espera_s"]
    for intento in range(1, cfg["intentos"] + 1):
        try:
            req = urllib.request.Request(url, data=cuerpo, headers={"User-Agent": AGENTE})
            with abrir(req, timeout=cfg["tiempo_maximo_s"]) as resp:
                contenido = json.loads(resp.read().decode("utf-8"))
            if isinstance(contenido, dict) and "error" in contenido:     # ArcGIS responde errores con código 200
                codigo = contenido["error"].get("code", 0)
                if codigo and int(codigo) < 500:
                    raise ErrorServicio(f"{url}: {contenido['error']}")
                raise urllib.error.URLError(f"error del servicio {contenido['error']}")
            return contenido
        except urllib.error.HTTPError as err:
            if err.code < 500:
                raise ErrorServicio(f"{url}: HTTP {err.code}") from err
            motivo = f"HTTP {err.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as err:
            motivo = str(getattr(err, "reason", err))
        if intento == cfg["intentos"]:
            raise RuntimeError(f"El servicio no respondió tras {intento} intentos ({motivo}): {url}")
        print(f"  intento {intento}/{cfg['intentos']} falló ({motivo}); nuevo intento en {espera} s", flush=True)
        time.sleep(espera)
        espera *= 2
    raise AssertionError("inalcanzable")


def descargar(cfg: dict, abrir=urllib.request.urlopen, solo_probar: bool = False) -> dict:
    servicio = cfg["servicio"].rstrip("/")
    info = _pedir(f"{servicio}?f=json", None, cfg, abrir)
    if info.get("geometryType") != "esriGeometryPolygon":
        raise ErrorServicio(f"La capa no es de polígonos: {info.get('geometryType')}")
    campos_capa = {f["name"].lower() for f in info.get("fields", [])}
    faltan = [c for c in cfg["campos"] if c.lower() not in campos_capa]
    if faltan:
        raise ErrorServicio(f"La capa no tiene los campos {faltan}. Campos: {sorted(campos_capa)}")
    ids = _pedir(f"{servicio}/query", {"where": cfg["donde"], "returnIdsOnly": "true", "f": "json"}, cfg, abrir)
    object_ids = sorted(ids.get("objectIds") or [])
    sr = (info.get("extent") or {}).get("spatialReference", {})
    print(f"Capa '{info.get('name')}': {len(object_ids)} objetos; CRS nativo EPSG:{sr.get('latestWkid') or sr.get('wkid')}")
    if solo_probar:
        return {"type": "FeatureCollection", "features": [], "metadata": {"registros_servicio": len(object_ids)}}
    if not object_ids:
        raise ErrorServicio("El servicio no devolvió objetos")
    features: list[dict] = []
    for i in range(0, len(object_ids), cfg["lote"]):
        lote = object_ids[i:i + cfg["lote"]]
        pagina = _pedir(f"{servicio}/query", {
            "objectIds": ",".join(map(str, lote)), "outFields": ",".join(cfg["campos"]), "returnGeometry": "true",
            "outSR": "4326", "geometryPrecision": str(cfg["precision_geometria"]), "f": "geojson"}, cfg, abrir)
        features += pagina.get("features", [])
        print(f"  {min(i + cfg['lote'], len(object_ids))}/{len(object_ids)} objetos", flush=True)
    recibidos = {f.get("id", (f.get("properties") or {}).get("objectid")) for f in features}
    if len(features) != len(object_ids) or len(recibidos) != len(object_ids):
        raise RuntimeError(f"Descarga incompleta: {len(features)} objetos recibidos de {len(object_ids)}")
    return {
        "type": "FeatureCollection",
        "metadata": {
            "fuente": "Servidor de mapas de la Alcaldía de Medellín (ArcGIS REST)",
            "servicio": servicio, "capa": info.get("name") or cfg["nombre_capa"],
            "fecha_descarga": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "registros": len(features), "crs": "EPSG:4326 (outSR=4326)", "crs_nativo_servicio": "EPSG:9377",
            "campos": cfg["campos"], "atribucion": cfg["atribucion"],
            "uso": "Informativo. No reemplaza el mapa protocolizado del Acuerdo 48 de 2014 ni constituye concepto jurídico.",
        },
        "features": sorted(features, key=lambda f: f.get("id", 0)),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Descarga la capa de tratamientos del POT de Medellín.")
    ap.add_argument("--probar", action="store_true", help="solo consultar la capa y contar los objetos")
    ap.add_argument("--intentos", type=int, default=None)
    ap.add_argument("--espera", type=int, default=None, help="segundos de espera inicial entre intentos")
    args = ap.parse_args()
    p = cargar_params()
    cfg = dict(p["pot_descarga"])
    if args.intentos:
        cfg["intentos"] = args.intentos
    if args.espera:
        cfg["espera_s"] = args.espera
    try:
        capa = descargar(cfg, solo_probar=args.probar)
    except (ErrorServicio, RuntimeError) as err:
        print(f"No se descargó la capa: {err}")
        return 1
    if args.probar:
        print("Servicio disponible.")
        return 0
    destino = p["rutas"]["pot_geojson"]
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(".descargando")
    temporal.write_text(json.dumps(capa, ensure_ascii=False), encoding="utf-8")
    temporal.replace(destino)
    print(f"Capa guardada en {destino.relative_to(RAIZ)}: {capa['metadata']['registros']} polígonos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
