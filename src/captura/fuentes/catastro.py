"""Capa oficial del punto: catastro, norma POT y contexto institucional (Alcaldía de Medellín).

Responde "qué predio, qué barrio y qué norma tiene este punto" sin extraer nada de portales.
Los datos no valen como certificación catastral ni como concepto urbanístico.
"""
from __future__ import annotations

import asyncio

import httpx

from .. import geo
from ..direcciones import normalizar
from . import arcgis
from .base import Ejecucion, FuenteBloqueada, primero

CITA = "Subsecretaría de Catastro – Alcaldía de Medellín (GeoMedellín)"


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


async def geocodificar(cli: httpx.AsyncClient, p: dict, texto: str) -> dict:
    """Dirección → punto con la nomenclatura domiciliaria oficial.

    Placa exacta = P1 (con CBML). Misma vía y cruce con la placa más cercana = P2.
    Si la dirección no se puede interpretar, no se inventa un punto.
    """
    d = normalizar(texto)
    if not d.valida:
        return {"ok": False, "motivo": "direccion_no_interpretable", "entrada": texto}
    capa = p["fuentes"]["capas_predio"]["nomenclatura"]
    donde = f"tipo_via='{d.tipo_via}' AND numero_via={d.num_via} AND numero_cruce={d.num_cruce}"
    campos = ("cbml,via,placa,numero_placa,apendice_via,orientacion_via,apendice_cruce,"
              "orientacion_cruce,longitud,latitud,codigo_postal")
    feats, _, _ = await arcgis.consultar(cli, p, capa["servicio"], capa["capa"], donde=donde,
                                         campos=campos, geometria=False)
    filas = []
    for f in feats:
        a = arcgis.atributos(f)
        if _txt(a.get("apendice_via")).upper() != d.ap_via or _txt(a.get("apendice_cruce")).upper() != d.ap_cruce:
            continue
        if (_txt(a.get("orientacion_via")).upper() == "S") != d.sur_via:
            continue
        if (_txt(a.get("orientacion_cruce")).upper() == "S") != d.sur_cruce:
            continue
        if a.get("latitud") is None or a.get("longitud") is None:
            continue
        num = _txt(a.get("numero_placa"))
        a["_num"] = int(num) if num.isdigit() else None
        filas.append(a)
    if not filas:
        return {"ok": False, "motivo": "no_esta_en_la_nomenclatura_oficial", "entrada": texto,
                "direccion_canonica": d.canonica}
    exactas = [a for a in filas if a["_num"] == d.placa]
    reglas = p["reglas"]["ubicacion"]["precision"]
    if exactas:
        a, precision, metodo = exactas[0], reglas["placa_exacta"], "placa exacta"
    else:
        con_num = [a for a in filas if a["_num"] is not None] or filas
        a = min(con_num, key=lambda x: abs((x["_num"] or 0) - d.placa))
        precision, metodo = reglas["placa_cercana"], f"placa más cercana en la cuadra ({_txt(a.get('placa'))})"
    return {"ok": True, "lat": float(a["latitud"]), "lng": float(a["longitud"]),
            "precision": precision, "metodo": metodo, "origen": "catastro",
            "cbml": _txt(a.get("cbml")) or None, "direccion_canonica": d.canonica,
            "direccion_catastral": f"{_txt(a.get('via'))} # {_txt(a.get('placa'))}",
            "interior": d.interior, "codigo_postal": _txt(a.get("codigo_postal")) or None, "fuente": CITA}


async def _capa(cli, p, nombre: str, **kw) -> tuple[str, list[dict] | Exception, Ejecucion]:
    capa = p["fuentes"]["capas_predio"][nombre]
    ej = Ejecucion("catastro", f"punto_{nombre}")
    try:
        feats, llamadas, _ = await arcgis.consultar(cli, p, capa["servicio"], capa["capa"], **kw)
        ej.llamadas, ej.registros = llamadas, len(feats)
        return nombre, feats, ej.cerrar("ok")
    except FuenteBloqueada as e:
        return nombre, e, ej.cerrar("bloqueado", str(e))
    except Exception as e:                       # una capa caída no tumba la captura
        return nombre, e, ej.cerrar("error", f"{type(e).__name__}: {str(e)[:160]}")


def _attrs(res: dict, nombre: str) -> list[dict]:
    feats = res.get(nombre)
    return [arcgis.atributos(f) for f in feats] if isinstance(feats, list) else []


def _sin_tecnicos(a: dict | None) -> dict:
    return {k: v for k, v in (a or {}).items() if k not in ("objectid", "geom", "t_id") and not k.startswith("st_")}


async def contexto_punto(cli: httpx.AsyncClient, p: dict, lat: float, lng: float) -> dict:
    """Predio, norma y candidatos a nombre del conjunto para un punto (lat, lng)."""
    ub = p["reglas"]["ubicacion"]
    pt = (lat, lng)
    tareas = [
        _capa(cli, p, "barrio", punto=pt, geometria=False),
        _capa(cli, p, "lote", punto=pt, geometria=False),
        _capa(cli, p, "zge", punto=pt, geometria=False),
        _capa(cli, p, "tratamiento", punto=pt, geometria=False),
        _capa(cli, p, "uso_suelo", punto=pt, geometria=False),
        _capa(cli, p, "bic", punto=pt, geometria=False),
        _capa(cli, p, "bic_influencia", punto=pt, geometria=False),
        _capa(cli, p, "cuadrante", punto=pt, geometria=False),
        _capa(cli, p, "nomenclatura", punto=pt, distancia_m=ub["radio_placa_cercana_m"], geometria=False,
              campos="objectid,cbml,via,placa,numero_placa,longitud,latitud,codigo_postal"),
        _capa(cli, p, "toponimia", punto=pt, distancia_m=ub["radio_conjunto_m"], geometria=False),
        _capa(cli, p, "estaciones", donde="1=1", campos="nombre,linea,estado,tipo,tipo_est"),
    ]
    resultados = await asyncio.gather(*tareas)
    res = {n: f for n, f, _ in resultados}
    ejecuciones = [e for _, _, e in resultados]

    barrio = (_attrs(res, "barrio") or [None])[0]
    lote = (_attrs(res, "lote") or [None])[0]
    if lote is None and not isinstance(res.get("lote"), list):
        _, f, e = await _capa(cli, p, "lote_respaldo", punto=pt, geometria=False)
        ejecuciones.append(e)
        res["lote"] = f
        lote = (_attrs(res, "lote") or [None])[0]
    en_cobertura = barrio is not None
    cbml = _txt(lote.get("cbml")) if lote else ""

    construcciones, topo_lote = [], []
    if cbml and cbml.strip("0"):
        extra = await asyncio.gather(
            _capa(cli, p, "construccion", donde=f"cbml='{cbml}'", geometria=False,
                  campos="numero_pisos,numero_sotanos,area_construida,tipo_construccion"),
            _capa(cli, p, "toponimia", donde=f"cbml='{cbml}'", geometria=False))
        for n, _, e in extra:
            e.capa = f"punto_{n}_por_cbml"
            ejecuciones.append(e)
        construcciones = [arcgis.atributos(f) for f in extra[0][1]] if isinstance(extra[0][1], list) else []
        topo_lote = [arcgis.atributos(f) for f in extra[1][1]] if isinstance(extra[1][1], list) else []

    # --- placa oficial más cercana ---
    placa = None
    for a in _attrs(res, "nomenclatura"):
        if a.get("latitud") is None or a.get("longitud") is None:
            continue
        dist = geo.haversine_m(lat, lng, float(a["latitud"]), float(a["longitud"]))
        if placa is None or dist < placa["dist"]:
            placa = {"via": _txt(a.get("via")), "placa": _txt(a.get("placa")), "dist": dist,
                     "cbml": _txt(a.get("cbml")), "codigo_postal": _txt(a.get("codigo_postal"))}

    # --- estación del sistema masivo más cercana (aunque quede fuera del radio) ---
    estacion = None
    if isinstance(res.get("estaciones"), list):
        for f in res["estaciones"]:
            g = arcgis.geometria(f)
            if not g:
                continue
            dist = geo.haversine_m(lat, lng, g["lat"], g["lng"])
            if estacion is None or dist < estacion["dist"]:
                a = arcgis.atributos(f)
                estacion = {"nombre": _txt(a.get("nombre")), "linea": _txt(a.get("linea")), "dist": dist}

    # --- candidatos a nombre del conjunto o edificio (toponimia residencial) ---
    tipos_res = set(ub["tipos_toponimia_residencial"])
    tax = p["taxonomia"]["toponimia"]
    candidatos, vistos = [], set()
    for a, mismo in [(x, True) for x in topo_lote] + [(x, False) for x in _attrs(res, "toponimia")]:
        tipo = _txt(a.get("tipo_sitio"))
        tipo = tipo.zfill(2) if tipo.isdigit() else tipo
        oid = a.get("objectid")
        if tipo not in tipos_res or not _txt(a.get("nombre")) or oid in vistos:
            continue
        vistos.add(oid)
        mismo_lote = mismo or (bool(cbml) and _txt(a.get("cbml")) == cbml)
        dist = None
        if a.get("latitud") is not None and a.get("longitud") is not None:
            dist = round(geo.haversine_m(lat, lng, float(a["latitud"]), float(a["longitud"])), 1)
        candidatos.append({"id": f"catastro:toponimia:{oid}", "nombre": _txt(a.get("nombre")),
                           "tipo": tax.get(tipo, [None, "edificio"])[1], "origen": "catastro",
                           "distancia_m": dist, "mismo_lote": mismo_lote, "contiene_punto": False})

    trat = (_attrs(res, "tratamiento") or [None])[0]
    uso = (_attrs(res, "uso_suelo") or [None])[0]
    zge = (_attrs(res, "zge") or [None])[0]
    bic = (_attrs(res, "bic") or [None])[0]
    cuad = (_attrs(res, "cuadrante") or [None])[0]
    cond = lote.get("condicion_predio") if lote else None

    if not en_cobertura:
        estado_norma = "pendiente_fuente_municipal"
    elif trat:
        estado_norma = "ok"
    else:
        estado_norma = "pendiente_capa"

    def de(a: dict | None, campos: list[str]):
        return (_txt(primero(a, campos)) or None) if a else None

    predio = {
        "cbml": cbml or None,
        "cobama": cbml[:7] if len(cbml) >= 7 else None,
        "barrio_cod": de(barrio, ["codigo"]),
        "barrio_nombre": de(barrio, ["nombre_barrio"]),
        "comuna_cod": de(barrio, ["comuna"]),
        "comuna_nombre": (de(barrio, ["nombre_comuna"]) or "").title() or None,
        "area_lote": lote.get("area_lote") if lote else None,
        "tipo_lote": de(lote, ["tipo_lote"]),
        "condicion_predio": None if cond is None else str(cond),
        "es_ph": None if cond is None else int(cond in ub["condicion_predio_ph"]),
        "n_construcciones": len(construcciones) if cbml else None,
        "pisos_max": max((c.get("numero_pisos") or 0 for c in construcciones), default=None),
        "area_construida": round(sum(c.get("area_construida") or 0 for c in construcciones), 2) if construcciones else None,
        "codigo_postal": (placa or {}).get("codigo_postal") or None,
        "via_cercana": (placa or {}).get("via") or None,
        "placa_cercana": (placa or {}).get("placa") or None,
        "dist_placa_m": round(placa["dist"], 1) if placa else None,
        "zge_valor_m2": zge.get("valor_m2") if zge else None,
        "zge_vigencia": de(zge, ["vigencia"]),
        "tipo_valor_zge": "referencia_zgh" if zge else None,
        "pot_tratamiento": de(trat, ["tratamiento"]),
        "pot_codigo": de(trat, ["codigo_tramiento", "codigo_tratamiento"]),
        "pot_categoria": de(trat, ["categoria_tratamiento"]),
        "pot_clase_suelo": de(trat, ["clase_suelo"]),
        "pot_uso_general": de(uso, ["areagraluso"]),
        "bic_nombre": de(bic, ["nombre"]),
        "en_zona_influencia_bic": int(bool(_attrs(res, "bic_influencia"))) if isinstance(res.get("bic_influencia"), list) else None,
        "cuadrante_policia": de(cuad, ["distrito", "n_cuadrante"]),
        "estacion_policia": de(cuad, ["estacion"]),
        "estacion_masiva": estacion["nombre"] if estacion else None,
        "estacion_masiva_linea": estacion["linea"] if estacion else None,
        "estacion_masiva_m": round(estacion["dist"], 1) if estacion else None,
        "estado_norma": estado_norma,
        "atributos": {
            "tratamiento": _sin_tecnicos(trat),
            "uso_suelo": _sin_tecnicos(uso),
            "lote": {k: v for k, v in (lote or {}).items() if k in (
                "subtipo_lote", "juridica_predio", "numero_edificacion", "relacion_superficie", "mz_igac")},
        },
        "fuente": CITA,
    }
    return {"predio": predio, "candidatos_conjunto": candidatos, "ejecuciones": ejecuciones,
            "en_cobertura": en_cobertura}
