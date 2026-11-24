"""Lectura de la base para la interfaz y las exportaciones."""
from __future__ import annotations

import csv
import io

from . import bd
from .servicio import NOMBRES_SESION

ATRIBUCION = {
    "catastro": "Subsecretaría de Catastro – Alcaldía de Medellín (GeoMedellín, CC BY-SA)",
    "geomedellin": "GeoMedellín – Alcaldía de Medellín (CC BY-SA)",
    "registro_ic": "Registro de Industria y Comercio – Alcaldía de Medellín",
    "oime": "Observatorio Inmobiliario de Medellín (OIME) – Subsecretaría de Catastro",
    "osm": "© colaboradores de OpenStreetMap (ODbL)",
    "google": "Google Maps",
}


def listar_capturas(p: dict, limite: int = 30, permitidas: set[str] | None = None) -> list[dict]:
    """Capturas recientes. `permitidas` limita a las que el usuario puede abrir (None = todas)."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute(
            "SELECT c.captura_id, c.creada_en, c.direccion_ingresada, c.direccion_catastral, c.conjunto_nombre, "
            "c.estado, c.lat, c.lng, c.precision_geo, p.barrio_nombre, "
            "(SELECT COUNT(DISTINCT entidad_id) FROM entorno_items i WHERE i.captura_id=c.captura_id "
            " AND i.en_mapa=1 AND i.motivo_exclusion IS NULL) AS lugares "
            "FROM capturas c LEFT JOIN predios p ON p.captura_id=c.captura_id "
            "ORDER BY c.creada_en DESC LIMIT ?", (limite if permitidas is None else 10_000,)).fetchall()
    return [dict(f) for f in filas if permitidas is None or f["captura_id"] in permitidas][:limite]


def leer_captura(p: dict, captura_id: str) -> dict | None:
    """Resultado completo de una captura, listo para pintar. None si no existe."""
    tax = p["taxonomia"]["categorias"]
    tope = p["params"]["respuesta"]["max_items_por_categoria"]
    sesion = NOMBRES_SESION.get(captura_id, {})
    with bd.conectar(p["rutas"]["bd"]) as con:
        c = con.execute("SELECT * FROM capturas WHERE captura_id=?", (captura_id,)).fetchone()
        if c is None:
            return None
        predio = con.execute("SELECT * FROM predios WHERE captura_id=?", (captura_id,)).fetchone()
        resumen = [dict(r) for r in con.execute(
            "SELECT categoria, fuente, conteo, excluidos, distancia_min_m, saturado FROM resumen_categoria "
            "WHERE captura_id=? ORDER BY categoria, fuente", (captura_id,))]
        ejecuciones = [dict(r) for r in con.execute(
            "SELECT fuente, capa, estado, registros, descartados, llamadas, mensaje FROM ejecuciones "
            "WHERE captura_id=? ORDER BY ejecucion_id", (captura_id,))]
        indicadores = {}
        for r in con.execute("SELECT indicador, base, valor, unidad, detalle_json FROM indicadores "
                             "WHERE captura_id=? ORDER BY base DESC", (captura_id,)):
            # 'todas' pisa a 'abiertas' cuando hay Google vigente: es la lectura más completa de la sesión.
            indicadores.setdefault(r["base"], {})[r["indicador"]] = {
                "valor": r["valor"], "unidad": r["unidad"], "detalle": bd.de_json(r["detalle_json"])}
        # Lugares: un registro por entidad (el maestro), con cuántas fuentes lo respaldan.
        filas = con.execute(
            "SELECT i.fuente, i.capa, i.id_externo, i.categoria, i.subtipo, i.nombre, i.lat, i.lng, i.distancia_m, "
            "i.curado_por, i.entidad_id, i.banderas_json, "
            "(SELECT COUNT(*) FROM entorno_items j WHERE j.captura_id=i.captura_id AND j.entidad_id=i.entidad_id) AS n_fuentes "
            "FROM entorno_items i WHERE i.captura_id=? AND i.en_mapa=1 AND i.es_maestro=1 "
            "AND i.motivo_exclusion IS NULL AND i.lat IS NOT NULL ORDER BY i.categoria, i.distancia_m",
            (captura_id,)).fetchall()
        cuenta = {r["categoria"]: r["n"] for r in con.execute(
            "SELECT categoria, COUNT(DISTINCT entidad_id) AS n FROM entorno_items WHERE captura_id=? AND en_mapa=1 "
            "AND motivo_exclusion IS NULL GROUP BY categoria", (captura_id,))}
        en_mapa: dict[str, dict[str, int]] = {}
        for r in con.execute(
                "SELECT categoria, fuente, COUNT(*) AS n FROM entorno_items WHERE captura_id=? AND en_mapa=1 "
                "AND motivo_exclusion IS NULL GROUP BY categoria, fuente", (captura_id,)):
            en_mapa.setdefault(r["categoria"], {})[r["fuente"]] = r["n"]
        log = con.execute(
            "SELECT origen, aceptado, COUNT(*) AS n FROM curaduria_log WHERE captura_id=? GROUP BY origen, aceptado",
            (captura_id,)).fetchall()
        muestra_ia = [dict(r) for r in con.execute(
            "SELECT item_ref, campo, valor_antes, valor_despues, regla, aceptado FROM curaduria_log "
            "WHERE captura_id=? AND origen='claude' ORDER BY aceptado DESC, log_id LIMIT 40", (captura_id,))]
        totales = con.execute(
            "SELECT COUNT(*) AS registros, SUM(es_maestro=0) AS enlazados, SUM(motivo_exclusion IS NOT NULL) AS excluidos, "
            "SUM(curado_por='claude') AS por_claude FROM entorno_items WHERE captura_id=?", (captura_id,)).fetchone()
        en_revision = con.execute(
            "SELECT COUNT(*) FROM cola_revision WHERE captura_id=? AND decision IS NULL", (captura_id,)).fetchone()[0]
        n_ofertas = con.execute(
            "SELECT COUNT(*) AS n, SUM(motivo_exclusion IS NOT NULL) AS excluidas FROM ofertas WHERE captura_id=?",
            (captura_id,)).fetchone()

    items, por_cat = [], {}
    for r in filas:
        n = por_cat.get(r["categoria"], 0)
        if n >= tope:
            continue
        por_cat[r["categoria"]] = n + 1
        nombre = r["nombre"] or (sesion.get(r["id_externo"]) if r["fuente"] == "google" else None)
        items.append({"ref": f"{r['fuente']}:{r['capa']}:{r['id_externo']}", "fuente": r["fuente"], "capa": r["capa"],
                      "categoria": r["categoria"], "subtipo": r["subtipo"], "nombre": nombre,
                      "lat": r["lat"], "lng": r["lng"], "distancia_m": r["distancia_m"],
                      "n_fuentes": r["n_fuentes"], "curado_por": r["curado_por"],
                      "banderas": bd.de_json(r["banderas_json"], [])})

    categorias = []
    for cat, meta in tax.items():
        de_cat = [r for r in resumen if r["categoria"] == cat]
        cercanos = [x["distancia_m"] for x in items if x["categoria"] == cat and x["distancia_m"] is not None]
        categorias.append({"id": cat, "etiqueta": meta["etiqueta"], "color": meta["color"], "letra": meta["letra"],
                           "oculta_al_abrir": bool(meta.get("oculta_al_abrir")),
                           "lugares": cuenta.get(cat, 0), "dist_min_m": min(cercanos) if cercanos else None,
                           "saturado": any(r["saturado"] for r in de_cat),
                           "por_fuente": en_mapa.get(cat, {})})

    cambios = {"regla": 0, "claude": 0, "claude_rechazados": 0}
    for r in log:
        if r["origen"] == "claude":
            cambios["claude" if r["aceptado"] else "claude_rechazados"] += r["n"]
        else:
            cambios["regla"] += r["n"]
    fuentes_usadas = sorted({e["fuente"] for e in ejecuciones if e["estado"] in ("ok", "parcial")})
    pr = None
    if predio is not None:
        pr = {k: predio[k] for k in predio.keys() if k not in ("captura_id", "atributos_json")}
        pr["atributos"] = bd.de_json(predio["atributos_json"], {})
    base_ind = indicadores.get("todas") or {}
    ind = dict(indicadores.get("abiertas") or {})
    ind.update(base_ind)
    return {
        "captura_id": captura_id, "creada_en": c["creada_en"], "estado": c["estado"], "duracion_s": c["duracion_s"],
        "punto": {"lat": c["lat"], "lng": c["lng"], "radio_m": c["radio_m"], "origen": c["origen_coordenada"],
                  "precision": c["precision_geo"], "direccion_ingresada": c["direccion_ingresada"],
                  "direccion_catastral": c["direccion_catastral"], "direccion_normalizada": c["direccion_normalizada"],
                  "en_cobertura": c["en_cobertura"], "coord_expira_en": c["coord_expira_en"]},
        "conjunto": {"nombre": c["conjunto_nombre"], "tipo": c["conjunto_tipo"], "origen": c["conjunto_origen"]},
        "predio": pr, "categorias": categorias, "resumen": resumen, "items": items,
        "indicadores": ind, "base_indicadores": "todas" if base_ind else "abiertas",
        "ofertas": {"total": n_ofertas["n"] or 0, "excluidas": n_ofertas["excluidas"] or 0},
        "curaduria": {"proveedor": c["curaduria_proveedor"], "modelo": c["curaduria_modelo"],
                      "nota": (c["nota_curaduria"] or "").split("\n") if c["nota_curaduria"] else [],
                      "cambios_regla": cambios["regla"], "cambios_claude": cambios["claude"],
                      "rechazados_claude": cambios["claude_rechazados"],
                      "registros": totales["registros"] or 0, "enlazados": totales["enlazados"] or 0,
                      "excluidos": totales["excluidos"] or 0, "por_claude": totales["por_claude"] or 0,
                      "en_revision": en_revision, "muestra": muestra_ia},
        "ejecuciones": ejecuciones, "avisos": bd.de_json(c["avisos_json"], []),
        "atribuciones": [ATRIBUCION[f] for f in fuentes_usadas if f in ATRIBUCION],
        "llamadas_google": c["llamadas_google"], "huella_config": c["huella_config"], "version": c["version_app"],
        "aviso_alcance": "Captura descriptiva de datos del entorno. No constituye evaluación ni recomendación de "
                         "inversión, ni concepto jurídico o urbanístico.",
    }


TABLAS_EXPORTABLES = {
    "entorno": ("entorno_items", "categoria, distancia_m"),
    "ofertas": ("ofertas", "distancia_m"),
    "indicadores": ("indicadores", "base, indicador"),
    "bitacora": ("curaduria_log", "log_id"),
    "ejecuciones": ("ejecuciones", "ejecucion_id"),
    "revision": ("cola_revision", "rev_id"),
    "predio": ("predios", "captura_id"),
}


def exportar_csv(p: dict, captura_id: str, tabla: str) -> str | None:
    """CSV de una tabla de la captura (UTF-8 con BOM, para que Excel lea bien las tildes)."""
    if tabla not in TABLAS_EXPORTABLES:
        return None
    nombre, orden = TABLAS_EXPORTABLES[tabla]
    with bd.conectar(p["rutas"]["bd"]) as con:
        cur = con.execute(f"SELECT * FROM {nombre} WHERE captura_id=? ORDER BY {orden}", (captura_id,))
        columnas = [d[0] for d in cur.description]
        filas = cur.fetchall()
    salida = io.StringIO()
    escritor = csv.writer(salida, lineterminator="\n")
    escritor.writerow(columnas)
    for f in filas:
        escritor.writerow(list(f))
    return "﻿" + salida.getvalue()
