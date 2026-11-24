"""Hoja para el cliente: la ficha en una página, lista para imprimir o guardar como PDF.

Qué muestra: el inmueble, el precio de referencia del mercado, qué tan firme es esa referencia, lo que hay
cerca y, en una frase, cómo se obtuvo. Nada del procesamiento (corridas, huellas, duplicados, bases).

Qué no hace: no calcula nada nuevo sobre el mercado. Toma las cifras de la ficha ya generada (que sale del
almacén) y las dice en lenguaje llano. No es un avalúo ni aconseja comprar o invertir: la nota del pie lo dice.

La plantilla (HTML y estilos) está en hoja_cliente.html; los textos y redondeos, en reglas.yaml › hoja_cliente.
"""
from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

import numpy as np

from almacen import normalizacion as nz

PLANTILLA = Path(__file__).with_name("hoja_cliente.html")
SISTEMAS = {"metro": "del metro", "tranvia": "del tranvía", "metrocable": "del metrocable", "metroplus": "de metroplús"}
GUION_IMPRIMIR = "<script>window.addEventListener('load', function () { setTimeout(function () { window.print(); }, 600); });</script>"


def _e(texto) -> str:
    return html.escape(str(texto if texto is not None else ""), quote=True)


def _miles(valor: float) -> str:
    return f"{int(round(valor)):,}".replace(",", ".")


def _redondear(valor: float, paso: float) -> int:
    return int(round(valor / paso) * paso)


def _pesos(valor: float, paso: float) -> str:
    return "$" + _miles(_redondear(valor, paso))


def _millones(valor: float, paso: float) -> str:
    """Cifra del inmueble completo, dicha en millones (el paso de redondeo define cuántos decimales tiene sentido)."""
    return _miles(_redondear(valor, paso) / 1_000_000)


def _numero(valor) -> str:
    return f"{valor:g}".replace(".", ",")


def _lista(partes: list[str]) -> str:
    partes = [x for x in partes if x]
    if len(partes) <= 1:
        return "".join(partes)
    return ", ".join(partes[:-1]) + " y " + partes[-1]


def _contar(n, formas: list[str]) -> str:
    return f"{_miles(n)} {formas[0] if n == 1 else formas[1]}"


def resumen(ficha: dict, p: dict) -> dict:
    """Las cifras que lleva la hoja, ya redondeadas. Sirve a la plantilla y a las pruebas."""
    cfg = p["reglas"]["hoja_cliente"]
    m, inm = ficha["mercado"], ficha["inmueble"]
    stats = m["precio_m2"]
    n, mediana, area = int(stats.get("n") or 0), stats.get("mediana"), inm.get("area_m2")
    paso_m2, paso_total = cfg["redondeo_m2_cop"], cfg["redondeo_total_cop"]
    if not n or not mediana:
        return {"hay_referencia": False, "n": 0, "confianza": None}
    valores = sorted(float(c["precio_m2"]) for c in m.get("comparables", []) if c.get("en_referencia", True) and c.get("precio_m2"))
    con_rango = len(valores) >= cfg["minimo_para_rango"]
    if con_rango:
        bajo, alto = (float(x) for x in np.percentile(valores, cfg["rango_percentiles"]))
    else:
        bajo, alto = float(stats["minimo"]), float(stats["maximo"])
    minimo, maximo = float(stats["minimo"]), float(stats["maximo"])
    ancho = (maximo - minimo) or 1.0
    salida = {"hay_referencia": True, "n": n, "confianza": (m.get("confianza") or {}).get("nivel"),
              "m2": _redondear(mediana, paso_m2), "m2_bajo": _redondear(bajo, paso_m2), "m2_alto": _redondear(alto, paso_m2),
              "m2_minimo": _redondear(minimo, paso_m2), "m2_maximo": _redondear(maximo, paso_m2), "rango_es_tipico": con_rango,
              "barra": {"desde": round((bajo - minimo) / ancho * 100, 1), "ancho": round((alto - bajo) / ancho * 100, 1),
                        "centro": round((float(mediana) - minimo) / ancho * 100, 1)},
              "area_m2": area, "total": None, "total_bajo": None, "total_alto": None}
    if area:
        salida.update(total=_redondear(mediana * area, paso_total), total_bajo=_redondear(bajo * area, paso_total),
                      total_alto=_redondear(alto * area, paso_total))
    return salida


def _bloque_resultado(ficha: dict, p: dict, r: dict) -> str:
    cfg = p["reglas"]["hoja_cliente"]
    if not r["hay_referencia"]:
        return ('  <section class="resultado sin-referencia">\n    <h2>Precio de referencia en el mercado</h2>\n'
                "    <p>Hoy no hay suficientes anuncios vigentes de inmuebles parecidos para dar un precio de referencia. "
                "Su asesor puede repetir la búsqueda más adelante.</p>\n  </section>")
    paso_total = cfg["redondeo_total_cop"]
    L = ['  <section class="resultado">', "    <h2>Precio de referencia en el mercado</h2>",
         f'    <p class="cifra">${_miles(r["m2"])} <span>por m²</span></p>']
    if r["total"]:
        L.append(f'    <p class="total">Para los {_numero(r["area_m2"])} m² de este inmueble, eso equivale a cerca de '
                 f'<b>${_millones(r["total"], paso_total)} millones</b>.</p>')
    b = r["barra"]
    L += [f'    <div class="rango" role="img" aria-label="Precios por metro cuadrado de los anuncios parecidos: del más bajo al más alto">',
          f'      <div class="pista"><i class="banda" style="left:{b["desde"]}%;width:{b["ancho"]}%"></i>'
          f'<i class="centro" style="left:{b["centro"]}%"></i></div>',
          f'      <div class="extremos"><span><b>${_miles(r["m2_minimo"])}</b>el anuncio más bajo, por m²</span>'
          f'<span><b>${_miles(r["m2_maximo"])}</b>el más alto</span></div>', "    </div>"]
    if r["rango_es_tipico"]:
        frase = f'La mitad de los anuncios parecidos pide entre ${_miles(r["m2_bajo"])} y ${_miles(r["m2_alto"])} por m²'
        if r["total"]:
            frase += (f', es decir, entre ${_millones(r["total_bajo"], paso_total)} y ${_millones(r["total_alto"], paso_total)} '
                      "millones por un inmueble de este tamaño")
        L.append(f'    <p class="lectura">{frase}. La franja verde lo muestra y la línea marca el valor del medio.</p>')
    else:
        L.append(f'    <p class="lectura">Los {_contar(r["n"], ["anuncio parecido", "anuncios parecidos"])} van de '
                 f'${_miles(r["m2_minimo"])} a ${_miles(r["m2_maximo"])} por m². La línea marca el valor del medio.</p>')
    firmeza = (cfg["firmeza"] or {}).get(r["confianza"] or "")
    if firmeza:
        L.append(f'    <p class="firmeza"><b>{_e(firmeza["titulo"])}.</b> {_e(firmeza["texto"])}</p>')
    L.append("  </section>")
    return "\n".join(L)


def _bloque_entorno(ficha: dict, p: dict) -> str:
    cfg = p["reglas"]["hoja_cliente"]
    e = ficha.get("entorno") or {}
    L = ["    <section>", "      <h2>Lo que hay cerca</h2>"]
    est = e.get("estacion_mas_cercana")
    if est and est.get("nombre"):
        sistema = SISTEMAS.get(nz.sin_tildes(str(est.get("sistema") or "")).lower(), "de transporte masivo")
        L.append(f'      <p class="cerca">La estación {_e(est["nombre"])} {sistema} queda a {_miles(est["distancia_m"])} m.</p>')
    conteos = e.get("equipamientos_en_radio") or {}
    filas = [(cfg["etiquetas_entorno"].get(c, [c, c]), conteos.get(c)) for c in cfg["categorias_entorno"]]
    filas = [(formas, n) for formas, n in filas if n]
    if filas:
        L.append(f'      <p>A {_miles(e.get("radio_busqueda_m") or 0)} m a la redonda:</p>')
        L.append("      <ul>" + "".join(f"<li>{_e(formas[0] if n == 1 else formas[1]).capitalize()}<b>{_miles(n)}</b></li>" for formas, n in filas) + "</ul>")
    if len(L) == 2:
        L.append("      <p>No hay datos del entorno para este punto.</p>")
    L.append("    </section>")
    return "\n".join(L)


def _como(ficha: dict, r: dict, fecha: str) -> str:
    m, inm = ficha["mercado"], ficha["inmueble"]
    if not r["hay_referencia"]:
        return f"Se buscaron anuncios vigentes de venta del mismo tipo de inmueble en la zona el {fecha}."
    crit = m.get("criterio_comparables") or {}
    rango = crit.get("rango_area_m2") or []
    zona = (f"en {inm['barrio']} y barrios vecinos" if m.get("ubicacion_comparables") and inm.get("barrio")
            else f"a {_miles(m['radio_m'])} m o menos del inmueble" if m.get("radio_m") else "en la misma zona")
    area = f", con área entre {_numero(rango[0])} y {_numero(rango[1])} m²" if len(rango) == 2 else ""
    return (f"Se compararon {_contar(r['n'], ['anuncio vigente', 'anuncios vigentes'])} de venta del mismo tipo de inmueble, "
            f"{zona}{area}, consultados el {fecha}.")


def construir(ficha: dict, p: dict, imprimir: bool = False) -> str:
    """El HTML completo de la hoja, a partir del JSON de una ficha ya generada."""
    inm, q = ficha["inmueble"], ficha.get("cotizacion") or {}
    r = resumen(ficha, p)
    try:
        dia = date.fromisoformat(str((ficha.get("metadatos") or {}).get("fecha_generacion"))[:10])
    except ValueError:
        dia = date.today()
    fecha = f"{dia.day} de {nz.MESES[dia.month]} de {dia.year}"
    tipo = str(inm.get("tipo") or "inmueble").replace("_", " ").capitalize()
    titulo = f"{tipo} en {inm['barrio']}" if inm.get("barrio") else tipo
    direccion = inm.get("direccion") if inm.get("direccion") and "marcado en el mapa" not in str(inm.get("direccion")).lower() else None
    lugar = ", ".join(x for x in (inm.get("barrio"), inm.get("municipio")) if x)
    donde = ". ".join(x for x in (", ".join(y for y in (direccion, inm.get("conjunto")) if y), lugar) if x)
    rasgos = _lista([f"{_numero(inm['area_m2'])} m²" if inm.get("area_m2") else "",
                     _contar(int(inm["habitaciones"]), ["habitación", "habitaciones"]) if inm.get("habitaciones") else "",
                     _contar(int(inm["banos"]), ["baño", "baños"]) if inm.get("banos") else "",
                     _contar(int(inm["parqueaderos"]), ["parqueadero", "parqueaderos"]) if inm.get("parqueaderos") else ""])
    cliente = (q.get("cliente") or {}).get("nombre")
    asesor = (q.get("comercial") or {}).get("nombre")
    valores = {
        "titulo_pagina": _e(f"Ficha del inmueble · {titulo}"), "fecha": _e(fecha),
        "para": _e(f"Ficha del inmueble preparada para {cliente}" if cliente else "Ficha del inmueble"),
        "titulo": _e(titulo), "donde": _e(donde), "rasgos": _e(rasgos),
        "resultado": _bloque_resultado(ficha, p, r), "entorno": _bloque_entorno(ficha, p), "como": _e(_como(ficha, r, fecha)),
        "nota": _e("Los precios son los que piden los vendedores en sus anuncios; el valor final de una negociación puede ser distinto. "
                   "Esta hoja describe el mercado y el entorno: no es un avalúo ni una recomendación de compra o de inversión, "
                   "y no reemplaza el estudio de títulos ni el concepto de norma urbanística."),
        "asesor": _e(f"Le atendió {asesor}, Venture One" if asesor else "Venture One"),
        "referencia": _e(f"Ficha {str(q.get('cotizacion_id') or '')[:8].upper()}, versión {q.get('version') or 1}"),
        "guion": GUION_IMPRIMIR if imprimir else ""}
    pagina = PLANTILLA.read_text(encoding="utf-8")
    for clave, valor in valores.items():
        pagina = pagina.replace("{{" + clave + "}}", valor)
    return pagina


def de_cotizacion(p: dict, registro: dict, imprimir: bool = False) -> str | None:
    """La hoja de la ficha vigente (`registro` es lo que devuelve ficha.servicio.leer). None si falta el archivo."""
    ruta = Path(p["rutas"]["fichas"]) / str(registro.get("ruta_json") or "")
    if not registro.get("ruta_json") or not ruta.exists():
        return None
    return construir(json.loads(ruta.read_text(encoding="utf-8")), p, imprimir)
