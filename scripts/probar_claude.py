"""Prueba EN VIVO de la conexión con Claude: envía un lote mínimo y muestra lo que devuelve.

Hace una llamada real (consume una fracción del plan Claude o de la llave de API).
No lee ni escribe la base. Sirve para confirmar que Claude Code responde en este equipo.

Uso:   python scripts/probar_claude.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from captura import config  # noqa: E402
from captura.curaduria import claude_cliente, claude_tareas  # noqa: E402

LOTE = {
    "contexto": {"municipio": "Medellín", "barrio": "Bolivariana", "radio_m": 1000},
    "items": [
        {"id": "demo:1", "fuente": "geomedellin", "nombre": "i.e. inem jose felix de restrepo",
         "categoria": "educacion", "subtipo": "colegio", "pista": "Educación Básica y Media"},
        {"id": "demo:2", "fuente": "catastro", "nombre": "CLINICA MEDELLIN SEDE OCCIDENTE",
         "categoria": "otros_equipamientos", "subtipo": "toponimia", "pista": "03"},
        {"id": "demo:3", "fuente": "osm", "nombre": "", "categoria": "parques", "subtipo": "park", "pista": "leisure=park"},
    ],
    "duplicados": [{"par": "demo:4|demo:5", "distancia_m": 38.0,
                    "a": {"fuente": "geomedellin", "nombre": "Colegio San José", "categoria": "educacion", "subtipo": "colegio", "pista": ""},
                    "b": {"fuente": "osm", "nombre": "Colegio San José de La Salle", "categoria": "educacion", "subtipo": "school", "pista": "amenity=school"}}],
    "conjunto": {"candidatos": [
        {"id": "catastro:toponimia:9", "nombre": "EDIFICIO LOS ALMENDROS", "tipo": "Edificios", "origen": "catastro",
         "distancia_m": 12.0, "mismo_lote": True, "contiene_punto": False}]},
}


def main() -> int:
    p = config.cargar()
    det = claude_cliente.detectar(p)
    print(f"Proveedor: {det['proveedor']} · disponible: {det['disponible']} · {det['detalle']}")
    if not det["disponible"]:
        return 1
    esquema = claude_tareas.esquema(list(p["taxonomia"]["categorias"]))
    t0 = time.perf_counter()
    try:
        r = claude_cliente.invocar(p, claude_tareas.SISTEMA, json.dumps(LOTE, ensure_ascii=False), esquema)
    except claude_cliente.ClaudeNoDisponible as e:
        print(f"FALLO: {e}")
        return 1
    print(f"Respondió en {time.perf_counter() - t0:.1f} s · modelo {r['modelo']} · costo informado: {r.get('costo_usd')}")
    datos = r["datos"]
    print(json.dumps(datos, ensure_ascii=False, indent=2))
    faltan = [k for k in esquema["required"] if k not in (datos or {})]
    ids = {x.get("id") for x in (datos or {}).get("items", [])}
    ok = not faltan and ids == {"demo:1", "demo:2", "demo:3"}
    print("\nEstructura correcta." if ok else f"\nEstructura incompleta (faltan {faltan}; ids {sorted(ids)}).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
