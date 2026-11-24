"""Pruebas unitarias de limpieza, deduplicación, estadísticas, confianza y distancia (reglas oficiales)."""
import pandas as pd
import pytest

from ficha.confianza import calificar
from ficha.config import cargar_params
from ficha.deduplicacion import deduplicar
from ficha.entorno import haversine_m
from ficha.limpieza import a_numero, a_positivo, limpiar_anuncios, normalizar_estado
from ficha.mercado import estadisticas


@pytest.fixture(scope="module")
def p():
    return cargar_params()


COLS = ["id_anuncio", "fuente", "fecha", "zona", "municipio", "barrio", "area_m2", "precio", "habitaciones",
        "banos", "parqueadero", "estado", "tipo_precio", "origen"]


def _df(*filas):
    base = {"id_anuncio": "", "fuente": "fincaraiz", "fecha": "", "zona": "z", "municipio": "medellin", "barrio": "b",
            "area_m2": "70", "precio": "350000000", "habitaciones": "3", "banos": "2", "parqueadero": "1",
            "estado": "REMODELADO", "tipo_precio": "pedido", "origen": "x"}
    df = pd.DataFrame([{**base, **f} for f in filas], columns=COLS).astype(str)
    df.insert(0, "fila_origen", range(2, len(df) + 2))
    return df


def _procesar(df, p):
    limpio, bitacora = limpiar_anuncios(df, p)
    out, pares, revision = deduplicar(limpio, p)
    return out, pares, bitacora


# ------------------------------------------------------------------ normalización

@pytest.mark.parametrize("texto,esperado", [
    ("REMODELADA", "remodelado"), (" Remodelada ? ", "remodelado"), ("REMODELADA ?", "remodelado"),
    ("HABITABLE POR REMODELAR", "habitable"), ("Ok", "habitable"), ("VIVIR", "habitable"),
    ("SIN REMODELAR (OBRA NEGRA)", "por_remodelar"), ("Reformar", "por_remodelar"),
    ("", "sin_clasificar"), ("lujo", "sin_clasificar"),
])
def test_normalizar_estado_oficial(p, texto, esperado):
    assert normalizar_estado(texto, p["reglas"]["estados"]) == esperado


@pytest.mark.parametrize("entrada,esperado", [
    ("$ 450.000.000", 450_000_000), ("85,5 m2", 85.5), ("1.200", 1200), ("70.4", 70.4), ("50.34", 50.34),
])
def test_a_numero(entrada, esperado):
    assert a_numero(entrada) == esperado


def test_cero_o_vacio_no_es_un_area(p):
    assert pd.isna(a_positivo("0")) and pd.isna(a_positivo("")) and pd.isna(a_positivo("?"))


# ------------------------------------------------------------------ validez (orden oficial de motivos)

def test_motivos_de_invalidez_en_orden_oficial(p):
    out, _, _ = _procesar(_df(
        {"origen": "a", "area_m2": ""}, {"origen": "b", "precio": ""}, {"origen": "c", "precio": "35000000"},
        {"origen": "d", "estado": ""}, {"origen": "e", "tipo_precio": "arriendo", "precio": "360000000"},
        {"origen": "f", "area_m2": "", "estado": ""}, {"origen": "g", "precio": "370000000"}), p)
    motivos = dict(zip(out["origen"], out["motivo_invalido"], strict=True))
    assert motivos == {"a": "sin_area", "b": "sin_precio", "c": "precio_m2_fuera_de_rango",
                       "d": "estado_sin_clasificar", "e": "tipo_precio_no_aceptado", "f": "sin_area", "g": None}


def test_bitacora_distingue_faltante_y_valor_no_reconocido(p):
    _, _, bit = _procesar(_df({"origen": "a", "banos": "", "parqueadero": "?", "estado": "lujo"}), p)
    eventos = {(e.campo, e.evento, e.detalle) for e in bit.itertuples()}
    assert ("banos", "faltante", "") in eventos
    assert ("parqueadero", "valor_no_reconocido", "?") in eventos
    assert ("estado", "valor_no_reconocido", "lujo") in eventos


# ------------------------------------------------------------------ deduplicación oficial

def test_mismo_id_es_duplicado_y_se_conserva_la_primera_fila(p):
    out, pares, _ = _procesar(_df({"id_anuncio": "fr:1", "origen": "a"}, {"id_anuncio": "fr:1", "origen": "b", "area_m2": "71"}), p)
    assert out.set_index("origen").loc["b", "es_duplicado"] and not out.set_index("origen").loc["a", "es_duplicado"]
    assert pares.iloc[0]["regla_aplicada"] == "R1_id_anuncio" and pares.iloc[0]["origen_representante"] == "a"


def test_misma_zona_area_y_precio_es_duplicado_aunque_cambie_el_formato(p):
    out, pares, _ = _procesar(_df({"id_anuncio": "fr:1", "origen": "a"},
                                  {"fuente": "facebook", "area_m2": "70.0", "origen": "b"}), p)
    assert int(out["es_duplicado"].sum()) == 1
    assert pares.iloc[0]["regla_aplicada"] == "R2_claves_sin_id" and pares.iloc[0]["puntaje"] == 1.0


def test_otra_zona_no_es_duplicado(p):
    out, _, _ = _procesar(_df({"origen": "a"}, {"zona": "otra", "origen": "b"}), p)
    assert not out["es_duplicado"].any()


def test_fila_sin_area_solo_se_deduplica_por_id(p):
    out, _, _ = _procesar(_df({"area_m2": "", "origen": "a"}, {"area_m2": "", "origen": "b"}), p)
    assert not out["es_duplicado"].any()


def test_grupo_con_ids_distintos_se_marca_ambiguo(p):
    out, pares, _ = _procesar(_df({"id_anuncio": "fr:1", "origen": "a"}, {"id_anuncio": "fr:2", "origen": "b"}), p)
    assert out["grupo_ambiguo"].all() and pares.iloc[0]["grupo_ambiguo"]


def test_representante_mas_completo_es_alternativa_explicita(p):
    import copy
    p2 = copy.deepcopy(p)
    p2["deduplicacion"]["representante"] = "mas_completo"
    df = _df({"origen": "a", "estado": ""}, {"origen": "b", "estado": "OK"})
    oficial, _, _ = _procesar(df, p)
    alterno, _, _ = _procesar(df, p2)
    assert oficial.set_index("origen").loc["b", "es_duplicado"]          # regla oficial: gana la primera fila
    assert alterno.set_index("origen").loc["a", "es_duplicado"]          # alternativa: gana la fila válida
    assert alterno.set_index("origen").loc["a", "regla_dup"] == "reasignado_por_completitud"


# ------------------------------------------------------------------ estadísticas, confianza, distancia

def test_estadisticas_valores_conocidos():
    e = estadisticas([4e6, 5e6, 6e6, 9e6])
    assert (e["n"], e["mediana"], e["promedio"], e["minimo"], e["maximo"]) == (4, 5_500_000, 6_000_000, 4_000_000, 9_000_000)
    assert e["desviacion"] == 2_160_247 and e["coef_variacion"] == pytest.approx(0.3600, abs=1e-4)   # muestral, n − 1


def test_un_solo_valor_no_tiene_dispersion():
    e = estadisticas([5e6])
    assert e["desviacion"] is None and e["coef_variacion"] is None
    assert estadisticas([])["n"] == 0 and estadisticas([])["mediana"] is None


@pytest.mark.parametrize("n,cv,nivel", [(5, 0.15, "alta"), (5, 0.16, "media"), (3, 0.25, "media"), (2, 0.05, "baja"),
                                        (10, 0.30, "baja"), (1, None, "baja")])
def test_regla_de_confianza(p, n, cv, nivel):
    assert calificar(n, cv, p["reglas"]["confianza"])["nivel"] == nivel


def test_motivo_de_confianza_en_formato_oficial(p):
    c = calificar(9, 0.2357, p["reglas"]["confianza"])
    assert c["motivo"] == "9 comparables (mínimo 3) y coeficiente de variación 23.57% (máximo 25%)"


def test_haversine_un_grado_de_latitud(p):
    assert haversine_m(6.0, -75.0, 7.0, -75.0, p["entorno"]["radio_tierra_m"]) == pytest.approx(111_195, abs=5)
