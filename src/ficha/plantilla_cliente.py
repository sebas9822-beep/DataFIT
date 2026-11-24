"""Estructura de la ficha del inmueble en Markdown (la que recibe el cliente).

Se guarda en `fichas_clientes/<NIT o cédula>/FICHA_<fecha>_<cotización>_v<versión>.md`, junto a su JSON.
Solo presenta lo que ya está en el JSON de la ficha: no calcula nada. La definición de cada sección está
en `docs/plantilla_ficha_inmueble.md`.

    encabezado (YAML)   identificación legible por máquina: ficha, cotización, comercial, cliente
    1. Inmueble         lo que declaró el comercial y lo que dice el catastro
    2. Mercado          precio por m² de comparables, confianza, pedido y cierre por separado, comparables
    3. Entorno          estación más cercana, estaciones y equipamientos en el radio
    4. Norma            tratamiento del POT, informativo
    5. Calidad          qué se descartó, qué falta, qué fuentes tuvieron novedad
    6. Trazabilidad     comercial, cliente, corrida del ETL, huellas y fuentes
"""
from __future__ import annotations

ETIQUETA_FALTANTE = {
    "direccion": "dirección del inmueble", "coordenadas_exactas": "coordenadas exactas",
    "fecha_publicacion_comparables": "fecha de publicación de los comparables", "precios_de_cierre": "precios de cierre",
    "estado_de_comparables": "estado de los comparables (la base no lo informa)",
    "area_del_inmueble": "área del inmueble", "estado_del_inmueble": "estado del inmueble",
    "tipo_de_inmueble": "tipo de inmueble", "sin_base_de_mercado": "base de mercado para este predio"}
PRECISION = {"P1": "P1 · placa exacta", "P2": "P2 · placa cercana o punto marcado", "P3": "P3 · aproximada",
             "cruce": "cruce de vías", "barrio": "centro del barrio (aproximada)"}
ORIGEN = {"catastro": "nomenclatura oficial del catastro", "pin_usuario": "punto marcado en el mapa",
          "google_autocomplete": "Google", "google_geocoding": "Google", "nominatim": "OpenStreetMap (aproximada)"}
CATEGORIAS = {"educacion": "Educación", "salud": "Salud", "parques": "Parques", "abastecimiento": "Abastecimiento",
              "seguridad": "Seguridad", "cultura": "Cultura", "movilidad": "Movilidad", "empresas": "Empresas",
              "residencial": "Edificios y conjuntos", "otros_equipamientos": "Otros equipamientos"}
ESTADOS = {"remodelado": "Remodelado", "habitable": "Habitable", "por_remodelar": "Por remodelar",
           "sin_clasificar": "Sin clasificar"}


def cop(v) -> str:
    return "sin dato" if v is None else f"{v:,.0f}".replace(",", ".")


def pct(v) -> str:
    return "sin dato" if v is None else f"{v * 100:.2f} %".replace(".", ",")


def _num(v, sufijo: str = "") -> str:
    return "sin dato" if v is None else f"{v:g}{sufijo}"


def _yaml(valor) -> str:
    texto = "" if valor is None else str(valor)
    return '"' + texto.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tipo(t) -> str:
    return str(t or "").replace("_", " ").capitalize()


def nombre_archivo(cotizacion_id: str, fecha: str, version: int) -> str:
    return f"FICHA_{fecha.replace('-', '')}_{cotizacion_id[:8].upper()}_v{version}"


def renderizar(f: dict, max_comparables: int = 40) -> str:
    """Markdown de la ficha del inmueble de una cotización."""
    q, i, m, e, n = f["cotizacion"], f["inmueble"], f["mercado"], f["entorno"], f["norma"]
    c, md, tr, pr = f["calidad_datos_zona"], f["metadatos"], f["trazabilidad"], f.get("predio") or {}
    co, pm, crit = i["coordenadas"], m["precio_m2"], m["criterio_comparables"]
    cli, com = q["cliente"], q["comercial"]
    base = next((b for b in m.get("bases", []) if b["es_principal"]), None)
    titulo = i.get("direccion") or "Punto marcado en el mapa"
    lugar = ", ".join(x for x in (i.get("barrio"), i.get("comuna"), i.get("municipio")) if x)

    L = [
        "---",
        f"ficha: {_yaml(q['ficha_id'])}",
        f"version: {q['version']}",
        f"generada_en: {_yaml(q['generada_en'])}",
        f"cotizacion: {_yaml(q['cotizacion_id'])}",
        f"comercial_usuario: {_yaml(com['usuario'])}",
        f"comercial_nombre: {_yaml(com['nombre'])}",
        f"cliente_tipo_documento: {_yaml(cli['tipo_documento'])}",
        f"cliente_documento: {_yaml(cli['documento'])}",
        f"cliente_nombre: {_yaml(cli['nombre'])}",
        f"inmueble: {_yaml(titulo)}",
        f"barrio: {_yaml(i.get('barrio'))}",
        f"base_mercado: {_yaml(m.get('base_mercado'))}",
        f"confianza: {_yaml(m['confianza']['nivel'])}",
        f"comparables: {pm['n']}",
        f"mediana_cop_m2: {pm['mediana'] if pm['mediana'] is not None else 'null'}",
        "---",
        "",
        f"# Ficha del inmueble · {titulo}",
        "",
        f"Venture One · Cotización `{q['cotizacion_id'][:8].upper()}` · {q['fecha_legible']}",
        "",
        "| | |",
        "|---|---|",
        f"| **Comercial que asesora** | {com['nombre']} (`{com['usuario']}`) |",
        f"| **Cliente** | {cli['nombre']} · {cli['tipo_documento']} {cli['documento']} |",
        f"| **Ficha** | versión {q['version']} · generada el {q['generada_en']} |",
        "",
        "## 1. Inmueble",
        "",
        f"- **Dirección:** {titulo}" + (f" · {i['conjunto']}" if i.get("conjunto") else ""),
        f"- **Ubicación:** {lugar or 'sin barrio identificado'}",
        "- **Características declaradas:** " + " · ".join(x for x in (
            (i.get("tipo") or "").replace("_", " ").capitalize() or "tipo sin declarar",
            f"{_num(i.get('area_m2'))} m²" if i.get("area_m2") is not None else "área sin declarar",
            f"estado **{ESTADOS.get(i.get('estado'), i.get('estado'))}**" if i.get("estado") not in (None, "", "sin_clasificar") else "",
            f"{i['habitaciones']} hab." if i.get("habitaciones") is not None else "",
            f"{i['banos']} baños" if i.get("banos") is not None else "",
            f"{i['parqueaderos']} parq." if i.get("parqueaderos") is not None else "") if x),
    ]
    if i.get("caracteristicas"):
        L.append("- **Otras características declaradas:** " + " · ".join(f"{x['nombre']}: {x['valor']}" for x in i["caracteristicas"]))
    busca = f.get("busqueda_cliente") or {}
    if busca.get("tipos"):
        L.append("- **Lo que busca el cliente:** " + " · ".join(
            _tipo(x) + (" (el que se cotiza)" if x == busca.get("tipo_cotizado") else "") for x in busca["tipos"]))
    if pr:
        ph = {True: "sí", False: "no", None: "sin dato"}[pr.get("propiedad_horizontal")]
        L.append(f"- **Catastro:** CBML `{pr.get('cbml') or 'sin dato'}` · propiedad horizontal: {ph}"
                 + (f" · lote de {_num(pr.get('area_lote_m2'))} m²" if pr.get("area_lote_m2") else "")
                 + (f" · hasta {_num(pr.get('pisos_max'))} pisos" if pr.get("pisos_max") else ""))
        if pr.get("valor_referencia_suelo_m2"):
            L.append(f"- **Valor de referencia del suelo:** {cop(pr['valor_referencia_suelo_m2'])} COP/m² · "
                     f"_{pr['nota_valor_referencia']}_")
    L += [
        f"- **Coordenadas (EPSG:4326):** {co['lat']}, {co['lon']} · precisión {PRECISION.get(co['precision'], co['precision'])} · "
        f"origen: {ORIGEN.get(co['fuente'], co['fuente'])}",
        "",
        "## 2. Mercado",
        "",
    ]
    vig = m.get("vigencia")
    if m.get("base_mercado") is None:
        L += [f"**Sin comparables calculados.** {m.get('motivo_sin_mercado', '')}", ""]
    else:
        criterio = []
        nombres = {"zona": m.get("ubicacion_comparables") or (f"a {cop(m['radio_m'])} m o menos del predio" if m.get("vinculo") == "radio"
                                                                 else "misma zona de mercado"),
                   "estado_canonico": "mismo estado", "tipo_inmueble": "mismo tipo de inmueble"}
        for col in crit.get("columnas_coincidentes", []):
            criterio.append(nombres.get(col, col))
        if crit.get("rango_area_m2"):
            criterio.append(f"área entre {crit['rango_area_m2'][0]:g} y {crit['rango_area_m2'][1]:g} m²")
        for k, v in (crit.get("filtros") or {}).items():
            criterio.append(f"{k.replace('operacion', 'operación')}: {v}")
        L += [
            f"Precio por m² de comparables · base: **{base['etiqueta'] if base else m['base_mercado']}**",
            "",
            f"- Tipo de precio de la referencia: **{m['base_referencia'] or 'sin base'}**",
            *([f"- Vigencia: solo ofertas publicadas o vistas desde el {vig['corte']} "
               f"(máximo {vig['max_meses_publicacion']} meses a la fecha de la ficha, {vig['fecha_referencia']})"] if vig else []),
            f"- Criterio de comparables: {'; '.join(criterio)}",
            "",
            "| Mediana | Mínimo | Máximo | Promedio | Desviación | CV | n |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            f"| {cop(pm['mediana'])} | {cop(pm['minimo'])} | {cop(pm['maximo'])} | {cop(pm['promedio'])} | "
            f"{cop(pm['desviacion'])} | {pct(pm['coef_variacion'])} | {pm['n']} |",
            "",
            f"**Confianza: {m['confianza']['nivel'].upper()}** → {m['confianza']['motivo']}  ",
            f"_{m['confianza']['alcance']}_",
            "",
            "Precio pedido y precio de cierre, por separado: " + " · ".join(
                f"{t} n={v['n']}, mediana {cop(v['mediana'])}" for t, v in m["precio_m2_por_tipo"].items()),
            "",
            "Criterios flexibilizados: " + ("sí, " + ", ".join(s["nombre"] for s in crit["pasos_aplicados"])
                                            if crit.get("aplicada") else "no"),
            "",
        ]
        if base and base.get("exige_estado"):
            L += ["Contexto de la zona (mediana por estado, sin filtro de área): " + " · ".join(
                f"{ESTADOS.get(k, k).lower()} {cop(v['mediana'])} (n={v['n']})" for k, v in m["precio_m2_por_estado_en_zona"].items()), ""]
        else:
            L += ["> La base no informa el estado de cada inmueble: los comparables **no** se filtraron por estado. "
                  "El estado declarado del inmueble queda como dato descriptivo.", ""]
        avisos = _avisos_mercado(f)
        L += [f"> {a}" for a in avisos] + ([""] if avisos else [])
        comps = m["comparables"]
        con_dist = any("distancia_m" in x for x in comps)
        L += [f"### Comparables ({len(comps)})", ""]
        con_fecha = any(x.get("fecha") or x.get("vista_el") for x in comps)
        if comps:
            L += ["| # | Origen | Fuente | Área m² | Precio COP | Precio/m² |" + (" Distancia |" if con_dist else "")
                  + (" Publicación |" if con_fecha else ""),
                  "|---:|---|---|---:|---:|---:|" + ("---:|" if con_dist else "") + ("---|" if con_fecha else "")]
            for k, x in enumerate(comps[:max_comparables], start=1):
                origen = f"[{x.get('barrio') or 'ver anuncio'}]({x['url']})" if str(x.get("url") or "").startswith("https://") else x["origen"]
                cuando = x.get("fecha") or (f"visto el {x['vista_el']}" if x.get("vista_el") else "sin fecha")
                L.append(f"| {k} | {origen} | {x['fuente']} | {x['area_m2']:g} | {cop(x['precio'])} | {cop(x['precio_m2'])} |"
                         + (f" {x.get('distancia_m', '—')} m |" if con_dist else "") + (f" {cuando} |" if con_fecha else ""))
            if len(comps) > max_comparables:
                L += ["", f"_Se muestran los {max_comparables} de menor precio por m²; la lista completa ({len(comps)}) está en el JSON de la ficha._"]
        else:
            L.append("Sin comparables con el criterio aplicado.")
        L.append("")
    if busca.get("oferta_por_tipo"):
        L += ["### Oferta por tipo de inmueble solicitado", "",
              "| Tipo | Ofertas vigentes | Mediana vigente COP/m² | Ofertas históricas | Mediana histórica COP/m² |", "|---|---:|---:|---:|---:|"]
        for x in busca["oferta_por_tipo"]:
            L.append(f"| {_tipo(x['tipo'])}{' · se cotiza' if x['es_cotizado'] else ''} | {x['vigentes']['n']} | {cop(x['vigentes']['mediana_m2'])} | "
                     f"{x['historicas']['n']} | {cop(x['historicas']['mediana_m2'])} |")
        L += ["", f"_{busca['nota']}_", ""]
    historica = f.get("referencia_historica") or {}
    if historica:
        L += ["### Referencia histórica (no vigente)", "",
              "Ofertas que cumplen el criterio pero quedaron fuera del plazo de vigencia. **No son comparables**: se informan como contexto.", "",
              "| Base | n | Mediana COP/m² | Mínimo | Máximo | CV | Publicadas entre |", "|---|---:|---:|---:|---:|---:|---|"]
        for h in historica.values():
            entre = " y ".join(h["publicadas_entre"]) if h.get("publicadas_entre") else "sin fecha"
            L.append(f"| {h['etiqueta']} | {h['n']} | {cop(h['mediana'])} | {cop(h['minimo'])} | {cop(h['maximo'])} | {pct(h['coef_variacion'])} | {entre} |")
        L.append("")
    otras = [b for b in m.get("bases", []) if not b["es_principal"]]
    if otras:
        L += ["### Otras bases de mercado consultadas", "", "| Base | Aplicó | n | Mediana COP/m² | CV | Confianza | Nota |",
              "|---|---|---:|---:|---:|---|---|"]
        for b in otras:
            L.append(f"| {b['etiqueta']} | {'sí' if b['aplicable'] else 'no'} | {b.get('n', '—')} | {cop(b.get('mediana')) if b['aplicable'] else '—'} | "
                     f"{pct(b.get('coef_variacion')) if b['aplicable'] else '—'} | {b.get('confianza', '—')} | {b['motivo'] or 'se reporta aparte; no se mezcla'} |")
        L.append("")

    cercana = e["estacion_mas_cercana"]
    L += [
        f"## 3. Entorno (radio de {e['radio_busqueda_m']} m)" + (" · aproximado" if e["aproximado"] else ""),
        "",
        "- **Estación más cercana:** " + (f"{cercana['nombre']} ({cercana['sistema']}) a {cop(cercana['distancia_m'])} m" if cercana
                                           else "ninguna de los sistemas definidos dentro del barrido de la captura"),
        f"- **Estaciones en el radio:** {e['estaciones_en_radio']}",
    ]
    ref = pr.get("estacion_masiva_mas_cercana")
    if ref and (not cercana or ref["nombre"] != cercana["nombre"]):
        L.append(f"- Estación de transporte masivo más cercana de cualquier sistema: {ref['nombre']} ({ref.get('linea') or 'sin línea'}) "
                 f"a {cop(ref['distancia_m'])} m")
    detalle = e.get("lugares_por_categoria") or {}
    L += ["", "| Categoría | En el radio de la ficha | En el barrido de la captura | Más cercano |", "|---|---:|---:|---:|"]
    for cat, n_radio in e["equipamientos_en_radio"].items():
        d = detalle.get(cat, {})
        L.append(f"| {CATEGORIAS.get(cat, cat)} | {n_radio} | {d.get('en_barrido', '—')} | "
                 f"{str(d['mas_cercano_m']) + ' m' if d.get('mas_cercano_m') is not None else '—'} |")
    for cat, d in detalle.items():
        if cat not in e["equipamientos_en_radio"]:
            L.append(f"| {CATEGORIAS.get(cat, cat)} _(informativa)_ | {d['en_radio']} | {d['en_barrido']} | "
                     f"{str(d['mas_cercano_m']) + ' m' if d['mas_cercano_m'] is not None else '—'} |")
    L += ["", f"Método de distancia: {e['metodo_distancia']}, en metros."
          + (f" Control en {tr['control_distancia']['crs']}: diferencia máxima de {tr['control_distancia']['max_diferencia_m']:g} m."
             if (tr.get("control_distancia") or {}).get("max_diferencia_m") is not None else ""), ""]

    L += ["## 4. Norma urbanística (informativa)", "",
          f"- **Tratamiento POT:** {n['tratamiento_pot'] or n['estado']}" + (f" · {n['motivo']}" if n.get("motivo") else "")]
    rotulos = {"codigo_tratamiento": "Código", "categoria_tratamiento": "Categoría", "clase_suelo": "Clase de suelo",
               "uso_general_del_suelo": "Uso general del suelo"}
    for k, v in (n.get("atributos") or {}).items():
        L.append(f"- {rotulos.get(k, k.replace('_', ' ').capitalize())}: {v}")
    if n.get("fuente"):
        L.append(f"- Fuente: {n['fuente']}")
    L += [f"- _{n['nota']}_", ""]

    invalidos = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in c["anuncios_invalidos"].items()) or "ninguno"
    faltantes = ", ".join(ETIQUETA_FALTANTE.get(x, x.replace("_", " ")) for x in f["datos_faltantes"]) or "ninguno"
    L += ["## 5. Calidad de los datos", "",
          f"- **{'Mercado en el radio' if m.get('vinculo') == 'radio' else 'Mercado de la zona'}:** {c['anuncios_en_zona']} anuncios · {c['duplicados_eliminados']} duplicados · "
          f"{c['anuncios_unicos']} únicos · {c['anuncios_validos']} válidos · inválidos: {invalidos}",
          f"- **Datos faltantes:** {faltantes}"]
    ev = q.get("evidencia") or {}
    if ev.get("fuentes_con_novedad"):
        L.append("- **Fuentes con novedad en la captura:** " + ", ".join(ev["fuentes_con_novedad"])
                 + " (sus datos no entraron; eso no significa que no existan)")
    if q.get("reusada"):
        L.append("- La captura del entorno se reutilizó de una consulta reciente del mismo punto.")
    L.append("")

    alm = tr.get("almacen") or {}
    L += ["## 6. Trazabilidad", "",
          f"- **Cotización:** `{q['cotizacion_id']}` · creada el {q['creada_en']} · evidencia `{(q.get('evidencia_sha256') or 'sin huella')[:16]}`",
          f"- **Comercial:** {com['nombre']} (`{com['usuario']}`) · **Cliente:** {cli['nombre']} ({cli['tipo_documento']} {cli['documento']})",
          f"- **Almacén (esquema estrella):** corrida ETL {alm.get('corrida_etl')} ({alm.get('corrida_estado')}) · "
          f"esquema v{alm.get('version_esquema')} · inmueble `{alm.get('inmueble')}` · predio `{alm.get('predio')}`",
          f"- **Huellas:** parámetros `{tr['hash_parametros'][:16]}` · almacén `{alm.get('huella_almacen')}` · captura `{alm.get('huella_captura')}`",
          f"- **Reglas:** parámetros oficiales v{md['version_parametros']} · motor de ficha {tr.get('version_motor')} · "
          f"duplicados: {', '.join(tr['regla_deduplicacion']['claves_sin_id'])} ({tr['regla_deduplicacion']['representante']})"
          + _regla_entre_sitios(tr["regla_deduplicacion"].get("entre_sitios")),
          "- **Fuentes:** " + " · ".join(md["fuentes"]),
          "",
          f"> {f['aviso']}",
          ""]
    return "\n".join(L)


def _pct(valor: float) -> str:
    return f"{valor * 100:g}".replace(".", ",") + " %"


def _regla_entre_sitios(regla: dict | None) -> str:
    """Cómo se reconoce el mismo inmueble publicado en dos sitios (criterio con tolerancia: va siempre a la vista)."""
    if not regla:
        return ""
    return (f" · entre sitios distintos: {'mismo barrio, ' if regla['exigir_mismo_barrio'] else ''}"
            f"área ±{_pct(regla['tolerancia_area_pct'])} y precio ±{_pct(regla['tolerancia_precio_pct'])}")


def _avisos_mercado(f: dict) -> list[str]:
    m = f["mercado"]
    avisos = []
    rep = m.get("repetidos_entre_sitios")
    if rep and rep["n"]:
        avisos.append(f"{rep['n']} {'anuncio se enlazó' if rep['n'] == 1 else 'anuncios se enlazaron'} como el mismo inmueble "
                      f"publicado en otro sitio (área ±{_pct(rep['tolerancia_area_pct'])}, precio ±{_pct(rep['tolerancia_precio_pct'])}"
                      f"{', mismo barrio' if rep['exigir_mismo_barrio'] else ''}): {'cuenta' if rep['n'] == 1 else 'cuentan'} "
                      "una sola vez. Pueden ser unidades distintas con datos casi iguales.")
    vig = m.get("vigencia")
    if vig:
        vistos = sum(1 for x in m["comparables"] if x.get("vista_el") and not x.get("fecha"))
        if not m["comparables"]:
            avisos.append(f"Sin comparables vigentes: ninguna oferta de las bases consultadas se publicó o se vio desde el {vig['corte']}. "
                          "Encienda las inmobiliarias y vuelva a buscar, o revise la referencia histórica.")
        elif vistos:
            avisos.append(f"{vistos} de los comparables no muestran fecha de publicación: valen por el día en que se vieron publicados.")
        if vig["fuera_de_vigencia"]:
            avisos.append(f"{vig['fuera_de_vigencia']} ofertas quedaron fuera por tener más de {vig['max_meses_publicacion']} meses de publicadas.")
    fechas = sorted({x["fecha"][:4] for x in m["comparables"] if x.get("fecha")}) if not vig else []
    if fechas:
        avisos.append(f"Los comparables son ofertas publicadas en {', '.join(fechas)}: precio pedido de referencia, no oferta vigente.")
    elif m["comparables"] and not vig:
        avisos.append("Los comparables no traen fecha de publicación: no se puede declarar su vigencia.")
    if m["precio_m2_por_tipo"].get("cierre", {}).get("n", 0) == 0 and m["comparables"]:
        avisos.append("No hay precios de cierre: la referencia es de precio pedido.")
    return avisos
