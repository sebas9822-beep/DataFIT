"""Etapa 6 · Regla de confianza (parametros.yaml: confianza).

Se evalúa de arriba hacia abajo: alta, luego media; si ninguna se cumple, baja.
CV = desviación estándar muestral / promedio del precio/m².

Mide únicamente la calidad y suficiencia de los datos que soportan el precio/m². No mide
rentabilidad ni conveniencia de la compra.
"""
from __future__ import annotations

ALCANCE = "Mide calidad y suficiencia de los datos. No mide rentabilidad ni conveniencia de compra."


def calificar(n: int, coef_variacion: float | None, reglas_confianza: dict) -> dict:
    """{'nivel', 'motivo', 'alcance'} con el texto de motivo del formato oficial."""
    for nivel in ("alta", "media"):
        r = reglas_confianza[nivel]
        if n >= r["min_comparables"] and coef_variacion is not None and coef_variacion <= r["max_coef_variacion"]:
            motivo = (f"{n} comparables (mínimo {r['min_comparables']}) y coeficiente de variación "
                      f"{coef_variacion:.2%} (máximo {r['max_coef_variacion']:.0%})")
            return {"nivel": nivel, "motivo": motivo, "alcance": ALCANCE}
    cv = "sin dato" if coef_variacion is None else f"{coef_variacion:.2%}"
    m = reglas_confianza["media"]
    motivo = (f"{n} comparables y coeficiente de variación {cv}: no cumple el nivel media "
              f"(mínimo {m['min_comparables']} comparables, máximo {m['max_coef_variacion']:.0%})")
    return {"nivel": "baja", "motivo": motivo, "alcance": ALCANCE}
