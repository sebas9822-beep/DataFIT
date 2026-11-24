"""Las cinco reglas obligatorias del proyecto, de punta a punta, sobre el caso calculable de conftest.py.

1. CRS: distancias en metros; nunca se asume un CRS sin verificarlo.
2. Trazabilidad: nada se elimina en silencio; duplicados con puntaje y regla.
3. Precio pedido y precio de cierre por separado.
4. Flexibilización de criterios visible en la ficha.
5. Sin recomendación de inversión ni concepto jurídico.
"""
from __future__ import annotations

import json

import geopandas as gpd
import pytest
from m2.apoyo_m2 import editar_params, escribir_pot, ruta_params

from ficha import carga
from ficha.config import cargar_params
from ficha.ficha import renderizar_markdown
from ficha.pipeline import ejecutar


@pytest.fixture()
def res(proyecto):
    return ejecutar(ruta_params(proyecto), proyecto)


def _ficha(res, id_inmueble):
    return next(f for f in res["fichas"] if f["id_inmueble"] == id_inmueble)


# ------------------------------------------------------------------ caso calculado a mano

def test_caso_manual_t01(res):
    f = _ficha(res, "T-01")
    m = f["mercado"]
    assert m["estado_referencia"] == "habitable" and m["base_referencia"] == "pedido"
    assert m["precio_m2"] == {"n": 3, "mediana": 8_500_000, "promedio": 8_500_000, "minimo": 8_000_000,
                              "maximo": 9_000_000, "desviacion": 500_000, "coef_variacion": 0.0588}
    assert m["confianza"]["nivel"] == "media"
    assert [c["origen"] for c in m["comparables"]] == ["h:6", "h:1", "h:4", "h:5"]     # orden por precio/m²
    assert m["precio_m2_por_estado_en_zona"]["habitable"] == {"n": 4, "mediana": 8_250_000}
    assert f["calidad_datos_zona"] == {"anuncios_en_zona": 16, "duplicados_eliminados": 4, "anuncios_unicos": 12,
                                       "anuncios_invalidos": {"estado_sin_clasificar": 1, "precio_m2_fuera_de_rango": 1,
                                                              "sin_area": 1, "sin_precio": 1, "tipo_precio_no_aceptado": 1},
                                       "anuncios_validos": 7, "grupos_duplicados_ambiguos": 1,
                                       "validos_absorbidos_por_representante_invalido": 1}
    e = f["entorno"]
    assert e["estacion_mas_cercana"] == {"nombre": "Estadio", "sistema": "metro", "distancia_m": 111}
    assert e["estaciones_en_radio"] == 2                                                 # metroplus no cuenta
    assert e["equipamientos_en_radio"] == {"educacion": 0, "salud": 1, "parques": 1, "abastecimiento": 0,
                                           "seguridad": 0, "cultura": 0}
    assert not e["aproximado"] and f["datos_faltantes"] == ["fecha_publicacion_comparables"]


# ------------------------------------------------------------------ regla 1 · CRS y distancias

def test_distancias_en_metros_y_control_proyectado(res):
    f = _ficha(res, "T-01")
    assert f["entorno"]["metodo_distancia"] == "haversine"
    ctrl = f["trazabilidad"]["control_distancia"]
    assert ctrl["crs"] == "EPSG:9377" and ctrl["distancia_estacion_m"] == 111 and ctrl["diferencia_m"] == 0
    assert ctrl["equipamientos_que_cambian"] == 0


def test_crs_de_cada_capa_queda_registrado(res, proyecto):
    capas = {c["capa"]: c for c in res["crs"]}
    assert capas["transporte"]["crs_usado"] == "EPSG:4326" and "RFC 7946" in capas["transporte"]["crs_declarado"]
    assert capas["pot"]["estado"] == "pendiente" and not any(c["crs_asumido"] for c in capas.values())
    assert (proyecto / "auditoria" / "crs_capas.csv").exists()
    assert (proyecto / "auditoria" / "control_distancias.csv").exists()


def test_capa_sin_crs_detiene_el_proceso(proyecto, monkeypatch):
    p = cargar_params(ruta_params(proyecto), proyecto)
    original = gpd.read_file
    monkeypatch.setattr(gpd, "read_file", lambda ruta, **k: original(ruta, **k).set_crs(None, allow_override=True))
    with pytest.raises(ValueError, match="no tiene CRS"):
        carga.leer_capa("transporte", p["rutas"]["transporte_geojson"], p)
    p["crs"]["asumir_entrada_si_falta"] = True
    _, registro = carga.leer_capa("transporte", p["rutas"]["transporte_geojson"], p)
    assert registro["crs_asumido"]


def test_capa_con_crs_mal_declarado_detiene_el_proceso(proyecto):
    p = cargar_params(ruta_params(proyecto), proyecto)
    ruta = p["rutas"]["transporte_geojson"]
    ruta.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"nombre": "X", "sistema": "metro"},
        "geometry": {"type": "Point", "coordinates": [100.0, 45.0]}}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="fuera del área de estudio"):
        carga.leer_capa("transporte", ruta, p)


def test_norma_con_capa_pot_y_sus_cuatro_estados(proyecto):
    assert {f["id_inmueble"]: f["norma"]["estado"] for f in ejecutar(ruta_params(proyecto), proyecto)["fichas"]} == \
        {"T-01": "pendiente_capa", "T-02": "pendiente_capa", "T-03": "pendiente_capa", "T-04": "no_disponible"}
    escribir_pot(proyecto)
    fichas = {f["id_inmueble"]: f for f in ejecutar(ruta_params(proyecto), proyecto)["fichas"]}
    assert fichas["T-01"]["norma"]["estado"] == "encontrado" and fichas["T-01"]["norma"]["tratamiento_pot"] == "CN2"
    assert fichas["T-03"]["norma"]["estado"] == "fuera_de_capa" and fichas["T-04"]["norma"]["estado"] == "no_disponible"
    assert "data/geo/pot_tratamientos.geojson" in fichas["T-01"]["metadatos"]["fuentes"]


def test_campo_del_pot_sin_distinguir_mayusculas_y_atributos(proyecto):
    # La capa real usa 'tratamiento' en minúscula; parametros.yaml dice TRATAMIENTO.
    escribir_pot(proyecto, {"tratamiento": "Consolidación Nivel 2", "codigo_tratamiento": "Z3_CN2_12",
                            "clase_suelo": "Urbano"})
    n = next(f for f in ejecutar(ruta_params(proyecto), proyecto)["fichas"] if f["id_inmueble"] == "T-01")["norma"]
    assert n["tratamiento_pot"] == "Consolidación Nivel 2" and n["campo_capa"] == "tratamiento"
    assert n["atributos"] == {"codigo_tratamiento": "Z3_CN2_12", "clase_suelo": "Urbano"}


def test_capa_pot_sin_el_campo_de_tratamiento_detiene_el_proceso(proyecto):
    escribir_pot(proyecto, {"otro": "x"})
    with pytest.raises(ValueError, match="no tiene el campo"):
        ejecutar(ruta_params(proyecto), proyecto)


def test_capa_pot_en_magna_sirgas_se_reproyecta_antes_del_cruce(proyecto):
    ruta = escribir_pot(proyecto)
    gpd.read_file(ruta).to_crs("EPSG:9377").to_file(ruta, driver="GeoJSON")
    fichas = {f["id_inmueble"]: f for f in ejecutar(ruta_params(proyecto), proyecto)["fichas"]}
    assert fichas["T-01"]["norma"]["tratamiento_pot"] == "CN2" and fichas["T-03"]["norma"]["estado"] == "fuera_de_capa"


# ------------------------------------------------------------------ regla 2 · trazabilidad

def test_ningun_registro_se_pierde(res):
    a, r = res["anuncios"], res["resumen"]
    assert r == {"anuncios_entrada": 17, "duplicados": 4, "invalidos": 5, "validos": 8}
    assert len(a) == 17 and (a.loc[a["es_duplicado"] | a["motivo_invalido"].notna(), "motivo_exclusion"] != "").all()


def test_exclusiones_listan_anuncios_duplicados_e_inmuebles(res, proyecto):
    conteo = res["exclusiones"]["motivo_exclusion"].value_counts().to_dict()
    assert conteo == {"duplicado": 4, "sin_precio": 1, "sin_area": 1, "estado_sin_clasificar": 1,
                      "precio_m2_fuera_de_rango": 1, "tipo_precio_no_aceptado": 1, "validacion_esquema": 1}
    inmueble = res["exclusiones"][res["exclusiones"]["entidad"] == "inmueble"].iloc[0]
    assert inmueble["id"] == "T-05" and "lat" in inmueble["detalle"]
    assert "T-05" not in {f["id_inmueble"] for f in res["fichas"]}
    assert (proyecto / "auditoria" / "exclusiones.csv").exists()


def test_duplicados_conservan_regla_puntaje_y_fila_de_coincidencia(res, proyecto):
    pares = res["duplicados"].set_index("origen_duplicado")
    assert pares.loc["h:2", "regla_aplicada"] == "R2_claves_sin_id" and pares.loc["h:2", "coincide_con"] == "h:1"
    assert pares.loc["h:3", "regla_aplicada"] == "R1_id_anuncio" and pares.loc["h:3", "origen_representante"] == "h:1"
    assert pares.loc["h:16", "grupo_ambiguo"] and (pares["puntaje"] == 1.0).all()
    assert pares.loc["h:17", "origen_representante"] == "h:9"
    assert (proyecto / "auditoria" / "duplicados.csv").exists()
    assert (proyecto / "auditoria" / "duplicados_revision.csv").exists()


def test_ficha_declara_fuentes_parametros_y_regla_de_duplicados(res):
    f = _ficha(res, "T-01")
    assert f["metadatos"]["version_parametros"] == "1.0" and f["metadatos"]["fecha_capas_osm"] == "2026-11-13T23:03:06Z"
    t = f["trazabilidad"]
    assert len(t["hash_parametros"]) == 64 and t["regla_deduplicacion"]["representante"] == "primera_fila"
    assert {k for k, v in t["archivos"].items() if v["sha256"]} == {"anuncios", "inmuebles", "transporte", "equipamientos"}
    assert t["archivos"]["pot"]["sha256"] is None


def test_grupo_propio_excluido_queda_registrado(res):
    criterio = _ficha(res, "T-01")["mercado"]["criterio_comparables"]
    assert criterio["grupos_propios_excluidos"] == ["h:12"] and criterio["rango_area_m2"] == [70.0, 130.0]


# ------------------------------------------------------------------ regla 3 · pedido y cierre

def test_pedido_y_cierre_por_separado(res):
    m = _ficha(res, "T-01")["mercado"]
    assert m["precio_m2_por_tipo"]["pedido"]["n"] == 3 and m["precio_m2_por_tipo"]["cierre"]["n"] == 1
    assert m["precio_m2_por_tipo"]["cierre"]["mediana"] == 7_500_000 and not m["mezcla_pedido_cierre"]
    cierre = next(c for c in m["comparables"] if c["tipo_precio"] == "cierre")
    assert not cierre["en_referencia"]                       # se lista, pero no entra a la estadística de pedido


def test_referencia_de_cierre_cuando_alcanza_el_minimo(proyecto):
    editar_params(proyecto, lambda p: p["precios"].update(min_comparables_cierre=1))
    m = _ficha(ejecutar(ruta_params(proyecto), proyecto), "T-01")["mercado"]
    assert m["base_referencia"] == "cierre" and m["precio_m2"]["n"] == 1 and m["precio_m2"]["mediana"] == 7_500_000


def test_mezcla_solo_con_parametro_explicito_y_queda_marcada(proyecto):
    editar_params(proyecto, lambda p: p["precios"].update(permitir_mezcla_pedido_cierre=True))
    m = _ficha(ejecutar(ruta_params(proyecto), proyecto), "T-01")["mercado"]
    assert m["base_referencia"] == "pedido+cierre" and m["mezcla_pedido_cierre"] and m["precio_m2"]["n"] == 4


# ------------------------------------------------------------------ regla 4 · flexibilización

def test_sin_flexibilizacion_por_defecto(res):
    m = _ficha(res, "T-02")["mercado"]
    assert not m["criterio_comparables"]["aplicada"] and not m["criterio_comparables"]["activa"]
    assert m["precio_m2"]["n"] == 2 and m["confianza"]["nivel"] == "baja"


def test_flexibilizacion_activada_queda_visible(proyecto):
    editar_params(proyecto, lambda p: p["comparables"]["flexibilizacion"].update(activa=True))
    f = _ficha(ejecutar(ruta_params(proyecto), proyecto), "T-02")
    c = f["mercado"]["criterio_comparables"]
    # ±30 % de 150 m² = [105, 195]: 2 comparables. Con ±40 % = [90, 210]: 6.
    assert c["aplicada"] and c["n_base"] == 2 and c["n_final"] == 6
    assert [s["nombre"] for s in c["pasos_aplicados"]] == ["tolerancia_area_40"] and c["rango_area_m2"] == [90.0, 210.0]
    assert "Criterios flexibilizados: sí, tolerancia_area_40" in renderizar_markdown(f)


# ------------------------------------------------------------------ regla 5 · alcance

PALABRAS_DE_RECOMENDACION = ("se recomienda", "recomendamos", "conviene comprar", "oportunidad de compra",
                             "comprar", "invertir", "rentabilidad esperada", "viable jurídicamente")


def test_ficha_sin_lenguaje_de_recomendacion(res):
    for f in res["fichas"]:
        cuerpo = {k: v for k, v in f.items() if k != "aviso"}
        cuerpo["mercado"] = {k: v for k, v in f["mercado"].items() if k != "confianza"}
        texto = json.dumps(cuerpo, ensure_ascii=False).lower()
        assert not [w for w in PALABRAS_DE_RECOMENDACION if w in texto]
        assert "valor_indicativo" not in f["mercado"]
        assert "No constituye" in f["aviso"] and "No mide rentabilidad" in f["mercado"]["confianza"]["alcance"]
        assert "No constituye concepto jurídico" in f["norma"]["nota"]


# ------------------------------------------------------------------ reproducibilidad y salidas

def test_mismos_datos_y_parametros_dan_la_misma_ficha(proyecto):
    ejecutar(ruta_params(proyecto), proyecto)
    fichas = sorted((proyecto / "salidas" / "fichas").glob("*.json"))
    primera = {f.name: f.read_bytes() for f in fichas}
    ejecutar(ruta_params(proyecto), proyecto)
    assert primera and all(f.read_bytes() == primera[f.name] for f in fichas)


def test_salidas_generadas(res, proyecto):
    for ruta in ("salidas/fichas/T-01.json", "salidas/fichas_md/T-01.md", "salidas/resumen_fichas.csv",
                 "salidas/anuncios_procesados.csv", "auditoria/bitacora_calidad.csv", "auditoria/manifiesto_ejecucion.json"):
        assert (proyecto / ruta).exists(), ruta
    assert res["validacion"] is None                          # el proyecto temporal no trae esperado.json
