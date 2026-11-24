"""Borra de la base lo que venció por la regla de 30 días de Google (coordenadas y derivados).

La aplicación ya lo hace sola al arrancar; este script sirve para programarlo o correrlo a mano.

Uso:   python scripts/purgar.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from captura import bd, config  # noqa: E402

if __name__ == "__main__":
    p = config.cargar()
    bd.iniciar(p)
    print(bd.purgar_expirados(p))
