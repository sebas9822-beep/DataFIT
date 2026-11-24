"""Vuelve a buscar anuncios vigentes en las inmobiliarias y rehace la ficha de cotizaciones ya hechas.

Sirve cuando cambia la regla de vigencia o la lista de inmobiliarias: las fichas anteriores se recalculan con los
anuncios de hoy. La ficha anterior no se borra: queda como versión previa.

    python -m captura.rebuscar                    todas las cotizaciones que cuentan
    python -m captura.rebuscar --cotizacion ID    una sola (basta el comienzo del identificador)
"""
from __future__ import annotations

import argparse

from ficha import servicio as fichas

from . import api, bd, config


def rebuscar(p: dict, cotizacion: str | None = None) -> list[dict]:
    """Por cada cotización: búsqueda en inmobiliarias, depuración de anuncios y ficha nueva. Un renglón por cotización."""
    cuentan = p["reglas"]["cotizaciones"]["estados_que_cuentan"]
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute(
            f"SELECT cotizacion_id, comercial, inm_tipo, captura_id FROM cotizaciones WHERE estado IN ({', '.join('?' for _ in cuentan)}) "
            "AND captura_id IS NOT NULL ORDER BY creada_en", cuentan).fetchall()
    salida = []
    for q in filas:
        if cotizacion and not q["cotizacion_id"].startswith(cotizacion):
            continue
        renglon = {"cotizacion_id": q["cotizacion_id"], "comercial": q["comercial"], "tipo": q["inm_tipo"]}
        if not q["inm_tipo"]:
            salida.append(dict(renglon, estado="pendiente", mensaje="faltan los datos del inmueble: complétalos en la pantalla de la cotización"))
            continue
        rpa = api.mercado_de_la_cotizacion(p, q["cotizacion_id"])
        ficha = fichas.generar_para_cotizacion(p, q["cotizacion_id"])
        salida.append(dict(renglon, estado=ficha.get("estado") or "sin_ficha", version=ficha.get("version"),
                           comparables=(ficha.get("resumen") or {}).get("n"), confianza=(ficha.get("resumen") or {}).get("confianza"),
                           rpa=rpa.get("estado"), mensaje=rpa.get("mensaje"), sitios=rpa.get("sitios_por_estado"),
                           segundos=rpa.get("duracion_s")))
    return salida


def main() -> None:
    analizador = argparse.ArgumentParser(description="Vuelve a buscar en inmobiliarias y rehace las fichas")
    analizador.add_argument("--cotizacion", default=None)
    args = analizador.parse_args()
    p = config.cargar()
    bd.iniciar(p)                                  # deja la base al día antes de buscar
    for r in rebuscar(p, args.cotizacion):
        print(f"{r['cotizacion_id'][:8]} · {r['comercial']} · {r['tipo'] or 'sin tipo'} · ficha {r['estado']}"
              + (f" v{r['version']} · {r['comparables']} comparables · confianza {r['confianza']}" if r.get("version") else "")
              + f"\n    {r['mensaje']}" + (f" · {r['segundos']} s · sitios: {r['sitios']}" if r.get("sitios") else ""))


if __name__ == "__main__":
    main()
