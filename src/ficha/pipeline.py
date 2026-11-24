"""Orquestador: ingesta -> validación -> limpieza -> deduplicación -> mercado + entorno + norma -> ficha
-> validación contra el cálculo manual.

Uso:
    python -m ficha.pipeline                      # con config/params.yaml
    python -m ficha.pipeline --params otro.yaml   # otro archivo de parámetros técnicos
    python -m ficha.pipeline --raiz C:\\ruta       # otra raíz de proyecto (datos y salidas)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import __version__, carga, deduplicacion, entorno, limpieza, validacion
from .config import cargar_params, hash_archivo
from .ficha import construir_ficha, renderizar_markdown
from .limpieza import normalizar_estado

log = logging.getLogger("ficha")

ENTRADAS = {"anuncios": "anuncios_csv", "inmuebles": "inmuebles_csv", "transporte": "transporte_geojson",
            "equipamientos": "equipamientos_geojson", "pot": "pot_geojson"}


def _json_default(o):
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()
    return str(o)


def _escribir_json(ruta: Path, contenido) -> None:
    with open(ruta, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(contenido, fh, ensure_ascii=False, indent=2, default=_json_default)
        fh.write("\n")


def _escribir_csv(df: pd.DataFrame, ruta: Path) -> None:
    df.to_csv(ruta, index=False, encoding="utf-8", lineterminator="\n")


def ejecutar(params_path: str | Path | None = None, raiz: str | Path | None = None) -> dict:
    p = cargar_params(params_path, raiz)
    r, rel = p["rutas"], p["_meta"]["rutas_relativas"]
    salida, auditoria = r["salida_dir"], r["auditoria_dir"]
    (salida / "fichas").mkdir(parents=True, exist_ok=True)
    auditoria.mkdir(parents=True, exist_ok=True)

    # 1. Ingesta y verificación de CRS
    anuncios_raw = carga.leer_anuncios(r["anuncios_csv"], p)
    validacion.validar_anuncios_crudos(anuncios_raw)
    inmuebles_raw = carga.leer_inmuebles(r["inmuebles_csv"], p)
    transporte, reg_t = carga.leer_capa("transporte", r["transporte_geojson"], p)
    equipamientos, reg_e = carga.leer_capa("equipamientos", r["equipamientos_geojson"], p)
    pot, reg_p = carga.leer_capa("pot", r["pot_geojson"], p, obligatoria=False)
    registro_crs = [reg_t, reg_e, reg_p, {
        "capa": "inmuebles", "archivo": r["inmuebles_csv"].name, "crs_calculo": p["crs"]["metrico"], "estado": "cargada",
        "crs_declarado": "CSV lat/lon (params: crs.entrada)", "crs_usado": p["crs"]["entrada"], "crs_asumido": False,
        "registros": int(len(inmuebles_raw)), "geometrias": "Point", "limites_wgs84": None, "dentro_area_estudio": None}]
    log.info("Anuncios: %d | Inmuebles: %d | Estaciones: %d | Equipamientos: %d | POT: %s", len(anuncios_raw),
             len(inmuebles_raw), len(transporte), len(equipamientos), reg_p["estado"])

    # 2. Limpieza, deduplicación y validación
    inmuebles, exclusiones_inm = validacion.validar_inmuebles(inmuebles_raw, p)
    anuncios, bitacora = limpieza.limpiar_anuncios(anuncios_raw, p)
    anuncios, pares, revision = deduplicacion.deduplicar(anuncios, p)
    anuncios["motivo_exclusion"] = validacion.motivo_exclusion(anuncios)
    validacion.verificar_invariantes(anuncios)
    resumen = {
        "anuncios_entrada": int(len(anuncios)),
        "duplicados": int(anuncios["es_duplicado"].sum()),
        "invalidos": int((~anuncios["es_duplicado"] & anuncios["motivo_invalido"].notna()).sum()),
        "validos": int((anuncios["motivo_exclusion"] == "").sum()),
    }
    if resumen["anuncios_entrada"] != resumen["duplicados"] + resumen["invalidos"] + resumen["validos"]:
        raise RuntimeError(f"Conteo de anuncios inconsistente: {resumen}")
    log.info("Resumen limpieza: %s", resumen)

    # 3. Fichas
    inmuebles = inmuebles.assign(estado_canonico=inmuebles["estado"].map(
        lambda v: normalizar_estado(v, p["reglas"]["estados"], p["limpieza"]["estado_sin_mapeo"])))
    capas = entorno.preparar_capas(transporte, equipamientos, p)
    contexto = {
        "fecha_generacion": str(p["ejecucion"]["fecha_generacion"] or date.today().isoformat()),
        "fecha_capas_osm": carga.leer_metadata_geojson(r["transporte_geojson"]).get("fecha_osm"),
        "archivos": {k: {"archivo": Path(rel[v]).as_posix(), "sha256": hash_archivo(r[v])} for k, v in ENTRADAS.items()},
    }
    fichas = []
    if p["ficha"]["generar_markdown"]:
        (salida / "fichas_md").mkdir(parents=True, exist_ok=True)
    for _, inm in inmuebles.iterrows():
        f = construir_ficha(inm, anuncios, capas, pot, p, contexto)
        fichas.append(f)
        _escribir_json(salida / "fichas" / f"{f['id_inmueble']}.json", f)
        if p["ficha"]["generar_markdown"]:
            (salida / "fichas_md" / f"{f['id_inmueble']}.md").write_text(renderizar_markdown(f), encoding="utf-8",
                                                                          newline="\n")

    tabla = pd.DataFrame([{
        "id_inmueble": f["id_inmueble"], "zona": f["inmueble"]["zona"], "estado": f["inmueble"]["estado"],
        "area_m2": f["inmueble"]["area_m2"], "n": f["mercado"]["precio_m2"]["n"],
        "mediana_m2": f["mercado"]["precio_m2"]["mediana"], "cv": f["mercado"]["precio_m2"]["coef_variacion"],
        "confianza": f["mercado"]["confianza"]["nivel"], "base": f["mercado"]["base_referencia"],
        "estacion": (f["entorno"]["estacion_mas_cercana"] or {}).get("nombre"),
        "distancia_m": (f["entorno"]["estacion_mas_cercana"] or {}).get("distancia_m"),
        "equip_800m": sum(f["entorno"]["equipamientos_en_radio"].values()),
        "aproximado": f["entorno"]["aproximado"], "norma": f["norma"]["estado"],
        "faltantes": "; ".join(f["datos_faltantes"]),
    } for f in fichas])
    control = pd.DataFrame([{"id_inmueble": f["id_inmueble"], "metodo_ficha": f["entorno"]["metodo_distancia"],
                             "estacion": (f["entorno"]["estacion_mas_cercana"] or {}).get("nombre"),
                             "distancia_ficha_m": (f["entorno"]["estacion_mas_cercana"] or {}).get("distancia_m"),
                             **{f"control_{k}": v for k, v in (f["trazabilidad"]["control_distancia"] or {}).items()}}
                            for f in fichas])

    exclusiones = validacion.tabla_exclusiones(anuncios, exclusiones_inm)
    _escribir_csv(tabla, salida / "resumen_fichas.csv")
    _escribir_csv(anuncios, salida / "anuncios_procesados.csv")
    _escribir_csv(bitacora, auditoria / "bitacora_calidad.csv")
    _escribir_csv(exclusiones, auditoria / "exclusiones.csv")
    _escribir_csv(pares, auditoria / "duplicados.csv")
    _escribir_csv(revision, auditoria / "duplicados_revision.csv")
    _escribir_csv(pd.DataFrame(registro_crs), auditoria / "crs_capas.csv")
    _escribir_csv(control, auditoria / "control_distancias.csv")

    # 4. Validación contra el cálculo manual (validador oficial)
    val = None
    if r["esperado_json"].exists() and r["validador_py"].exists():
        val = validacion.validar_contra_esperado(fichas, r["esperado_json"], r["validador_py"])
        _escribir_csv(val, salida / "validacion.csv")
        log.info("Validación contra el cálculo manual: %d/%d inmuebles coinciden", int(val["ok"].sum()), len(val))

    _escribir_json(auditoria / "manifiesto_ejecucion.json", {
        "ejecutado": datetime.now().isoformat(timespec="seconds"),
        "version_pipeline": __version__,
        "version_parametros": p["_meta"]["version_parametros"],
        "hash_parametros": p["_meta"]["hash_params"],
        "metodo_distancia": p["entorno"]["metodo_distancia"],
        "representante_duplicados": p["deduplicacion"]["representante"],
        "archivos": contexto["archivos"],
        "resumen_anuncios": resumen,
        "inmuebles": {"entrada": int(len(inmuebles_raw)), "excluidos": int(len(exclusiones_inm)),
                      "fichas_generadas": [f["id_inmueble"] for f in fichas]},
        "parejas_duplicadas": int(len(pares)), "candidatos_revision": int(len(revision)),
        "validacion_manual": None if val is None else {"inmuebles": int(len(val)), "ok": int(val["ok"].sum())},
    })
    return {"params": p, "resumen": resumen, "fichas": fichas, "tabla": tabla, "validacion": val,
            "exclusiones": exclusiones, "duplicados": pares, "revision": revision, "crs": registro_crs,
            "anuncios": anuncios, "bitacora": bitacora, "control": control}


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Genera las fichas de mercado y entorno.")
    ap.add_argument("--params", default=None, help="ruta de params.yaml (por defecto config/params.yaml)")
    ap.add_argument("--raiz", default=None, help="raíz del proyecto para datos y salidas")
    args = ap.parse_args()
    res = ejecutar(args.params, args.raiz)
    print(res["tabla"].to_string(index=False))
    if res["validacion"] is not None:
        print()
        print(res["validacion"].to_string(index=False))


if __name__ == "__main__":
    main()
