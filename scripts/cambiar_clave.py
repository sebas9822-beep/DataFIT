"""Cambia la clave de un comercial.

Uso:   iniciar.bat clave comercial1
       python scripts/cambiar_clave.py comercial1

La clave no se muestra al escribirla. Al cambiarla se cierran las sesiones abiertas de ese usuario
y queda registrado en la tabla de accesos.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from captura import acceso, bd, config  # noqa: E402


def main() -> int:
    p = config.cargar()
    bd.iniciar(p)
    usuario = sys.argv[1] if len(sys.argv) > 1 else input("Usuario: ")
    nueva = getpass.getpass(f"Clave nueva para {acceso.normalizar_usuario(usuario)} "
                            f"(mínimo {p['reglas']['acceso']['clave_minima']} caracteres): ")
    if nueva != getpass.getpass("Repítela: "):
        print("No coinciden. No se cambió nada.")
        return 1
    try:
        acceso.cambiar_clave(p, usuario, nueva)
    except ValueError as e:
        print(f"{e}. No se cambió nada.")
        return 1
    print("Clave cambiada. Las sesiones abiertas de ese usuario se cerraron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
