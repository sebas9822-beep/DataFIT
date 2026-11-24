"""Etapa 7 · Ensamblaje de la ficha por inmueble (JSON) y su vista de lectura (Markdown).

El JSON sigue el esquema oficial (`reto_oficial/docs/plantilla_ficha.md` y
`reto_oficial/ejemplos/ficha_ejemplo.json`). Los campos adicionales de trazabilidad del proyecto
(`base_referencia`, `precio_m2_por_tipo`, `criterio_comparables`, `trazabilidad`, `aviso`…) se agregan
sin cambiar ni quitar ningún campo oficial.

La ficha es descriptiva: no recomienda comprar, no da un valor de inversión y no emite concepto
jurídico sobre el POT.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from . import __version__, confianza, entorno, mercado

AVISO = ("Ficha descriptiva de mercado y entorno. No constituye evaluación ni recomendación de inversión, "
         "ni concepto jurídico sobre la norma urbanística.")


def calidad_zona(zona: str, anuncios: pd.DataFrame) -> dict:
    del_zona = anuncios[anuncios["zona"] == zona]
    unicos = del_zona[~del_zona["es_duplicado"]]
    invalidos = Counter(m for m in unicos["motivo_invalido"] if isinstance(m, str))
    absorbidos_validos = del_zona[del_zona["es_duplicado"] & del_zona["motivo_invalido"].isna()
                                  & del_zona["id_representante"].map(
                                      anuncios.set_index("origen")["motivo_invalido"]).notna()]
    return {
        "anuncios_en_zona": int(len(del_zona)),
        "duplicados_eliminados": int(del_zona["es_duplicado"].sum()),
        "anuncios_unicos": int(len(unicos)),
        "anuncios_invalidos": dict(sorted(invalidos.items())),
        "anuncios_validos": int(len(unicos) - sum(invalidos.values())),
        "grupos_duplicados_ambiguos": int(del_zona.loc[del_zona["grupo_ambiguo"], "grupo_dup"].nunique()),
        "validos_absorbidos_por_representante_invalido": int(len(absorbidos_validos)),
    }


def datos_faltantes(inmueble: pd.Series, comps: pd.DataFrame, p: dict) -> list[str]:
    """Códigos oficiales: direccion, coordenadas_exactas, fecha_publicacion_comparables, precios_de_cierre."""
    faltantes = []
    if not str(inmueble.get("direccion") or "").strip():
        faltantes.append("direccion")
    if inmueble["precision_coordenadas"] not in p["entorno"]["precision_exacta"]:
        faltantes.append("coordenadas_exactas")
    if not comps["fecha"].notna().any():
        faltantes.append("fecha_publicacion_comparables")
    if not (comps["tipo_precio"] == "cierre").any():
        faltantes.append("precios_de_cierre")
    return faltantes


def construir_ficha(inmueble: pd.Series, anuncios: pd.DataFrame, capas: dict | None, pot, p: dict, contexto: dict,
                    bloques: dict | None = None) -> dict:
    """Ficha con el esquema oficial más trazabilidad.

    El lote oficial calcula entorno y norma desde las capas GeoJSON (`capas`, `pot`). El motor sobre el
    almacén los trae ya calculados desde el esquema estrella en `bloques` = {entorno, control, norma}.
    """
    m = mercado.bloque_mercado(inmueble, anuncios, p)
    comps = m.pop("_comparables_df")
    est = m["precio_m2"]
    m_oficial = {
        "estado_referencia": m["estado_referencia"],
        "precio_m2": est,
        "confianza": confianza.calificar(est["n"], est["coef_variacion"], p["reglas"]["confianza"]),
        "comparables": m["comparables"],
        "precio_m2_por_estado_en_zona": m["precio_m2_por_estado_en_zona"],
        "base_referencia": m["base_referencia"],
        "mezcla_pedido_cierre": m["mezcla_pedido_cierre"],
        "precio_m2_por_tipo": m["precio_m2_por_tipo"],
        "criterio_comparables": m["criterio_comparables"],
    }
    if p["ficha"]["incluir_valor_indicativo"]:
        m_oficial["valor_indicativo"] = round(est["mediana"] * float(inmueble["area_m2"])) if est["mediana"] else None
    if bloques is not None:
        ent, control, norma = bloques["entorno"], bloques.get("control"), bloques["norma"]
    else:
        ent = entorno.bloque_entorno(inmueble, capas, p)
        control = entorno.control_proyectado(inmueble, capas, ent, p) if p["entorno"]["control_proyectado"] else None
        norma = entorno.norma_pot(inmueble, pot, p)
    fuentes = contexto.get("fuentes") or (
        list(p["ficha"]["fuentes"]) + ([p["reglas"]["norma"]["archivo_pot"]] if pot is not None else []))
    return {
        "id_inmueble": str(inmueble["id_inmueble"]),
        "inmueble": {
            "direccion": str(inmueble["direccion"]).strip() or None,
            "barrio": inmueble["barrio"],
            "municipio": inmueble["municipio"],
            "zona": inmueble["zona"],
            "area_m2": float(inmueble["area_m2"]),
            "estado_texto": inmueble["estado"],
            "estado": inmueble["estado_canonico"],
            "coordenadas": {"lat": float(inmueble["lat"]), "lon": float(inmueble["lon"]),
                            "precision": inmueble["precision_coordenadas"], "fuente": inmueble["fuente_coordenadas"]},
        },
        "mercado": m_oficial,
        "calidad_datos_zona": calidad_zona(inmueble["zona"], anuncios),
        "entorno": ent,
        "norma": norma,
        "datos_faltantes": datos_faltantes(inmueble, comps, p),
        "metadatos": {
            "version_parametros": p["_meta"]["version_parametros"],
            "fecha_generacion": contexto["fecha_generacion"],
            "fecha_capas_osm": contexto["fecha_capas_osm"],
            "fuentes": fuentes,
        },
        "trazabilidad": {
            "version_pipeline": __version__,
            "hash_parametros": p["_meta"]["hash_params"],
            "regla_deduplicacion": {"claves_sin_id": p["reglas"]["deduplicacion"]["claves_sin_id"],
                                    "representante": p["deduplicacion"]["representante"]},
            "control_distancia": control,
            "archivos": contexto["archivos"],
        },
        "aviso": AVISO,
    }


# ------------------------------------------------------------------ vista de lectura (plantilla oficial)

def _cop(v) -> str:
    return "sin dato" if v is None else f"{v:,.0f}".replace(",", ".")


def _cv(v) -> str:
    return "sin dato" if v is None else f"{v * 100:.2f} %".replace(".", ",")


def renderizar_markdown(f: dict) -> str:
    """Vista de lectura de `docs/plantilla_ficha.md`. Solo presenta lo que ya está en el JSON."""
    i, m, e, n, c = f["inmueble"], f["mercado"], f["entorno"], f["norma"], f["calidad_datos_zona"]
    co, pm, md = i["coordenadas"], m["precio_m2"], f["metadatos"]
    contexto = " · ".join(f"{k.replace('_', ' ')} {_cop(v['mediana'])} (n={v['n']})"
                          for k, v in m["precio_m2_por_estado_en_zona"].items())
    cercana = e["estacion_mas_cercana"]
    est_txt = f"{cercana['nombre']} ({cercana['sistema']}) a {cercana['distancia_m']} m" if cercana else "sin estaciones"
    equip = " · ".join(f"{k} {v}" for k, v in e["equipamientos_en_radio"].items())
    invalidos = ", ".join(f"{k} {v}" for k, v in c["anuncios_invalidos"].items()) or "ninguno"
    flex = m["criterio_comparables"]
    lineas = [
        f"# Ficha de mercado y entorno · {f['id_inmueble']}",
        "",
        f"Generada el {md['fecha_generacion']} · Parámetros v{md['version_parametros']} · OSM {md['fecha_capas_osm']}",
        "",
        "## Inmueble",
        "",
        f"- {i['direccion'] or 'Sin dirección'} · {i['barrio']}, {i['municipio']} (zona `{i['zona']}`)",
        f"- {i['area_m2']:g} m² · Estado: **{i['estado']}** (\"{i['estado_texto']}\")",
        f"- Ubicación: {co['lat']}, {co['lon']} (precisión: {co['precision']}; {co['fuente']})",
        "",
        "## Mercado",
        "",
        f"Precio/m² de comparables en estado **{m['estado_referencia']}** "
        f"(base: precio {m['base_referencia'] or 'sin base'}; área entre {flex['rango_area_m2'][0]:g} y "
        f"{flex['rango_area_m2'][1]:g} m²)",
        "",
        "| Mediana | Mínimo | Máximo | Promedio | Desviación | CV | n |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {_cop(pm['mediana'])} | {_cop(pm['minimo'])} | {_cop(pm['maximo'])} | {_cop(pm['promedio'])} | "
        f"{_cop(pm['desviacion'])} | {_cv(pm['coef_variacion'])} | {pm['n']} |",
        "",
        f"**Confianza: {m['confianza']['nivel'].upper()}** → {m['confianza']['motivo']}",
        f"_{m['confianza']['alcance']}_",
        "",
        f"Contexto de la zona (mediana por estado, sin filtro de área): {contexto}",
        "",
        "Precio pedido y precio de cierre, por separado: " + " · ".join(
            f"{t} n={v['n']}, mediana {_cop(v['mediana'])}" for t, v in m["precio_m2_por_tipo"].items()),
        "",
        "Criterios flexibilizados: " + ("sí, " + ", ".join(s["nombre"] for s in flex["pasos_aplicados"])
                                        if flex["aplicada"] else "no"),
        "",
        "| # | Origen | Fuente | Área m² | Precio COP | Precio/m² |",
        "|---:|---|---|---:|---:|---:|",
    ]
    lineas += [f"| {k} | {x['origen']} | {x['fuente']} | {x['area_m2']:g} | {_cop(x['precio'])} | {_cop(x['precio_m2'])} |"
               for k, x in enumerate(m["comparables"], start=1)] or ["| – | sin comparables | | | | |"]
    lineas += [
        "",
        "## Entorno",
        "",
        f"- Radio de búsqueda: {e['radio_busqueda_m']} m" + (" **(aproximado: coordenadas de barrio)**" if e["aproximado"] else ""),
        f"- Estación más cercana: {est_txt}",
        f"- Estaciones en el radio: {e['estaciones_en_radio']}",
        f"- Equipamientos en el radio: {equip}",
        f"- Método de distancia: {e['metodo_distancia']}",
    ]
    ctrl = f["trazabilidad"]["control_distancia"]
    if ctrl:
        lineas.append(f"- Control en {ctrl['crs']}: estación a {ctrl['distancia_estacion_m']} m "
                      f"(diferencia {ctrl['diferencia_m']:+d} m); equipamientos que cambian de lado del radio: "
                      f"{ctrl['equipamientos_que_cambian']}")
    lineas += [
        "",
        "## Norma",
        "",
        f"- Tratamiento POT: {n['tratamiento_pot'] or n['estado']}" + (f" · {n['motivo']}" if n["motivo"] else ""),
        f"- _{n['nota']}_",
        "",
        "## Calidad de los datos",
        "",
        f"- Zona: {c['anuncios_en_zona']} anuncios · {c['duplicados_eliminados']} duplicados · "
        f"{c['anuncios_unicos']} únicos · {c['anuncios_validos']} válidos · inválidos: {invalidos}",
        f"- Grupos de duplicados ambiguos (ids distintos): {c['grupos_duplicados_ambiguos']} · "
        f"anuncios válidos absorbidos por un representante inválido: {c['validos_absorbidos_por_representante_invalido']}",
        "- Datos faltantes: " + (", ".join(f["datos_faltantes"]) or "ninguno"),
        "",
        "## Fuentes",
        "",
        "- " + " · ".join(md["fuentes"]),
        f"- Huella de parámetros: `{f['trazabilidad']['hash_parametros'][:16]}` · pipeline {f['trazabilidad']['version_pipeline']}",
        "",
        f"> {f['aviso']}",
        "",
    ]
    return "\n".join(lineas)
