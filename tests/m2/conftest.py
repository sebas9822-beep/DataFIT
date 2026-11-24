"""Pruebas del lote oficial del Momento 2 (pipeline sobre archivos y capas GeoJSON).

Necesitan la pila geográfica del entorno conda `venture-one` (geopandas y pandera). Sin ella se omiten:
el motor de ficha sobre el almacén no la requiere y tiene sus propias pruebas en tests/test_integracion.py.
"""
from __future__ import annotations

import importlib.util

if importlib.util.find_spec("geopandas") is None or importlib.util.find_spec("pandera") is None:
    collect_ignore_glob = ["test_*.py"]
else:
    from m2.apoyo_m2 import proyecto  # noqa: F401  (fixture)
