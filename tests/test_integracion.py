"""Aplicación única: ETL → esquema estrella → ficha del inmueble → roles y supervisión.

1. El ETL carga la muestra oficial y cuadra con el resultado del reto (117 = 63 válidos + 33 duplicados + 21 inválidos).
2. El motor de ficha, leyendo SOLO del almacén, pasa el validador oficial (3/3), coincide con la
   implementación de referencia del reto y con las 11 fichas del Momento 2 (v0.3.0), campo por campo.
3. Una cotización hecha por la API termina en una ficha Markdown guardada en la carpeta del cliente
   (su NIT o cédula), con la trazabilidad del comercial y del cliente, y en los hechos del almacén.
4. Cada comercial ve solo lo suyo; el supervisor ve el consolidado, fija metas y registra seguimiento.
5. La base de la fase 1 se conserva: las columnas nuevas se agregan sin tocar los datos.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest
from test_api import CLIENTES, entrar, esperar, pedir, servidor  # noqa: F401 (servidor es fixture)
from test_reglas_duras import PROHIBIDO

from almacen import esquema, etl
from captura import acceso, api, bd, clientes
from captura.curaduria import claude_cliente
from captura.fuentes import portales
from ficha import desde_almacen
from ficha import servicio as fichas
from gestion import metas, seguimiento, tablero

RAIZ = Path(__file__).resolve().parents[1]
INMUEBLE = {"tipo": "apartamento", "area_m2": 80, "estado": "habitable", "habitaciones": 3, "banos": 2, "parqueaderos": 1}
SUPERVISOR = {"usuario": "supervisor1", "nombre": "Supervisor 1", "rol": "supervisor", "supervisa": "todos"}

# Lo que devolvería Claude Code al leer las inmobiliarias: 3 apartamentos útiles (7,0 · 7,5 · 8,0 M/m²), uno de un
# tipo que el cliente no pidió, uno de un sitio que no está en la lista y uno sin precio.
ANUNCIOS_RPA = [
    {"portal": "fincaraiz.com.co", "url": "https://www.fincaraiz.com.co/apartamento-en-venta/101", "titulo": "Apto 1",
     "tipo_inmueble": "apartamento", "precio_cop": 560000000, "area_m2": 80, "habitaciones": 3, "banos": 2,
     "barrio": "Belén", "fecha_publicacion": None},
    {"portal": "fincaraiz.com.co", "url": "https://www.fincaraiz.com.co/apartamento-en-venta/102", "titulo": "Apto 2",
     "tipo_inmueble": "apartamento", "precio_cop": 600000000, "area_m2": 80, "barrio": "Belén", "fecha_publicacion": None},
    {"portal": "metrocuadrado.com", "url": "https://www.metrocuadrado.com/inmueble/venta-apartamento/103", "titulo": "Apto 3",
     "tipo_inmueble": "apartamento", "precio_cop": 640000000, "area_m2": 80, "barrio": "Belén", "fecha_publicacion": None},
    {"portal": "fincaraiz.com.co", "url": "https://www.fincaraiz.com.co/bodega-en-venta/104", "titulo": "Bodega",
     "tipo_inmueble": "bodega", "precio_cop": 900000000, "area_m2": 300, "fecha_publicacion": None},
    {"portal": "otrositio.com", "url": "https://www.otrositio.com/apartamento/105", "titulo": "Fuera de la lista",
     "tipo_inmueble": "apartamento", "precio_cop": 500000000, "area_m2": 80, "fecha_publicacion": None},
    {"portal": "fincaraiz.com.co", "url": "https://www.fincaraiz.com.co/apartamento-en-venta/106", "titulo": "Sin precio",
     "tipo_inmueble": "apartamento", "precio_cop": None, "area_m2": 75, "fecha_publicacion": None},
]
LLAMADAS_RPA: list[dict] = []


@pytest.fixture(autouse=True)
def claude_lee_inmobiliarias(monkeypatch):
    """Las pruebas no salen a internet: la lectura de las inmobiliarias se reemplaza por una respuesta fija."""
    LLAMADAS_RPA.clear()

    def falso(p, sistema, mensaje, esquema_json, dominios, cfg):
        LLAMADAS_RPA.append({"mensaje": mensaje, "dominios": list(dominios), "sistema": sistema})
        return {"datos": {"anuncios": ANUNCIOS_RPA, "portales": [
            {"portal": "fincaraiz.com.co", "estado": "ok", "anuncios_vistos": 4},
            {"portal": "metrocuadrado.com", "estado": "ok", "anuncios_vistos": 1}]},
            "modelo": "prueba", "turnos": 3, "denegados": []}

    monkeypatch.setattr(claude_cliente, "invocar_con_web", falso)


def _modulo(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def dw(p):
    con = esquema.conectar(p["rutas"]["almacen"])
    return con


def uno(p, sql, *args):
    con = dw(p)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def como(puerto, usuario):
    return {"Cookie": entrar(puerto, usuario, usuario)}


def cotizar(puerto, p, inmueble=INMUEBLE, cliente_id=None, cabeceras=None, exigir=True, rpa=True, **extra):
    """Cotización completa por la API con la ficha encendida. Devuelve (cotizacion_id, progreso)."""
    p["reglas"]["fichas"]["generar_al_cotizar"] = True
    p["params"]["rpa"]["activo"] = rpa
    p["reglas"]["inmueble"]["exigir_al_cotizar"] = exigir
    cuerpo = {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False, "cliente_id": cliente_id or CLIENTES[puerto], **extra}
    if inmueble is not None:
        cuerpo["inmueble"] = inmueble
    estado, r, _ = pedir(puerto, "POST", "/api/capturas", cuerpo, cabeceras=cabeceras, sin_sesion=bool(cabeceras))
    assert estado == 202, r
    for _ in range(600):
        _, prog, _ = pedir(puerto, "GET", f"/api/trabajos/{r['trabajo_id']}", cabeceras=cabeceras, sin_sesion=bool(cabeceras))
        if prog.get("terminado"):
            return r["cotizacion_id"], prog
        __import__("time").sleep(0.03)
    raise AssertionError("la cotización no terminó")


# =============================================================================================
# 1. ETL y esquema estrella
# =============================================================================================
@pytest.fixture
def almacen_reto(p):
    res = etl.ejecutar(p, "reto")
    return p, res


def test_etl_carga_la_muestra_y_cuadra_con_el_resultado_oficial(almacen_reto):
    p, res = almacen_reto
    assert res["estado"] in ("ok", "con_alertas") and res["controles"].get("falla", 0) == 0
    assert res["muestra"] == {"anuncios": 117, "duplicados": 33, "invalidos": 21, "validos": 63}
    paso = res["pasos"]["oferta_muestra_v1"]
    assert paso["leidos"] == paso["cargados"] + paso["duplicados"] + paso["rechazados"] == 117
    con = dw(p)
    # nada se borra: las 117 filas están en el hecho, cada una con su marca
    assert con.execute("SELECT COUNT(*), SUM(es_valida), SUM(es_duplicado) FROM fact_oferta").fetchone()[:] == (117, 63, 33)
    motivos = dict(con.execute("SELECT motivo_exclusion, COUNT(*) FROM fact_oferta WHERE motivo_exclusion IS NOT NULL GROUP BY 1"))
    assert motivos == {"duplicado": 33, "estado_sin_clasificar": 20, "sin_area": 1}
    # todo duplicado guarda regla, puntaje, representante y la fila con la que coincidió
    for d in con.execute("SELECT * FROM fact_oferta WHERE es_duplicado=1"):
        assert d["regla_dup"] in ("R1_id_anuncio", "R2_claves_sin_id") and d["puntaje_dup"] == 1.0
        assert d["coincide_con"] and d["id_representante"] and d["grupo_dup"]
    # pedido y cierre viven en su dimensión; la muestra es toda de precio pedido
    assert {r[0] for r in con.execute("SELECT DISTINCT tipo_precio FROM v_oferta WHERE es_valida=1")} == {"pedido"}
    # dimensiones conformadas: 3 estados oficiales + sin_clasificar; los 11 inmuebles; integridad referencial
    assert [r[0] for r in con.execute("SELECT estado FROM dim_estado_inmueble ORDER BY orden")] == \
        ["remodelado", "habitable", "por_remodelar", "sin_clasificar"]
    assert con.execute("SELECT COUNT(*) FROM dim_inmueble WHERE origen='reto'").fetchone()[0] == 11
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    # las grafías del barrio se conforman sin adivinar: 'salvadror/palmas' queda sin revisar
    assert con.execute("SELECT COUNT(DISTINCT u.barrio) FROM fact_oferta o JOIN dim_ubicacion u USING (ubicacion_key) "
                       "WHERE u.barrio_clave='elsalvador'").fetchone()[0] == 1
    assert con.execute("SELECT revisado FROM dim_ubicacion WHERE barrio_clave='salvadrorpalmas'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM etl_ajuste WHERE regla='alias_de_barrio'").fetchone()[0] > 0
    # las coordenadas 6.242 no se leen como "6 mil 242" (INM-08)
    assert con.execute("SELECT lat FROM dim_predio WHERE predio_nk='reto:INM-08'").fetchone()[0] == pytest.approx(6.242)
    con.close()


def test_etl_es_idempotente_y_conserva_las_claves(almacen_reto):
    p, _ = almacen_reto
    con = dw(p)
    antes = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in esquema.TABLAS_DIM + esquema.TABLAS_HECHOS}
    claves = dict(con.execute("SELECT oferta_nk, oferta_key FROM fact_oferta"))
    predios = dict(con.execute("SELECT predio_nk, predio_key FROM dim_predio"))
    con.close()
    res = etl.ejecutar(p, "completo", forzar_reto=True)
    assert res["controles"].get("falla", 0) == 0
    con = dw(p)
    assert {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in antes} == antes
    assert dict(con.execute("SELECT oferta_nk, oferta_key FROM fact_oferta")) == claves      # claves sustitutas estables
    assert dict(con.execute("SELECT predio_nk, predio_key FROM dim_predio")) == predios
    assert con.execute("SELECT COUNT(*) FROM etl_corrida WHERE estado IN ('ok','con_alertas')").fetchone()[0] >= 2
    con.close()
    # sin cambios en los insumos, una corrida normal no vuelve a cargarlos
    assert etl.ejecutar(p, "completo")["reto"] == {"cargado": False, "motivo": "sin_cambios"}


def test_un_error_del_etl_no_deja_el_almacen_a_medias(almacen_reto, monkeypatch):
    p, _ = almacen_reto
    antes = uno(p, "SELECT COUNT(*) FROM fact_oferta")
    monkeypatch.setattr(etl.calidad, "revisar", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falla simulada")))
    with pytest.raises(RuntimeError):
        etl.ejecutar(p, "completo", forzar_reto=True)
    assert uno(p, "SELECT COUNT(*) FROM fact_oferta") == antes
    assert uno(p, "SELECT mensaje FROM etl_corrida WHERE estado='error' ORDER BY corrida_id DESC LIMIT 1").startswith("RuntimeError")


# =============================================================================================
# 2. Momento 2 sobre el almacén: los casos oficiales
# =============================================================================================
@pytest.fixture
def fichas_reto(almacen_reto):
    p, _ = almacen_reto
    con = dw(p)
    try:
        lista = desde_almacen.fichas_reto(con, p, etl.params_ficha(p), "2026-11-15")
    finally:
        con.close()
    return [json.loads(json.dumps(f, default=str)) for f in lista]


def test_validador_oficial_3_de_3_desde_el_almacen(fichas_reto):
    validar = _modulo("validar_oficial", RAIZ / "reto_oficial" / "validacion" / "validar.py")
    esperado = json.loads((RAIZ / "reto_oficial" / "validacion" / "esperado.json").read_text(encoding="utf-8"))
    por_id = {f["id_inmueble"]: f for f in fichas_reto}
    assert sorted(esperado["inmuebles"]) == ["INM-04", "INM-06", "INM-09"]
    for id_inmueble, esp in esperado["inmuebles"].items():
        assert validar.validar(por_id[id_inmueble], esp, esperado["_tolerancias"]) == [], id_inmueble


def test_11_fichas_iguales_a_la_referencia_del_reto_y_al_momento_2(fichas_reto):
    comparador = _modulo("comparar", RAIZ / "scripts" / "comparar_con_referencia.py")
    assert [f["id_inmueble"] for f in fichas_reto] == [f"INM-{i:02d}" for i in range(1, 12)]
    referencia = comparador.fichas_referencia()                       # implementación de referencia de Venture One
    for f in fichas_reto:
        assert comparador.diferencias(referencia[f["id_inmueble"]], f) == [], f["id_inmueble"]
        m2 = json.loads((RAIZ / "validacion" / "referencia_momento2" / f"{f['id_inmueble']}.json").read_text(encoding="utf-8"))
        m2["trazabilidad"] = {k: v for k, v in m2["trazabilidad"].items() if k not in ("version_pipeline", "hash_parametros")}
        assert comparador.diferencias(m2, f) == [], f["id_inmueble"]   # incluye control EPSG:9377 y huellas de archivos
        assert f["mercado"]["base_mercado"] == "muestra_v1" and f["trazabilidad"]["almacen"]["inmueble"] == f"reto:{f['id_inmueble']}"


def test_lote_del_reto_escribe_fichas_y_valida(p, tmp_path):
    res = fichas.generar_lote_reto(p, "2026-11-15", tmp_path / "reto")
    assert len(res["fichas"]) == 11 and res["validacion"]["ok"].all()
    assert (tmp_path / "reto" / "fichas" / "INM-04.json").exists() and (tmp_path / "reto" / "fichas_md" / "INM-04.md").exists()
    otra = fichas.generar_lote_reto(p, "2026-11-15", tmp_path / "reto2")
    for f in res["fichas"]:                                          # dos corridas: fichas idénticas
        a = (tmp_path / "reto" / "fichas" / f"{f['id_inmueble']}.json").read_text(encoding="utf-8")
        b = (tmp_path / "reto2" / "fichas" / f"{f['id_inmueble']}.json").read_text(encoding="utf-8")
        quitar = lambda t: re.sub(r'"corrida_(etl|fin)": [^,\n]+', "", t)   # noqa: E731 - la corrida cambia; el resultado no
        assert quitar(a) == quitar(b)
    assert otra["corrida"]["corrida_id"] > res["corrida"]["corrida_id"]


# =============================================================================================
# 3. De la cotización a la ficha en la carpeta del cliente
# =============================================================================================
def test_cotizacion_completa_deja_la_ficha_en_la_carpeta_del_cliente(servidor, p):
    cot, prog = cotizar(servidor, p)
    assert prog["error"] is None
    pasos = {x["id"]: x for x in prog["pasos"]}
    assert pasos["inmobiliarias"]["estado"] == "ok" and "3 anuncios útiles de 6 leídos" in pasos["inmobiliarias"]["detalle"]
    assert pasos["almacen"]["estado"] == "ok" and "corrida" in pasos["almacen"]["detalle"]
    # el RPA solo recibe la zona y lo que se busca: ningún dato del cliente; y solo los sitios permitidos
    assert len(LLAMADAS_RPA) == 1 and LLAMADAS_RPA[0]["dominios"] == ["fincaraiz.com.co", "metrocuadrado.com"]
    assert "1017234567" not in LLAMADAS_RPA[0]["mensaje"] and "Quintero" not in LLAMADAS_RPA[0]["mensaje"]
    assert "apartamento" in LLAMADAS_RPA[0]["mensaje"] and "área entre 56 y 104 m²" in LLAMADAS_RPA[0]["mensaje"]
    assert pasos["ficha"]["estado"] == "ok" and "3 comparables" in pasos["ficha"]["detalle"]
    assert prog["ficha"]["estado"] == "ok" and prog["ficha"]["version"] == 1

    # el archivo: una carpeta por documento del cliente, Markdown + JSON, con su huella
    carpeta = p["rutas"]["fichas"] / "1017234567"
    md = sorted(carpeta.glob("FICHA_*_v1.md"))
    assert len(md) == 1 and md[0].with_suffix(".json").exists()
    assert md[0].name.endswith(f"_{cot[:8].upper()}_v1.md")
    texto = md[0].read_text(encoding="utf-8")
    ficha = json.loads(md[0].with_suffix(".json").read_text(encoding="utf-8"))

    # estructura definida de la ficha (docs/plantilla_ficha_inmueble.md)
    assert texto.startswith("---\nficha: ") and "\n---\n\n# Ficha del inmueble · " in texto
    assert [l for l in texto.splitlines() if l.startswith("## ")] == [
        "## 1. Inmueble", "## 2. Mercado", "## 3. Entorno (radio de 800 m)", "## 4. Norma urbanística (informativa)",
        "## 5. Calidad de los datos", "## 6. Trazabilidad"]
    # trazabilidad del comercial que asesora y del cliente que recibe
    assert 'comercial_usuario: "comercial1"' in texto and 'cliente_documento: "1017234567"' in texto
    assert "| **Comercial que asesora** | Comercial 1 (`comercial1`) |" in texto
    assert "| **Cliente** | Ana María Quintero · CC 1017234567 |" in texto
    q = ficha["cotizacion"]
    assert q["comercial"] == {"usuario": "comercial1", "nombre": "Comercial 1"}
    assert q["cliente"]["documento"] == "1017234567" and q["cliente"]["tipo_documento"] == "CC" and q["cotizacion_id"] == cot

    # mercado: los 3 anuncios vigentes de las inmobiliarias con área 56–104 m² (7,0 · 7,5 · 8,0 M/m²)
    m = ficha["mercado"]
    assert m["base_mercado"] == "portales" and m["base_referencia"] == "pedido" and m["mezcla_pedido_cierre"] is False
    assert m["precio_m2"] == {"n": 3, "mediana": 7500000, "promedio": 7500000, "minimo": 7000000, "maximo": 8000000,
                              "desviacion": 500000, "coef_variacion": 0.0667}
    assert m["confianza"]["nivel"] == "media" and "No mide rentabilidad" in m["confianza"]["alcance"]
    assert m["criterio_comparables"]["columnas_coincidentes"] == ["zona", "tipo_inmueble"]      # la base no informa estado
    assert m["criterio_comparables"]["filtros"] == {"operacion": "venta"} and m["criterio_comparables"]["rango_area_m2"] == [56.0, 104.0]
    assert all(c["tipo_precio"] == "pedido" and c["vista_el"] and c["url"].startswith("https://") for c in m["comparables"])
    assert m["radio_m"] is None and "- Criterio de comparables: barrio del predio y barrios vecinos de su comuna" in texto   # sin coordenadas
    # vigencia: 6 meses; las ofertas OIME de 2023 no son comparables y quedan como referencia histórica
    assert m["vigencia"]["max_meses_publicacion"] == 6 and m["vigencia"]["fuera_de_vigencia"] == 3
    oime = next(b for b in m["bases"] if b["base"] == "oime")
    assert oime["aplicable"] and oime["n"] == 0 and oime["vigencia"]["fuera_de_vigencia"] == 3
    assert ficha["referencia_historica"]["oime"]["n"] == 3 and ficha["referencia_historica"]["oime"]["mediana"] == 7500000
    assert "### Referencia histórica (no vigente)" in texto and "**No son comparables**" in texto
    # lo que busca el cliente
    assert ficha["busqueda_cliente"]["tipos"] == ["apartamento"] and ficha["busqueda_cliente"]["tipo_cotizado"] == "apartamento"
    assert ficha["busqueda_cliente"]["oferta_por_tipo"][0]["vigentes"] == {"n": 3, "mediana_m2": 7500000, "area_mediana_m2": 80.0}
    assert "- **Lo que busca el cliente:** Apartamento (el que se cotiza)" in texto
    assert ficha["calidad_datos_zona"]["anuncios_invalidos"] == {"area_fuera_de_rango": 1, "sin_precio": 1} or \
        sum(ficha["calidad_datos_zona"]["anuncios_invalidos"].values()) == 2
    assert {"precios_de_cierre", "estado_de_comparables"} <= set(ficha["datos_faltantes"])
    assert "no** se filtraron por estado" in texto and "valen por el día en que se vieron publicados" in texto
    otra = next(b for b in m["bases"] if b["base"] == "muestra_v1")
    assert otra["aplicable"] is False and "no pertenece a ninguna zona" in otra["motivo"]        # Bolivariana no está en la muestra
    # entorno y norma leídos del almacén
    e = ficha["entorno"]
    assert e["estacion_mas_cercana"]["nombre"] == "Estadio" and e["estacion_mas_cercana"]["sistema"] == "metro"
    assert e["estaciones_en_radio"] == 1 and e["radio_busqueda_m"] == 800 and e["aproximado"] is False
    assert set(e["equipamientos_en_radio"]) == {"educacion", "salud", "parques", "abastecimiento", "seguridad", "cultura"}
    assert ficha["norma"]["estado"] == "encontrado" and ficha["norma"]["tratamiento_pot"] == "Consolidación Nivel 4"
    assert "No constituye concepto jurídico" in ficha["norma"]["nota"]
    assert ficha["trazabilidad"]["almacen"]["inmueble"] == f"cot:{cot}" and ficha["trazabilidad"]["almacen"]["corrida_etl"]
    # sin lenguaje de recomendación en lo que recibe el cliente
    assert not PROHIBIDO.search(texto) and not PROHIBIDO.search(json.dumps(ficha, ensure_ascii=False))
    assert "No constituye evaluación ni recomendación de inversión" in texto

    # el almacén: hechos de la cotización y de la ficha con sus dimensiones
    con = dw(p)
    v = dict(con.execute("SELECT * FROM v_cotizacion WHERE cotizacion_id=?", (cot,)).fetchone())
    assert (v["comercial"], v["cliente_nombre"], v["tipo_inmueble"], v["estado_inmueble"], v["area_m2"]) == \
        ("comercial1", "Ana María Quintero", "apartamento", "habitable", 80.0)
    assert v["cuenta"] == 1 and v["tiene_ficha"] == 1 and v["confianza"] == "media" and v["n_comparables"] == 3
    assert v["barrio"] == "Bolivariana" and v["documento_mascara"] == "******4567"
    assert "1017234567" not in "\n".join(con.iterdump())          # el documento completo no sale de la base operativa
    assert con.execute("SELECT COUNT(*) FROM etl_calidad WHERE resultado='falla'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM etl_paso WHERE cuadra=0").fetchone()[0] == 0
    # arriendos y ofertas inválidas se cargan marcados; no se descartan
    assert dict(con.execute("SELECT operacion, COUNT(*) FROM v_oferta WHERE base_mercado='oime' GROUP BY 1")) == {"arriendo": 1, "venta": 5}
    assert con.execute("SELECT COUNT(*) FROM fact_oferta WHERE base_mercado='oime' AND es_valida=0 AND motivo_exclusion IS NULL").fetchone()[0] == 0
    con.close()

    # la API entrega la ficha a su dueño y la descarga con su nombre de archivo
    estado, f, _ = pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha")
    assert estado == 200 and f["archivo_integro"] and f["markdown"] == texto and f["resumen"]["n"] == 3
    estado, cuerpo, r = pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha?descargar=md")
    assert estado == 200 and cuerpo == texto and md[0].name in r.getheader("Content-Disposition")
    _, lista, _ = pedir(servidor, "GET", "/api/cotizaciones")
    assert lista["cotizaciones"][0]["ficha"]["confianza"] == "media" and lista["cotizaciones"][0]["inmueble"]["tipo"] == "apartamento"


def test_cliente_con_nit_y_carpeta_por_nit(servidor, p):
    estado, r, _ = pedir(servidor, "POST", "/api/clientes", {"documento": "900.123.456", "nombre": "Inversiones El Prado S.A.S.",
                                                            "autoriza": True, "tipo_documento": "NIT"})
    assert estado == 200 and r["cliente"]["tipo_documento"] == "NIT" and r["cliente"]["documento"] == "900123456"
    estado, r2, _ = pedir(servidor, "POST", "/api/clientes", {"documento": "12345", "nombre": "Empresa Corta", "autoriza": True,
                                                             "tipo_documento": "NIT"})
    assert estado == 422 and "NIT tiene entre 9 y 10" in r2["error"]
    assert pedir(servidor, "POST", "/api/clientes", {"documento": "123456", "nombre": "Alguien", "autoriza": True,
                                                    "tipo_documento": "PASAPORTE"})[0] == 422
    cot, prog = cotizar(servidor, p, cliente_id=r["cliente"]["cliente_id"])
    assert prog["ficha"]["estado"] == "ok"
    md = list((p["rutas"]["fichas"] / "900123456").glob("FICHA_*.md"))
    assert len(md) == 1 and "· NIT 900123456 |" in md[0].read_text(encoding="utf-8")
    assert not (p["rutas"]["fichas"] / "1017234567").exists()        # nada se guardó en la carpeta de otro cliente


def test_los_datos_del_inmueble_son_obligatorios_y_se_pueden_completar(servidor, p):
    p["reglas"]["inmueble"]["exigir_al_cotizar"] = True
    cuerpo = {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False, "cliente_id": CLIENTES[servidor]}
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", cuerpo)
    assert estado == 422 and "falta el tipo, el área" in r["error"] and "estado" not in r["error"]
    for mal, mensaje in (({**INMUEBLE, "tipo": "castillo"}, "Tipo de inmueble"), ({**INMUEBLE, "area_m2": 5}, "área del inmueble va entre"),
                         ({**INMUEBLE, "estado": "ruinoso"}, "Estado del inmueble"), ({**INMUEBLE, "banos": "dos"}, "número entero")):
        estado, r, _ = pedir(servidor, "POST", "/api/capturas", {**cuerpo, "inmueble": mal})
        assert estado == 422 and mensaje in r["error"], mal
    with bd.conectar(p["rutas"]["bd"]) as con:
        assert con.execute("SELECT COUNT(*) FROM cotizaciones").fetchone()[0] == 0      # ninguna quedó a medias

    # una cotización anterior a la aplicación única (sin datos del inmueble): ficha incompleta, y se completa después
    cot, prog = cotizar(servidor, p, inmueble=None, exigir=False)
    assert prog["ficha"]["estado"] == "incompleta" and next(x for x in prog["pasos"] if x["id"] == "ficha")["estado"] == "parcial"
    _, f1, _ = pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha")
    assert f1["version"] == 1 and "**Sin comparables calculados.**" in f1["markdown"] and "área del inmueble" in f1["markdown"]
    estado, r, _ = pedir(servidor, "POST", f"/api/cotizaciones/{cot}/inmueble", {"inmueble": {"tipo": "apartamento"}})
    assert estado == 422
    estado, r, _ = pedir(servidor, "POST", f"/api/cotizaciones/{cot}/inmueble", {"inmueble": INMUEBLE})
    assert estado == 200 and r["ficha"]["version"] == 2 and r["ficha"]["estado"] == "ok" and r["ficha"]["resumen"]["n"] == 3
    # la versión anterior se conserva (nada se borra en silencio) y solo una queda vigente
    carpeta = p["rutas"]["fichas"] / "1017234567"
    assert len(list(carpeta.glob("FICHA_*_v1.md"))) == 1 and len(list(carpeta.glob("FICHA_*_v2.md"))) == 1
    with bd.conectar(p["rutas"]["bd"]) as con:
        assert [tuple(f) for f in con.execute("SELECT version, vigente, estado FROM fichas ORDER BY version")] == \
            [(1, 0, "incompleta"), (2, 1, "ok")]
    assert uno(p, "SELECT COUNT(*) FROM fact_ficha WHERE vigente=1") == 1 and uno(p, "SELECT COUNT(*) FROM fact_ficha") == 2


def test_una_captura_sin_cerrar_no_alimenta_hechos_y_queda_con_motivo(servidor, p):
    cot, _ = cotizar(servidor, p)
    cap = clientes.cotizacion(p, cot)["captura_id"]
    del_predio = "SELECT COUNT(*) FROM {} WHERE predio_key = (SELECT predio_key FROM dim_predio WHERE captura_id=?)"
    assert uno(p, del_predio.format("fact_entorno"), cap) > 0 and uno(p, del_predio.format("fact_oferta_predio"), cap) == 11   # 6 del OIME + 5 de inmobiliarias
    with bd.conectar(p["rutas"]["bd"]) as con:
        con.execute("UPDATE capturas SET estado='en_curso' WHERE captura_id=?", (cap,))
    res = etl.ejecutar(p, "captura", [cap])
    assert res["capturas"][0]["motivo"] == "captura_sin_cerrar"
    assert uno(p, del_predio.format("fact_entorno"), cap) == 0 and uno(p, del_predio.format("fact_oferta_predio"), cap) == 0
    assert uno(p, "SELECT utilizable FROM dim_predio WHERE captura_id=?", cap) == 0
    assert uno(p, "SELECT motivo_exclusion FROM etl_rechazo WHERE corrida_id=? AND entidad='captura'", res["corrida_id"]) == "captura_sin_cerrar"
    assert uno(p, "SELECT resultado FROM etl_calidad WHERE corrida_id=? AND control='capturas_no_utilizables'", res["corrida_id"]) == "alerta"


# =============================================================================================
# 4. Roles: cada comercial ve lo suyo; el supervisor, el consolidado
# =============================================================================================
def test_un_comercial_no_ve_las_cotizaciones_de_otro(servidor, p):
    cot, prog = cotizar(servidor, p)
    cap = prog["captura_id"]
    otro = como(servidor, "comercial2")
    estado, lista, _ = pedir(servidor, "GET", "/api/cotizaciones", cabeceras=otro, sin_sesion=True)
    assert estado == 200 and lista == {"cotizaciones": [], "total": 0}
    for ruta in (f"/api/cotizaciones/{cot}", f"/api/cotizaciones/{cot}/ficha", f"/api/cotizaciones/{cot}/ficha?descargar=md",
                 f"/api/capturas/{cap}", f"/api/capturas/{cap}/exportar?tabla=entorno"):
        estado, r, _ = pedir(servidor, "GET", ruta, cabeceras=otro, sin_sesion=True)
        assert estado == 404, ruta                                   # ni siquiera confirma que exista
    assert pedir(servidor, "GET", "/api/capturas", cabeceras=otro, sin_sesion=True)[1] == {"capturas": []}
    for ruta, cuerpo in ((f"/api/cotizaciones/{cot}/inmueble", {"inmueble": INMUEBLE}), (f"/api/cotizaciones/{cot}/ficha", {}),
                         (f"/api/capturas/{cap}/conjunto", {"nombre": "X"})):
        assert pedir(servidor, "POST", ruta, cuerpo, cabeceras=otro, sin_sesion=True)[0] == 404, ruta
    # el otro comercial encuentra al cliente (es la misma persona), pero no cuántas cotizaciones le hizo su colega
    _, r, _ = pedir(servidor, "GET", "/api/clientes/1017234567", cabeceras=otro, sin_sesion=True)
    assert r["cliente"]["nombre"] == "Ana María Quintero" and r["cliente"]["cotizaciones"] == 0
    assert pedir(servidor, "GET", "/api/clientes/1017234567")[1]["cliente"]["cotizaciones"] == 1
    # lo de supervisión le está cerrado a un comercial
    for metodo, ruta, cuerpo in (("GET", "/api/tablero", None), ("GET", "/api/supervision/clientes", None), ("GET", "/api/almacen", None),
                                 ("GET", "/api/metas", None), ("POST", "/api/metas", {"comercial": "comercial2", "mes": "2026-11", "meta_cotizaciones": 1}),
                                 ("POST", "/api/seguimientos", {"comercial": "comercial2", "tipo": "alerta", "nota": "auto-nota"}),
                                 ("POST", "/api/almacen/etl", {}), ("GET", "/api/seguimientos?comercial=comercial1", None)):
        estado, r, _ = pedir(servidor, metodo, ruta, cuerpo, cabeceras=otro, sin_sesion=True)
        assert estado == 403, (ruta, r)
    # y el segundo comercial cotiza lo suyo sin tocar lo del primero
    estado, r, _ = pedir(servidor, "POST", "/api/clientes", {"documento": "43.555.666", "nombre": "Luis Restrepo", "autoriza": True},
                         cabeceras=otro, sin_sesion=True)
    cot2, prog2 = cotizar(servidor, p, cliente_id=r["cliente"]["cliente_id"], cabeceras=otro)
    assert prog2["reusada"] is True and prog2["ficha"]["estado"] == "ok"     # la captura del punto se reusa; la cotización es propia
    assert [c["cotizacion_id"] for c in pedir(servidor, "GET", "/api/cotizaciones")[1]["cotizaciones"]] == [cot]
    assert [c["cotizacion_id"] for c in pedir(servidor, "GET", "/api/cotizaciones", cabeceras=otro, sin_sesion=True)[1]["cotizaciones"]] == [cot2]
    assert (p["rutas"]["fichas"] / "43555666").is_dir() and len(list((p["rutas"]["fichas"] / "1017234567").glob("*.md"))) == 1


def test_el_supervisor_ve_el_consolidado_fija_metas_y_registra_seguimiento(servidor, p):
    cot, _ = cotizar(servidor, p)
    sup = como(servidor, "supervisor1")
    estado, s, _ = pedir(servidor, "GET", "/api/sesion", cabeceras=sup, sin_sesion=True)
    assert s == {"autenticado": True, "usuario": "supervisor1", "nombre": "Supervisor 1", "rol": "supervisor", "supervisa": "todos"}
    # no cotiza ni registra clientes
    assert pedir(servidor, "POST", "/api/clientes", {"documento": "1234567", "nombre": "Fulano", "autoriza": True}, cabeceras=sup, sin_sesion=True)[0] == 403
    assert pedir(servidor, "POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18", "cliente_id": CLIENTES[servidor], "inmueble": INMUEBLE},
                 cabeceras=sup, sin_sesion=True)[0] == 403
    # ve todas las cotizaciones y la ficha de cualquiera
    _, lista, _ = pedir(servidor, "GET", "/api/cotizaciones", cabeceras=sup, sin_sesion=True)
    assert [c["cotizacion_id"] for c in lista["cotizaciones"]] == [cot] and lista["cotizaciones"][0]["comercial"] == "comercial1"
    assert pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha", cabeceras=sup, sin_sesion=True)[0] == 200
    assert len(pedir(servidor, "GET", "/api/supervision/comerciales", cabeceras=sup, sin_sesion=True)[1]["comerciales"]) == 10
    _, cl, _ = pedir(servidor, "GET", "/api/supervision/clientes", cabeceras=sup, sin_sesion=True)
    assert cl["clientes"][0]["documento_mascara"] == "******4567" and "documento" not in cl["clientes"][0]
    assert cl["clientes"][0]["cotizaciones"] == 1 and cl["clientes"][0]["comerciales"] == ["comercial1"]

    # metas: validación, historial y avance
    mes = tablero._hoy_local(p).isoformat()[:7]
    assert pedir(servidor, "POST", "/api/metas", {"comercial": "comercial1", "mes": "noviembre", "meta_cotizaciones": 4}, cabeceras=sup, sin_sesion=True)[0] == 422
    assert pedir(servidor, "POST", "/api/metas", {"comercial": "nadie", "mes": mes, "meta_cotizaciones": 4}, cabeceras=sup, sin_sesion=True)[0] == 422
    assert pedir(servidor, "POST", "/api/metas", {"comercial": "comercial1", "mes": mes, "meta_cotizaciones": 2, "meta_clientes": 2}, cabeceras=sup, sin_sesion=True)[0] == 200
    estado, r, _ = pedir(servidor, "POST", "/api/metas", {"comercial": "comercial1", "mes": mes, "meta_cotizaciones": 4, "meta_clientes": 3}, cabeceras=sup, sin_sesion=True)
    assert estado == 200 and r["meta"]["definida_por"] == "supervisor1"
    with bd.conectar(p["rutas"]["bd"]) as con:
        assert [f[0] for f in con.execute("SELECT meta_cotizaciones FROM metas_historial ORDER BY hist_id")] == [2, 4]

    # tablero: leído del almacén (hechos de cotización, ficha y meta)
    estado, t, _ = pedir(servidor, "GET", "/api/tablero", cabeceras=sup, sin_sesion=True)
    assert estado == 200 and t["kpis"]["cotizaciones"]["valor"] == 1 and t["kpis"]["clientes_cotizados"]["valor"] == 1
    assert t["kpis"]["clientes_nuevos"]["valor"] == 1 and t["kpis"]["fichas"] == {"valor": 1, "de": 1}
    assert t["kpis"]["meta_mes"] == {"mes": mes, "meta": 4, "hechas": 1, "avance": 0.25}
    assert t["kpis"]["comerciales_activos"] == {"valor": 1, "de": 10}
    c1 = t["por_comercial"][0]
    assert (c1["usuario"], c1["cotizaciones"], c1["clientes"], c1["fichas"], c1["meta_cotizaciones"], c1["avance"]) == ("comercial1", 1, 1, 1, 4, 0.25)
    assert len(t["por_comercial"]) == 10 and all(c["cotizaciones"] == 0 for c in t["por_comercial"][1:])
    assert len(t["serie_diaria"]) == 30 and sum(d["cotizaciones"] for d in t["serie_diaria"]) == 1
    assert t["por_barrio"] == [{"etiqueta": "Bolivariana", "n": 1}] and t["por_confianza"] == [{"etiqueta": "media", "n": 1}]
    assert t["por_tipo_inmueble"] == [{"etiqueta": "apartamento", "n": 1}] and t["por_base_mercado"] == [{"etiqueta": "portales", "n": 1}]
    assert t["demanda"]["cotizaciones_con_busqueda"] == 1 and t["demanda"]["por_tipo"][0] == {
        "tipo": "apartamento", "solicitudes": 1, "cotizado": 1, "clientes": 1, "comerciales": 1, "ofertas_media": 6.0, "no_lo_pidieron": 0}
    assert "bodega" in t["demanda"]["sin_solicitudes"] and t["demanda"]["por_comercial"] == [
        {"usuario": "comercial1", "nombre": "Comercial 1", "tipo": "apartamento", "solicitudes": 1, "clientes": 1}]
    assert uno(p, "SELECT meta_cotizaciones FROM fact_meta") == 4
    assert pedir(servidor, "GET", "/api/tablero?comercial=comercial2", cabeceras=sup, sin_sesion=True)[1]["kpis"]["cotizaciones"]["valor"] == 0
    assert pedir(servidor, "GET", "/api/tablero?desde=2026-13-01", cabeceras=sup, sin_sesion=True)[0] == 422

    # seguimiento: lo crea el supervisor, lo ve el comercial en su panel, y se cierra
    assert pedir(servidor, "POST", "/api/seguimientos", {"comercial": "comercial1", "tipo": "chisme", "nota": "x y z"}, cabeceras=sup, sin_sesion=True)[0] == 422
    assert pedir(servidor, "POST", "/api/seguimientos", {"comercial": "comercial1", "tipo": "alerta", "nota": "ok", "cotizacion_id": cot}, cabeceras=sup, sin_sesion=True)[0] == 422
    estado, r, _ = pedir(servidor, "POST", "/api/seguimientos", {"comercial": "comercial1", "tipo": "compromiso", "cotizacion_id": cot,
                                                                 "nota": "Entregar la ficha al cliente", "fecha_compromiso": "2026-12-01"}, cabeceras=sup, sin_sesion=True)
    assert estado == 200 and r["seguimiento"]["estado"] == "abierto"
    sid = r["seguimiento"]["seguimiento_id"]
    _, g, _ = pedir(servidor, "GET", "/api/mi-gestion")
    assert (g["cotizaciones"], g["meta_cotizaciones"], g["avance"]) == (1, 4, 0.25)
    assert [s["nota"] for s in g["seguimiento"]] == ["Entregar la ficha al cliente"] and g["seguimiento"][0]["supervisor_nombre"] == "Supervisor 1"
    assert pedir(servidor, "POST", f"/api/seguimientos/{sid}/cerrar", {})[0] == 403                  # el comercial no lo cierra
    assert pedir(servidor, "POST", f"/api/seguimientos/{sid}/cerrar", {}, cabeceras=sup, sin_sesion=True)[0] == 200
    assert pedir(servidor, "GET", "/api/mi-gestion")[1]["seguimiento"] == []
    assert pedir(servidor, "GET", "/api/seguimientos", cabeceras=sup, sin_sesion=True)[1]["seguimientos"][0]["cerrado_por"] == "supervisor1"

    # panel de calidad del almacén y ETL completo a pedido
    estado, a, _ = pedir(servidor, "GET", "/api/almacen", cabeceras=sup, sin_sesion=True)
    assert estado == 200 and a["disponible"] and {x["tabla"] for x in a["tablas"]} == set(esquema.TABLAS_DIM + esquema.TABLAS_HECHOS)
    assert not [c for c in a["controles"] if c["resultado"] == "falla"] and all(x["cuadra"] for x in a["pasos"])
    estado, r, _ = pedir(servidor, "POST", "/api/almacen/etl", {}, cabeceras=sup, sin_sesion=True)
    assert estado == 200 and r["estado"] in ("ok", "con_alertas") and r["controles"].get("falla", 0) == 0


def test_un_supervisor_con_equipo_solo_ve_a_los_suyos(servidor, p):
    cot, _ = cotizar(servidor, p)
    p["usuarios"]["supervisores"][1]["supervisa"] = ["comercial2", "comercial3"]
    acceso.sembrar_comerciales(p)
    sup2 = como(servidor, "supervisor2")
    assert [c["usuario"] for c in pedir(servidor, "GET", "/api/supervision/comerciales", cabeceras=sup2, sin_sesion=True)[1]["comerciales"]] == \
        ["comercial2", "comercial3"]
    assert pedir(servidor, "GET", "/api/cotizaciones", cabeceras=sup2, sin_sesion=True)[1]["total"] == 0
    assert pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha", cabeceras=sup2, sin_sesion=True)[0] == 404
    assert pedir(servidor, "GET", "/api/tablero", cabeceras=sup2, sin_sesion=True)[1]["kpis"]["cotizaciones"]["valor"] == 0
    assert pedir(servidor, "GET", "/api/tablero?comercial=comercial1", cabeceras=sup2, sin_sesion=True)[0] == 422
    assert pedir(servidor, "POST", "/api/metas", {"comercial": "comercial1", "mes": "2026-11", "meta_cotizaciones": 3}, cabeceras=sup2, sin_sesion=True)[0] == 403
    assert pedir(servidor, "GET", "/api/supervision/clientes", cabeceras=sup2, sin_sesion=True)[1]["clientes"] == []
    assert pedir(servidor, "GET", "/api/tablero", cabeceras=como(servidor, "supervisor1"), sin_sesion=True)[1]["kpis"]["cotizaciones"]["valor"] == 1


def test_metas_y_seguimiento_validan_sus_datos(p):
    with pytest.raises(metas.GestionInvalida):
        metas.fijar(p, "supervisor1", "comercial1", "2026-11", None, None)
    with pytest.raises(metas.GestionInvalida):
        metas.fijar(p, "supervisor1", "comercial1", "2026-11", -1)
    with pytest.raises(metas.GestionInvalida):
        metas.fijar(p, "supervisor1", "supervisor2", "2026-11", 5)          # la meta es de un comercial
    assert metas.fijar(p, "supervisor1", "comercial1", "2026-11", "7")["meta_cotizaciones"] == 7
    with pytest.raises(metas.GestionInvalida):
        seguimiento.crear(p, "supervisor1", "comercial1", "alerta", "revisar", fecha_compromiso="mañana")
    s = seguimiento.crear(p, "supervisor1", "comercial1", "reconocimiento", "  Buen   registro de clientes  ")
    assert s["nota"] == "Buen registro de clientes"
    assert seguimiento.cerrar(p, "supervisor2", s["seguimiento_id"], ["comercial5"]) is False       # no está a su cargo
    assert seguimiento.cerrar(p, "supervisor1", s["seguimiento_id"]) is True


# =============================================================================================
# 5. La base de la fase 1 se conserva
# =============================================================================================
def test_la_base_de_la_fase_1_migra_sin_perder_datos(p, tmp_path):
    vieja = tmp_path / "fase1.db"
    c = sqlite3.connect(vieja)
    c.executescript("""
        CREATE TABLE comerciales (usuario TEXT PRIMARY KEY, nombre TEXT NOT NULL, clave_hash TEXT NOT NULL, sal TEXT NOT NULL,
          iteraciones INTEGER NOT NULL, activo INTEGER NOT NULL DEFAULT 1, creado_en TEXT NOT NULL, clave_cambiada_en TEXT);
        CREATE TABLE clientes (cliente_id INTEGER PRIMARY KEY AUTOINCREMENT, documento TEXT NOT NULL UNIQUE, nombre TEXT NOT NULL,
          autoriza_datos INTEGER NOT NULL DEFAULT 0, autorizado_en TEXT, autorizado_por TEXT, texto_autorizacion_sha256 TEXT,
          creado_por TEXT NOT NULL, creado_en TEXT NOT NULL, actualizado_en TEXT NOT NULL);
        CREATE TABLE cotizaciones (cotizacion_id TEXT PRIMARY KEY, comercial TEXT NOT NULL, cliente_id INTEGER NOT NULL,
          cliente_nombre TEXT NOT NULL, cliente_documento TEXT NOT NULL, direccion_ingresada TEXT, captura_id TEXT,
          reusada INTEGER NOT NULL DEFAULT 0, estado TEXT NOT NULL, mensaje TEXT, evidencia_json TEXT, evidencia_sha256 TEXT,
          huella_config TEXT, version_app TEXT, creada_en TEXT NOT NULL, terminada_en TEXT);
        INSERT INTO comerciales VALUES ('comercial1', 'Comercial 1', 'hash-propio', 'aa', 1000, 1, '2026-11-21T10:00:00+00:00', '2026-11-21T11:00:00+00:00');
        INSERT INTO clientes (documento, nombre, autoriza_datos, creado_por, creado_en, actualizado_en)
          VALUES ('71222333', 'Cliente Antiguo', 1, 'comercial1', '2026-11-21T10:00:00+00:00', '2026-11-21T10:00:00+00:00');
        INSERT INTO cotizaciones (cotizacion_id, comercial, cliente_id, cliente_nombre, cliente_documento, estado, creada_en)
          VALUES ('11111111-1111-1111-1111-111111111111', 'comercial1', 1, 'Cliente Antiguo', '71222333', 'error', '2026-11-21T10:05:00+00:00');
    """)
    c.commit()
    c.close()
    p["rutas"]["bd"] = vieja
    bd.iniciar(p)
    bd.iniciar(p)                                                    # arrancar dos veces no repite la migración
    c = sqlite3.connect(vieja)
    c.row_factory = sqlite3.Row
    com = c.execute("SELECT * FROM comerciales WHERE usuario='comercial1'").fetchone()
    assert com["clave_hash"] == "hash-propio" and com["rol"] == "comercial" and com["clave_cambiada_en"]   # la clave no se toca
    assert c.execute("SELECT COUNT(*) FROM comerciales WHERE rol='supervisor'").fetchone()[0] == 2
    assert dict(c.execute("SELECT nombre, tipo_documento FROM clientes").fetchone()) == {"nombre": "Cliente Antiguo", "tipo_documento": "CC"}
    q = c.execute("SELECT * FROM cotizaciones").fetchone()
    assert q["estado"] == "error" and q["inm_tipo"] is None and q["cliente_tipo_documento"] is None
    assert {"fichas", "metas", "metas_historial", "seguimientos"} <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    # y el almacén la recibe: la cotización fallida queda cargada, sin contar y sin predio
    res = etl.ejecutar(p, "completo")
    assert res["controles"].get("falla", 0) == 0
    assert uno(p, "SELECT cuenta || '|' || predio_key || '|' || estado FROM fact_cotizacion") == "0|0|error"
    assert fichas.generar_para_cotizacion(p, "11111111-1111-1111-1111-111111111111")["estado"] == "sin_captura"


def test_la_configuracion_de_la_ficha_y_la_del_formulario_no_se_contradicen(p):
    pf = etl.params_ficha(p)
    assert list(p["reglas"]["inmueble"]["etiquetas_estado"]) == list(pf["reglas"]["estados"])      # mismos estados canónicos
    assert set(p["almacen"]["preferencia"]) == set(p["almacen"]["bases_mercado"])
    assert set(p["almacen"]["categorias_ficha"].values()) == set(pf["reglas"]["entorno"]["categorias_equipamientos"])
    sistemas = set(p["almacen"]["transporte"]["lineas"].values())
    assert set(pf["reglas"]["entorno"]["sistemas_transporte"]) <= sistemas
    assert (RAIZ / "config" / "parametros.yaml").read_bytes() == (RAIZ / "reto_oficial" / "config" / "parametros.yaml").read_bytes()
    sup = p["reglas"]["supervision"]                                # cada tipo de seguimiento tiene su texto para la pantalla
    assert set(sup["etiquetas_seguimiento"]) == set(sup["tipos_seguimiento"])
    assert api.configuracion_publica(p)["supervision"]["etiquetas_seguimiento"] == sup["etiquetas_seguimiento"]


# =============================================================================================
# 7. Lo que busca el cliente, datos por tipo de inmueble, vigencia e inmobiliarias
# =============================================================================================
def test_lo_que_busca_el_cliente_llega_al_almacen_y_al_tablero(servidor, p):
    p["reglas"]["busqueda"]["exigir_al_cotizar"] = True
    cuerpo = {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False, "cliente_id": CLIENTES[servidor]}
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", {**cuerpo, "inmueble": INMUEBLE, "busca": ["castillo"]})
    assert estado == 422 and "buscado no reconocido" in r["error"]
    p["reglas"]["inmueble"]["exigir_al_cotizar"] = False
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", cuerpo)                       # ni inmueble ni búsqueda
    assert estado == 422 and "qué tipo de inmueble busca" in r["error"]

    cot, _ = cotizar(servidor, p, busca=["casa", "apartamento", "lote", "casa"])          # varios tipos; repetido no cuenta
    cot2, prog2 = cotizar(servidor, p, inmueble={"tipo": "lote", "area_m2": 300}, busca=["lote"])
    assert pedir(servidor, "GET", f"/api/cotizaciones/{cot}")[1]["cotizacion"]["busca"] == ["casa", "apartamento", "lote"]
    assert "Tipos de inmueble pedidos: casa, apartamento, lote." in LLAMADAS_RPA[0]["mensaje"]
    assert "Tipos de inmueble pedidos: lote." in LLAMADAS_RPA[1]["mensaje"]           # el RPA busca solo lo que pidió el cliente

    con = dw(p)
    filas = [tuple(r) for r in con.execute("SELECT tipo_inmueble, es_cotizado, n_tipos, origen, comercial, cliente_nombre "
                                           "FROM v_interes WHERE cotizacion_id=? ORDER BY orden", (cot,))]
    assert filas == [("casa", 0, 3, "declarado", "comercial1", "Ana María Quintero"),
                     ("apartamento", 1, 3, "declarado", "comercial1", "Ana María Quintero"),
                     ("lote", 0, 3, "declarado", "comercial1", "Ana María Quintero")]
    assert [tuple(r) for r in con.execute("SELECT tipo_inmueble, es_cotizado, n_tipos FROM v_interes WHERE cotizacion_id=?", (cot2,))] == [("lote", 1, 1)]
    assert [round(r[0], 6) for r in con.execute("SELECT SUM(peso) FROM fact_interes GROUP BY cotizacion_id")] == [1.0, 1.0]
    assert con.execute("SELECT resultado FROM etl_calidad WHERE control='busqueda_del_cliente_consistente' ORDER BY rowid DESC").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM etl_calidad WHERE resultado='falla'").fetchone()[0] == 0
    con.close()

    # la ficha dice qué busca el cliente y cuánta oferta hay de cada tipo
    ficha = json.loads((p["rutas"]["fichas"] / "1017234567" / fichas.leer(p, cot)["ruta_json"].split("/")[-1]).read_text(encoding="utf-8"))
    assert ficha["busqueda_cliente"]["tipos"] == ["casa", "apartamento", "lote"]
    por_tipo = {x["tipo"]: x for x in ficha["busqueda_cliente"]["oferta_por_tipo"]}
    assert por_tipo["apartamento"]["es_cotizado"] and por_tipo["apartamento"]["vigentes"]["n"] == 3 and por_tipo["lote"]["vigentes"]["n"] == 0
    md = fichas.leer(p, cot)["markdown"]
    assert "### Oferta por tipo de inmueble solicitado" in md and "Casa · Apartamento (el que se cotiza) · Lote" in md
    # un lote: solo área; sin anuncios vigentes de lotes la ficha lo dice
    f2 = fichas.leer(p, cot2)
    assert f2["estado"] == "sin_mercado" and "Sin comparables vigentes" in f2["markdown"] and "estado **" not in f2["markdown"]
    assert f2["ofertas"]["busca"] == ["lote"] and f2["ofertas"]["comparables"] == []

    # tablero: lo pidieron, no lo pidieron y quién
    t = pedir(servidor, "GET", "/api/tablero", cabeceras=como(servidor, "supervisor1"), sin_sesion=True)[1]["demanda"]
    d = {x["tipo"]: x for x in t["por_tipo"]}
    assert t["cotizaciones_con_busqueda"] == 2 and t["cotizaciones_sin_busqueda"] == 0
    assert (d["lote"]["solicitudes"], d["lote"]["no_lo_pidieron"], d["lote"]["cotizado"]) == (2, 0, 1)
    assert (d["casa"]["solicitudes"], d["casa"]["no_lo_pidieron"], d["casa"]["cotizado"]) == (1, 1, 0)
    assert (d["bodega"]["solicitudes"], d["bodega"]["no_lo_pidieron"]) == (0, 2) and "bodega" in t["sin_solicitudes"]
    assert t["por_tipo"][0]["tipo"] == "lote" and {(x["usuario"], x["tipo"], x["solicitudes"]) for x in t["por_comercial"]} == {
        ("comercial1", "lote", 2), ("comercial1", "casa", 1), ("comercial1", "apartamento", 1)}


def test_el_celular_del_cliente_es_obligatorio_y_no_sale_de_la_base_operativa(servidor, p):
    p["reglas"]["clientes"]["celular"]["obligatorio"] = True
    nuevo = {"documento": "52123456", "nombre": "Luis Restrepo", "autoriza": True}
    estado, r, _ = pedir(servidor, "POST", "/api/clientes", nuevo)
    assert estado == 422 and "celular" in r["error"]
    for mal in ("12345", "6041234567", "30012345678", "3001234abc"):
        assert pedir(servidor, "POST", "/api/clientes", {**nuevo, "celular": mal})[0] == 422, mal
    estado, r, _ = pedir(servidor, "POST", "/api/clientes", {**nuevo, "celular": "+57 300 123 4567"})
    assert estado == 200 and r["cliente"]["tiene_celular"] and r["cliente"]["celular_mascara"] == "******4567"
    assert "3001234567" not in json.dumps(r)                                     # a la pantalla solo va enmascarado
    # el cliente anterior (sin celular) no puede cotizar hasta completarlo
    estado, r2, _ = pedir(servidor, "POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18", "cliente_id": CLIENTES[servidor], "inmueble": INMUEBLE})
    assert estado == 422 and "celular" in r2["error"]
    assert pedir(servidor, "POST", "/api/clientes", {"documento": "1017234567", "nombre": "Ana María Quintero", "celular": "3109876543"})[0] == 200
    cot, _ = cotizar(servidor, p, cliente_id=r["cliente"]["cliente_id"])
    con = dw(p)
    volcado = "\n".join(con.iterdump())
    con.close()
    assert "3001234567" not in volcado and "3109876543" not in volcado          # el almacén no guarda celulares
    assert "3001234567" not in LLAMADAS_RPA[0]["mensaje"] and "3001234567" not in fichas.leer(p, cot)["markdown"]


def test_los_campos_del_inmueble_dependen_del_tipo(servidor, p):
    cuerpo = {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False, "cliente_id": CLIENTES[servidor]}
    for tipo in ("lote", "local", "oficina"):                                     # solo área
        estado, r, _ = pedir(servidor, "POST", "/api/capturas", {**cuerpo, "inmueble": {"tipo": tipo, "area_m2": 120, "habitaciones": 3}})
        assert estado == 422 and "no aplica para " + tipo in r["error"], tipo
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", {**cuerpo, "inmueble": {"tipo": "lote", "area_m2": 120, "estado": "habitable"}})
    assert estado == 422 and "El estado no aplica para lote" in r["error"]
    p["reglas"]["inmueble"]["exigir_al_cotizar"] = True
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", {**cuerpo, "inmueble": {"tipo": "lote"}})
    assert estado == 422 and "falta el área" in r["error"]                        # el área es obligatoria para todos
    for mal, mensaje in (([{"nombre": "Balcón"}], "nombre y valor"), ([{"nombre": "x" * 41, "valor": "sí"}], "demasiado larga"),
                         ([{"nombre": f"c{i}", "valor": "1"} for i in range(9)], "Máximo 8")):
        estado, r, _ = pedir(servidor, "POST", "/api/capturas", {**cuerpo, "inmueble": {**INMUEBLE, "extras": mal}})
        assert estado == 422 and mensaje in r["error"], mal
    with bd.conectar(p["rutas"]["bd"]) as con:
        assert con.execute("SELECT COUNT(*) FROM cotizaciones").fetchone()[0] == 0
    extras = [{"nombre": "Balcón", "valor": "Sí"}, {"nombre": "Piso", "valor": "7"}]
    cot, _ = cotizar(servidor, p, inmueble={"tipo": "apartamento", "area_m2": 80, "habitaciones": 3, "extras": extras})   # sin estado
    f = fichas.leer(p, cot)
    assert f["estado"] == "ok" and "- **Otras características declaradas:** Balcón: Sí · Piso: 7" in f["markdown"]
    assert pedir(servidor, "GET", f"/api/cotizaciones/{cot}")[1]["cotizacion"]["inmueble"]["extras"] == extras
    assert uno(p, "SELECT caracteristicas_json FROM dim_inmueble WHERE inmueble_nk=?", f"cot:{cot}") == json.dumps(extras, ensure_ascii=False)


def test_la_vigencia_deja_fuera_lo_publicado_hace_mas_de_seis_meses():
    import pandas as pd
    marco = pd.DataFrame({
        "origen": ["a", "b", "c", "d", "e"], "motivo_invalido": [None, None, None, None, "sin_precio"],
        "fecha": pd.to_datetime(["2026-11-01", "2026-05-22", "2026-05-21", None, None]),
        "observada_en": [None, None, None, "2026-11-22T15:00:00+00:00", None]})
    regla = {"max_meses_publicacion": 6, "sin_fecha_publicacion": "fecha_observacion"}
    m, info = desde_almacen.aplicar_vigencia(marco, regla, "2026-11-22")
    assert info["corte"] == "2026-05-22" and info["fuera_de_vigencia"] == 1 and info["sin_fecha"] == 0 and info["vigentes_por_observacion"] == 1
    assert [None if pd.isna(x) else x for x in m["motivo_invalido"]] == [None, None, "publicacion_fuera_de_vigencia", None, "sin_precio"]
    assert marco["motivo_invalido"].isna().sum() == 4                              # el marco original no se toca
    m2, info2 = desde_almacen.aplicar_vigencia(marco, {**regla, "sin_fecha_publicacion": "excluir"}, "2026-11-22")
    assert info2["sin_fecha"] == 1 and m2.loc[3, "motivo_invalido"] == "sin_fecha_de_publicacion"


def test_el_rpa_solo_lee_los_sitios_encendidos_y_valida_lo_que_recibe(servidor, p):
    lista = portales.catalogo(p)
    assert [x["id"] for x in portales.elegir(p, None)] == ["fincaraiz", "metrocuadrado"]            # Ciencuadras no se consulta
    assert portales.elegir(p, ["ciencuadras", "metrocuadrado"]) == [x for x in lista if x["id"] == "metrocuadrado"]
    with pytest.raises(ValueError):
        portales.validar_seleccion(["otro"], p)
    ctx = {"fecha": "2026-11-22", "tipos": ["apartamento"]}
    permitidos = portales.elegir(p, None)
    crudos = [{"portal": "x", "url": u, "tipo_inmueble": "apartamento", "precio_cop": 300000000, "area_m2": 60, "fecha_publicacion": f}
              for u, f in (("https://www.fincaraiz.com.co/a/1", "2026-10-01"), ("https://www.fincaraiz.com.co/a/1", None),
                           ("http://www.fincaraiz.com.co/a/2", None), ("https://fincaraiz.com.co.malo.com/a/3", None),
                           ("https://www.fincaraiz.com.co/a/4", "2027-01-01"), ("https://casas.metrocuadrado.com/a/5", "ayer"))]
    filas = portales.validar(crudos, ctx, permitidos, p)
    assert [f["motivo_exclusion"] for f in filas] == [None, "repetido_en_la_busqueda", "fuera_de_los_sitios_permitidos",
                                                      "fuera_de_los_sitios_permitidos", None, None]
    assert [f["fecha_publicacion"] for f in filas] == ["2026-10-01", None, None, None, None, None]   # fecha futura o mal escrita: vacía
    assert filas[5]["portal_id"] == "metrocuadrado"

    # todas apagadas: no se llama a Claude y la ficha queda sin comparables vigentes
    cot, prog = cotizar(servidor, p, portales=[])
    paso = next(x for x in prog["pasos"] if x["id"] == "inmobiliarias")
    assert paso["estado"] == "omitido" and "apagadas" in paso["detalle"] and LLAMADAS_RPA == []
    assert prog["ficha"]["estado"] == "sin_mercado"
    q = pedir(servidor, "GET", f"/api/cotizaciones/{cot}")[1]["cotizacion"]
    assert q["portales"] == [] and q["rpa"]["estado"] == "omitido"
    # se encienden y se vuelve a buscar: queda el resultado por sitio y la ficha nueva
    estado, r, _ = pedir(servidor, "POST", f"/api/cotizaciones/{cot}/inmobiliarias", {"portales": ["fincaraiz"]})
    assert estado == 200 and LLAMADAS_RPA[0]["dominios"] == ["fincaraiz.com.co"]
    assert r["rpa"]["estado"] == "ok" and r["rpa"]["utiles"] == 2 and r["rpa"]["fuera_de_lista"] == 2 and "anuncios" not in r["rpa"]
    assert [(x["portal"], x["utiles"], x["descartados"]) for x in r["rpa"]["por_portal"]] == [("fincaraiz", 2, 2)]
    assert r["ficha"]["version"] == 2 and r["ficha"]["resumen"]["n"] == 2
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute("SELECT capa, tipo_precio, motivo_exclusion, url FROM ofertas WHERE fuente='portales' ORDER BY oferta_id").fetchall()
        assert {f["capa"] for f in filas} == {"fincaraiz"} and {f["tipo_precio"] for f in filas} == {"pedido"}
        assert sorted(str(f["motivo_exclusion"]) for f in filas) == ["None", "None", "sin_precio", "tipo_no_solicitado"]
        assert con.execute("SELECT estado, registros, descartados FROM ejecuciones WHERE fuente='portales'").fetchone()[:] == ("ok", 2, 2)
    # Claude Code no disponible: la cotización sigue y queda dicho
    def caido(*a, **k):
        raise claude_cliente.ClaudeNoDisponible("Claude Code no respondió")
    import captura.curaduria.claude_cliente as cc
    cc_original, cc.invocar_con_web = cc.invocar_con_web, caido
    try:
        r = pedir(servidor, "POST", f"/api/cotizaciones/{cot}/inmobiliarias", {})[1]
    finally:
        cc.invocar_con_web = cc_original
    assert r["rpa"]["estado"] == "no_disponible" and "no respondió" in r["rpa"]["mensaje"] and r["ficha"]["version"] == 3


def test_buscar_un_barrio_devuelve_la_lista_con_su_centro(servidor, p, monkeypatch):
    pedidos = []

    async def capa(cli, p_, servicio_, capa_, **kw):
        pedidos.append(kw["donde"])
        cuadro = lambda x, y: {"rings": [[[x, y], [x + 0.01, y], [x + 0.01, y + 0.01], [x, y + 0.01], [x, y]]]}   # noqa: E731
        return ([{"attributes": {"codigo": "1101", "nombre_barrio": "LAURELES", "comuna": "11", "nombre_comuna": "LAURELES ESTADIO"},
                  "geometry": cuadro(-75.60, 6.24)},
                 {"attributes": {"codigo": "1105", "nombre_barrio": "BOLIVARIANA", "comuna": "11", "nombre_comuna": "LAURELES ESTADIO"},
                  "geometry": cuadro(-75.59, 6.24)}], 1, "")

    monkeypatch.setattr(api.arcgis, "consultar", capa)
    estado, r, _ = pedir(servidor, "GET", "/api/barrios?q=laur%27%3B--")           # comillas y punto y coma no llegan a la consulta
    assert estado == 200 and "'" not in pedidos[0].replace("LIKE '%LAUR%'", "") and "--" not in pedidos[0]
    assert [x["barrio"] for x in r["barrios"]] == ["Laureles", "Bolivariana"]      # primero el que coincide por nombre de barrio
    assert r["barrios"][0]["comuna"] == "Laureles Estadio" and abs(r["barrios"][0]["lat"] - 6.245) < 1e-6 and abs(r["barrios"][0]["lng"] + 75.595) < 1e-6
    assert pedir(servidor, "GET", "/api/barrios?q=la")[0] == 422


# =============================================================================================
# 8. Catálogo ampliado de inmobiliarias: lectura por lotes, prueba de lectura y fichas anteriores
# =============================================================================================
def test_el_catalogo_real_de_inmobiliarias_es_coherente():
    import yaml
    from urllib.parse import urlparse
    cat = yaml.safe_load((RAIZ / "config" / "inmobiliarias.yaml").read_text(encoding="utf-8"))
    sitios, descartadas = cat["sitios"], cat["descartadas"]
    assert len(sitios) >= 60 and len({s["id"] for s in sitios}) == len(sitios) and len({s["dominio"] for s in sitios}) == len(sitios)
    for s in sitios:
        assert s["tipo"] in ("portal", "inmobiliaria", "agregador") and s["nivel"] in ("A", "B", "C"), s["id"]
        assert re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", s["dominio"]) and not s["dominio"].startswith("www."), s["id"]
        host = (urlparse(s["busqueda"]).hostname or "").removeprefix("www.")
        assert s["busqueda"].startswith("http") and (host == s["dominio"] or host.endswith("." + s["dominio"])), s["id"]
        assert s["lectura"] in ("ok", "sin_resultados", "no_legible", "bloqueado", "error", "sin_probar"), s["id"]
        assert not (s["por_defecto"] and not s["permitida"]), s["id"]          # lo no permitido nunca sale encendido
    por_id = {s["id"]: s for s in sitios}
    assert por_id["ciencuadras"]["permitida"] is False and por_id["fincaraiz"]["permitida"] is True
    assert not {d["dominio"].removeprefix("www.").split("/")[0] for d in descartadas} & {s["dominio"] for s in sitios}
    assert all(d["motivo"] for d in descartadas)                                # lo que se descartó dice por qué


def test_la_busqueda_va_por_lotes_y_junta_los_resultados(p, monkeypatch):
    p["params"]["rpa"].update(activo=True, sitios_por_lote=3, lotes_en_paralelo=2, max_sitios_por_busqueda=0)
    sitios = [{"id": f"s{i}", "nombre": f"Sitio {i}", "dominio": f"s{i}.com", "tipo": "inmobiliaria", "nivel": "A", "permitida": True,
               "por_defecto": True, "busqueda": f"https://s{i}.com/venta/apartamentos/laureles"} for i in range(1, 8)]
    llamadas = []

    def falso(p_, sistema, mensaje, esquema_json, dominios, cfg):
        llamadas.append({"dominios": list(dominios), "mensaje": mensaje})
        if "s4.com" in dominios:
            raise claude_cliente.ClaudeNoDisponible("Claude Code no respondió en 420 s")
        return {"datos": {"anuncios": [{"portal": d, "url": f"https://www.{d}/apto/1", "tipo_inmueble": "apartamento",
                                        "precio_cop": 500000000, "area_m2": 80} for d in dominios],
                          "portales": [{"portal": d, "estado": "ok"} for d in dominios]}, "modelo": "prueba", "turnos": 2, "denegados": []}

    monkeypatch.setattr(claude_cliente, "invocar_con_web", falso)
    ctx = {"fecha": "2026-11-23", "operacion": "venta", "municipio": "Medellín", "barrio": "San Joaquín", "comuna": "Laureles Estadio",
           "tipos": ["apartamento"], "tipo_cotizado": "apartamento", "rango_area": [56.0, 104.0]}
    avances = []
    r = portales.buscar(p, ctx, sitios, avance=lambda h, t: avances.append((h, t)))
    assert sorted(tuple(x["dominios"]) for x in llamadas) == [("s1.com", "s2.com", "s3.com"), ("s4.com", "s5.com", "s6.com"), ("s7.com",)]
    assert sorted(avances) == [(1, 3), (2, 3), (3, 3)] and r["lotes"] == 3 and r["lotes_con_error"] == 1
    assert "búsqueda que funciona para apartamento en venta en Laureles: https://s1.com/venta/apartamentos/laureles" in llamadas[0]["mensaje"] or \
        any("https://s1.com/venta/apartamentos/laureles" in x["mensaje"] for x in llamadas)
    assert all("Barrio para las URL: san-joaquin." in x["mensaje"] for x in llamadas)
    estados = {x["portal"]: x["estado"] for x in r["por_portal"]}
    assert estados == {"s1": "ok", "s2": "ok", "s3": "ok", "s4": "error", "s5": "error", "s6": "error", "s7": "ok"}
    assert r["estado"] == "ok" and r["utiles"] == 4 and r["sitios_por_estado"] == {"ok": 4, "error": 3}
    assert "no respondió" in next(x["detalle"] for x in r["por_portal"] if x["portal"] == "s5")
    # con tope, lo que no se alcanza a leer queda dicho
    p["params"]["rpa"]["max_sitios_por_busqueda"] = 2
    llamadas.clear()
    r2 = portales.buscar(p, ctx, sitios)
    assert [x["dominios"] for x in llamadas] == [["s1.com", "s2.com"]]
    assert [x["estado"] for x in r2["por_portal"]].count("sin_consultar") == 5 and "tope de 2 sitios" in r2["por_portal"][-1]["detalle"]


def test_la_prueba_de_lectura_anota_el_catalogo_sin_tocar_lo_demas(tmp_path):
    prueba = _modulo("probar_inmobiliarias", RAIZ / "scripts" / "probar_inmobiliarias.py")
    archivo = tmp_path / "inmobiliarias.yaml"
    archivo.write_text("# comentario que se conserva\nsitios:\n  - id: uno\n    nombre: \"Uno\"\n    por_defecto: true\n    lectura: sin_probar\n"
                       "  - id: dos\n    nombre: \"Dos\"\n    por_defecto: true\n    lectura: sin_probar\n"
                       "  - id: tres\n    nombre: \"Tres\"\n    por_defecto: true\n    lectura: sin_probar\n", encoding="utf-8")
    n = prueba.anotar(archivo, [{"portal": "uno", "estado": "ok"}, {"portal": "dos", "estado": "no_legible"}], True, "2026-11-23")
    import yaml
    texto = archivo.read_text(encoding="utf-8")
    cat = {s["id"]: s for s in yaml.safe_load(texto)["sitios"]}
    assert n == 2 and texto.startswith("# comentario que se conserva\n")
    assert (cat["uno"]["lectura"], cat["uno"]["por_defecto"]) == ("ok", True)
    assert (cat["dos"]["lectura"], cat["dos"]["por_defecto"]) == ("no_legible", False)       # no se dejó leer: apagado por defecto
    assert (cat["tres"]["lectura"], cat["tres"]["por_defecto"]) == ("sin_probar", True)      # no se probó: queda igual


def test_volver_a_buscar_rehace_las_fichas_anteriores(servidor, p):
    from captura import rebuscar
    cot, prog = cotizar(servidor, p, rpa=False)                 # como las fichas de antes: sin búsqueda en inmobiliarias
    assert prog["ficha"]["estado"] == "sin_mercado" and fichas.leer(p, cot)["version"] == 1
    sin_datos, _ = cotizar(servidor, p, inmueble=None, exigir=False, rpa=False)
    p["params"]["rpa"]["activo"] = True
    filas = {r["cotizacion_id"]: r for r in rebuscar.rebuscar(p)}
    assert filas[cot]["estado"] == "ok" and filas[cot]["version"] == 2 and filas[cot]["comparables"] == 3 and filas[cot]["rpa"] == "ok"
    assert filas[sin_datos]["estado"] == "pendiente" and "datos del inmueble" in filas[sin_datos]["mensaje"]
    f = fichas.leer(p, cot)
    assert f["version"] == 2 and [v["version"] for v in f["versiones"]] == [2, 1]              # la anterior se conserva
    assert len(LLAMADAS_RPA) == 1
    assert [r["cotizacion_id"] for r in rebuscar.rebuscar(p, cot[:8])] == [cot]                # una sola, por el comienzo del id


# =============================================================================================
# Orden del flujo: entorno del predio (se reusa) → mercado de la cotización (del día)
# =============================================================================================
GEMELO = {"portal": "metrocuadrado.com", "url": "https://www.metrocuadrado.com/inmueble/venta-apartamento/107", "titulo": "Apto 1 otra vez",
          "tipo_inmueble": "apartamento", "precio_cop": 565000000, "area_m2": 80.3, "habitaciones": 3, "banos": 2,
          "barrio": "BELEN", "fecha_publicacion": None}        # el Apto 1 de Finca Raíz, publicado también en otro sitio


def _con_gemelo(monkeypatch):
    """La lectura de inmobiliarias devuelve, además, el mismo Apto 1 visto en otro sitio con área y precio casi iguales."""
    def falso(p, sistema, mensaje, esquema_json, dominios, cfg):
        LLAMADAS_RPA.append({"mensaje": mensaje, "dominios": list(dominios), "sistema": sistema})
        return {"datos": {"anuncios": ANUNCIOS_RPA + [GEMELO], "portales": []}, "modelo": "prueba", "turnos": 3, "denegados": []}
    monkeypatch.setattr(claude_cliente, "invocar_con_web", falso)


def test_la_depuracion_de_anuncios_enlaza_el_mismo_inmueble_publicado_en_dos_sitios(servidor, p, monkeypatch):
    _con_gemelo(monkeypatch)
    cot, prog = cotizar(servidor, p)
    pasos = {x["id"]: x for x in prog["pasos"]}
    assert prog["error"] is None and "4 anuncios útiles" in pasos["inmobiliarias"]["detalle"]
    assert pasos["depuracion"]["estado"] == "ok" and pasos["depuracion"]["detalle"] == (
        "4 anuncios válidos · 1 repetidos entre sitios enlazados · 3 vigentes (0 con fecha, 3 por el día en que se vieron)")
    assert pasos["almacen"]["estado"] == "ok" and "3 comparables" in pasos["ficha"]["detalle"]      # el repetido no cuenta dos veces

    # base operativa: nada se borra; el repetido queda enlazado con regla, puntaje y el anuncio que se conserva
    nk = lambda url: "portales:" + hashlib.sha256(url.encode()).hexdigest()[:20]              # noqa: E731
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = {f["url"]: f for f in con.execute("SELECT url, capa, motivo_exclusion, regla_dup, puntaje_dup, coincide_con FROM ofertas WHERE fuente='portales'")}
    gemelo, original = filas[GEMELO["url"]], filas[ANUNCIOS_RPA[0]["url"]]
    assert gemelo["regla_dup"] == "R3_entre_sitios" and 0 < gemelo["puntaje_dup"] < 1 and gemelo["motivo_exclusion"] is None
    assert gemelo["coincide_con"] == nk(ANUNCIOS_RPA[0]["url"]) and original["regla_dup"] is None
    assert sum(f["regla_dup"] is not None for f in filas.values()) == 1                          # los demás no se parecen tanto

    # almacén: llega como duplicado del anuncio conservado, con la misma regla y el mismo puntaje
    con = dw(p)
    try:
        d = con.execute("SELECT es_duplicado, es_valida, motivo_exclusion, regla_dup, puntaje_dup, coincide_con, id_representante, grupo_dup "
                        "FROM fact_oferta WHERE oferta_nk=?", (nk(GEMELO["url"]),)).fetchone()
        assert d[:4] == (1, 0, "duplicado", "R3_entre_sitios") and d[4] == gemelo["puntaje_dup"]
        assert d[5] == d[6] == d[7] == nk(ANUNCIOS_RPA[0]["url"])
        assert con.execute("SELECT es_duplicado, es_valida FROM fact_oferta WHERE oferta_nk=?", (nk(ANUNCIOS_RPA[0]["url"]),)).fetchone()[:] == (0, 1)
        estado_etl, resumen = con.execute("SELECT estado, resumen_json FROM etl_corrida WHERE modo='captura' ORDER BY corrida_id DESC LIMIT 1").fetchone()
        assert next(x for x in json.loads(resumen)["deduplicacion"] if x["base"] == "portales")["entre_sitios"] == 1
        assert estado_etl != "con_fallas"                         # los controles de calidad del almacén pasan con el enlace nuevo
    finally:
        con.close()

    # ficha: el criterio con tolerancia queda a la vista, en el mercado y en la trazabilidad
    md = sorted((p["rutas"]["fichas"] / "1017234567").glob("FICHA_*_v1.md"))[0]
    texto, ficha = md.read_text(encoding="utf-8"), json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
    assert ficha["mercado"]["precio_m2"]["n"] == 3 and ficha["mercado"]["repetidos_entre_sitios"]["n"] == 1
    assert ficha["trazabilidad"]["regla_deduplicacion"]["entre_sitios"] == {
        "tolerancia_area_pct": 0.01, "tolerancia_precio_pct": 0.02, "exigir_mismo_barrio": True, "iguales_si_existen": ["habitaciones", "banos"]}
    assert "1 anuncio se enlazó como el mismo inmueble publicado en otro sitio (área ±1 %, precio ±2 %, mismo barrio)" in texto
    assert "· entre sitios distintos: mismo barrio, área ±1 % y precio ±2 %" in texto
    # lo que ve el comercial en la cotización: el resumen del paso y qué anuncio se enlazó con cuál
    dep = pedir(servidor, "GET", f"/api/cotizaciones/{cot}")[1]["cotizacion"]["rpa"]["depuracion"]
    assert dep["repetidos_entre_sitios"] == 1 and dep["unicos"] == 3 and dep["regla"]["nombre"] == "R3_entre_sitios"
    nombres = {x["id"]: x["nombre"] for x in portales.catalogo(p)}
    assert dep["pares"] == [{"repetido": GEMELO["url"], "sitio_repetido": nombres["metrocuadrado"], "conservado": ANUNCIOS_RPA[0]["url"],
                             "sitio_conservado": nombres["fincaraiz"], "puntaje": gemelo["puntaje_dup"]}]

    # la regla se puede dejar en "marcar": se cuenta, pero el anuncio sigue entre los comparables
    p["almacen"]["bases_mercado"]["portales"]["duplicados_entre_sitios"]["accion"] = "marcar"
    try:
        r = pedir(servidor, "POST", f"/api/cotizaciones/{cot}/inmobiliarias", {})[1]
    finally:
        p["almacen"]["bases_mercado"]["portales"]["duplicados_entre_sitios"]["accion"] = "enlazar"
    assert r["rpa"]["depuracion"]["repetidos_entre_sitios"] == 1 and "marcados, sin sacar" in r["rpa"]["depuracion"]["mensaje"]
    assert "pares" not in r["rpa"]["depuracion"] and r["ficha"]["version"] == 2 and r["ficha"]["resumen"]["n"] == 4
    with bd.conectar(p["rutas"]["bd"]) as con:
        assert con.execute("SELECT COUNT(*) FROM ofertas WHERE regla_dup IS NOT NULL").fetchone()[0] == 0
    assert uno(p, "SELECT COUNT(*) FROM fact_oferta WHERE regla_dup='R3_entre_sitios'") == 0


def test_la_busqueda_en_inmobiliarias_arranca_con_la_captura_y_no_espera_a_que_termine(servidor, p, monkeypatch):
    from captura import servicio
    leido = threading.Event()
    lectura = claude_cliente.invocar_con_web                    # la respuesta fija de estas pruebas

    def lee_y_avisa(*a, **k):
        leido.set()
        return lectura(*a, **k)
    monkeypatch.setattr(claude_cliente, "invocar_con_web", lee_y_avisa)
    curar, visto = servicio.modelo.curar, {}

    def curar_esperando(*a, **k):                               # paso 4 de la captura: espera a que la búsqueda ya esté leyendo
        visto["a_tiempo"] = leido.wait(20)
        return curar(*a, **k)
    monkeypatch.setattr(servicio.modelo, "curar", curar_esperando)

    avisos = []                                                 # lo que va diciendo el paso mientras corre adelantado
    marcar = servicio.Progreso.marcar

    def marcar_y_anotar(self, paso, estado, detalle=""):
        if paso == "inmobiliarias":
            avisos.append((estado, detalle))
        return marcar(self, paso, estado, detalle)
    monkeypatch.setattr(servicio.Progreso, "marcar", marcar_y_anotar)

    cot, prog = cotizar(servidor, p)
    assert visto["a_tiempo"], "la búsqueda en inmobiliarias no había arrancado cuando la captura iba en la depuración por reglas"
    assert [e for e, _ in avisos][-2:] == ["en_curso", "ok"] and avisos[-1][1].startswith("3 anuncios útiles")
    assert avisos[-2][1].startswith("lectura terminada: 3 anuncios útiles") and "se guarda cuando cierre el entorno" in avisos[-2][1]
    pasos = {x["id"]: x for x in prog["pasos"]}
    assert prog["error"] is None and not prog["reusada"] and pasos["inmobiliarias"]["estado"] == "ok"
    assert pasos["depuracion"]["estado"] == "ok" and pasos["ficha"]["estado"] == "ok" and len(LLAMADAS_RPA) == 1
    assert "barrio" in LLAMADAS_RPA[0]["mensaje"] and "Medellín" in LLAMADAS_RPA[0]["mensaje"]    # ya llevaba la zona del predio
    q = pedir(servidor, "GET", f"/api/cotizaciones/{cot}")[1]["cotizacion"]
    assert q["rpa"]["estado"] == "ok" and q["rpa"]["consulta"]["barrio"] and q["rpa"]["depuracion"]["anuncios_validos"] == 3

    # captura reusada: no hay nada que adelantar, pero los anuncios se buscan otra vez (son del día)
    visto.clear()
    otra, prog2 = cotizar(servidor, p)
    assert prog2["reusada"] and "a_tiempo" not in visto and len(LLAMADAS_RPA) == 2
    assert {x["id"]: x["estado"] for x in prog2["pasos"]}["inmobiliarias"] == "ok"
    assert LLAMADAS_RPA[1]["mensaje"] == LLAMADAS_RPA[0]["mensaje"]                                # la misma consulta por los dos caminos


def test_si_la_captura_falla_lo_que_traia_la_busqueda_no_se_guarda(servidor, p, monkeypatch):
    from captura import servicio

    def rota(*a, **k):
        raise RuntimeError("falla de prueba en la depuración por reglas")
    monkeypatch.setattr(servicio.modelo, "curar", rota)
    cot, prog = cotizar(servidor, p)
    pasos = {x["id"]: x for x in prog["pasos"]}
    assert "falla de prueba" in prog["error"] and pasos["reglas"]["estado"] == "error"
    assert {pasos[k]["estado"] for k in ("inmobiliarias", "depuracion", "almacen", "ficha")} == {"omitido"}
    __import__("time").sleep(0.2)                                # la lectura adelantada ya terminó: su aviso no cambia el paso
    _, prog, _ = pedir(servidor, "GET", f"/api/trabajos/{[k for k, v in api.TRABAJOS.items() if v.captura_id == prog['captura_id']][0]}")
    assert {x["id"]: x["estado"] for x in prog["pasos"]}["inmobiliarias"] == "omitido"
    with bd.conectar(p["rutas"]["bd"]) as con:
        assert con.execute("SELECT COUNT(*) FROM ofertas WHERE fuente='portales'").fetchone()[0] == 0
        assert con.execute("SELECT rpa_json FROM cotizaciones WHERE cotizacion_id=?", (cot,)).fetchone()[0] is None
        assert con.execute("SELECT estado FROM capturas WHERE captura_id=?", (prog["captura_id"],)).fetchone()[0] == "error"


# =============================================================================================
# Estado de la ficha con el cliente y hoja para imprimir
# =============================================================================================
def test_el_comercial_registra_el_estado_de_la_ficha_con_el_cliente(servidor, p):
    cot, prog = cotizar(servidor, p)
    assert prog["ficha"]["estado"] == "ok"
    catalogo = pedir(servidor, "GET", "/api/config")[1]["gestion_cliente"]
    assert [e["id"] for e in catalogo["estados"]] == ["por_entregar", "entregada", "aceptada", "rechazada"]
    assert [e["id"] for e in catalogo["estados"] if e["exige_motivo"]] == ["rechazada"] and catalogo["estado_inicial"] == "por_entregar"
    # recién hecha: por entregar, sin nada registrado todavía
    det = pedir(servidor, "GET", f"/api/cotizaciones/{cot}")[1]
    assert det["gestion"]["vigente"]["estado"] == "por_entregar" and det["gestion"]["vigente"]["sin_registrar"] and det["gestion"]["historial"] == []
    assert det["cotizacion"]["estado_cliente"] == {"estado": "por_entregar", "etiqueta": "Por entregar", "desde": det["ficha"]["generada_en"],
                                                   "sin_registrar": True, "motivo_etiqueta": None}
    ruta = f"/api/cotizaciones/{cot}/estado-cliente"
    # entregada
    estado, r, _ = pedir(servidor, "POST", ruta, {"estado": "entregada", "nota": "  Se la envié   por WhatsApp "})
    assert estado == 200 and r["gestion"]["vigente"]["estado"] == "entregada" and r["gestion"]["vigente"]["nota"] == "Se la envié por WhatsApp"
    assert r["gestion"]["vigente"]["ficha_version"] == 1 and r["gestion"]["vigente"]["motivo"] is None
    # rechazada: pide el tipo de motivo y qué dijo el cliente
    for cuerpo, fragmento in (({"estado": "rechazada"}, "Elige el motivo"), ({"estado": "rechazada", "motivo_tipo": "precio"}, "qué dijo el cliente"),
                              ({"estado": "rechazada", "motivo_tipo": "precio", "motivo": "no"}, "qué dijo el cliente"),
                              ({"estado": "rechazada", "motivo_tipo": "clima", "motivo": "No le gustó"}, "Elige el motivo"),
                              ({"estado": "vendida"}, "Elige el estado"), ({}, "Elige el estado"),
                              ({"estado": "entregada", "nota": "Se la envié por WhatsApp"}, "ya es el estado")):
        estado, r, _ = pedir(servidor, "POST", ruta, cuerpo)
        assert estado == 422 and fragmento in r["error"], (cuerpo, r)
    estado, r, _ = pedir(servidor, "POST", ruta, {"estado": "rechazada", "motivo_tipo": "precio", "motivo": "Le pareció alto para la zona"})
    assert estado == 200 and r["gestion"]["vigente"]["motivo_etiqueta"] == "Precio" and len(r["gestion"]["historial"]) == 2
    # el cliente cambia de parecer: se registra otro estado; el motivo anterior queda en la historia, no en el estado nuevo
    estado, r, _ = pedir(servidor, "POST", ruta, {"estado": "aceptada", "motivo_tipo": "precio", "motivo": "esto no se guarda"})
    assert estado == 200 and [x["estado"] for x in r["gestion"]["historial"]] == ["aceptada", "rechazada", "entregada"]
    assert r["gestion"]["vigente"]["motivo"] is None and r["gestion"]["historial"][1]["motivo"] == "Le pareció alto para la zona"
    # se ve en la lista del comercial y en la historia del cliente
    lista = pedir(servidor, "GET", "/api/cotizaciones")[1]["cotizaciones"]
    assert lista[0]["estado_cliente"]["estado"] == "aceptada" and lista[0]["estado_cliente"]["sin_registrar"] is False
    historia = pedir(servidor, "GET", "/api/clientes/1017234567")[1]["cliente"]["historia"]
    assert len(historia) == 1 and historia[0]["cotizacion_id"] == cot and historia[0]["estado_cliente"]["estado"] == "aceptada"
    assert historia[0]["cambios_de_estado"] == 3 and historia[0]["tipo"] == "apartamento" and historia[0]["ficha_version"] == 1
    cliente_id = pedir(servidor, "GET", "/api/clientes/1017234567")[1]["cliente"]["cliente_id"]
    assert pedir(servidor, "GET", f"/api/clientes/id/{cliente_id}/historia")[1]["historia"] == historia      # la misma, pedida por su identificador
    # nada se reescribe: tres filas, en orden
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute("SELECT estado, comercial, cliente_id, motivo_tipo FROM gestion_cliente ORDER BY gestion_id").fetchall()
    assert [(f["estado"], f["comercial"], f["motivo_tipo"]) for f in filas] == [("entregada", "comercial1", None), ("rechazada", "comercial1", "precio"),
                                                                              ("aceptada", "comercial1", None)]
    # solo el dueño lo cambia: otro comercial no la encuentra y el supervisor la ve pero no la toca
    otro, sup = como(servidor, "comercial2"), como(servidor, "supervisor1")
    assert pedir(servidor, "POST", ruta, {"estado": "entregada"}, cabeceras=otro, sin_sesion=True)[0] == 404
    assert pedir(servidor, "POST", ruta, {"estado": "entregada"}, cabeceras=sup, sin_sesion=True)[0] == 403
    visto = pedir(servidor, "GET", f"/api/cotizaciones/{cot}", cabeceras=sup, sin_sesion=True)[1]
    assert visto["gestion"]["vigente"]["estado"] == "aceptada" and visto["es_propia"] is False
    assert pedir(servidor, "GET", "/api/cotizaciones", cabeceras=sup, sin_sesion=True)[1]["cotizaciones"][0]["estado_cliente"]["estado"] == "aceptada"
    assert pedir(servidor, "GET", "/api/clientes/1017234567", cabeceras=otro, sin_sesion=True)[1]["cliente"]["historia"] == []   # la historia no se cruza
    assert pedir(servidor, "GET", f"/api/clientes/id/{cliente_id}/historia", cabeceras=otro, sin_sesion=True)[1]["historia"] == []
    assert len(pedir(servidor, "GET", f"/api/clientes/id/{cliente_id}/historia", cabeceras=sup, sin_sesion=True)[1]["historia"]) == 1
    # sin ficha no hay nada que entregar ni que imprimir
    p["reglas"]["fichas"]["generar_al_cotizar"] = False
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False,
                                                            "cliente_id": CLIENTES[servidor], "inmueble": INMUEBLE})
    sin_ficha = r["cotizacion_id"]
    esperar(servidor, r["trabajo_id"])
    det = pedir(servidor, "GET", f"/api/cotizaciones/{sin_ficha}")[1]
    assert det["ficha"] is None and det["gestion"] is None and det["cotizacion"]["estado_cliente"] is None
    estado, r, _ = pedir(servidor, "POST", f"/api/cotizaciones/{sin_ficha}/estado-cliente", {"estado": "entregada"})
    assert estado == 422 and "todavía no tiene ficha" in r["error"]
    assert pedir(servidor, "GET", f"/api/cotizaciones/{sin_ficha}/ficha/cliente")[0] == 404
    assert [h["estado_cliente"] for h in pedir(servidor, "GET", "/api/clientes/1017234567")[1]["cliente"]["historia"]
            if h["cotizacion_id"] == sin_ficha] == [None]


def test_la_hoja_para_el_cliente_es_simple_y_no_muestra_el_procesamiento(servidor, p):
    cot, prog = cotizar(servidor, p)
    estado, hoja, r = pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha/cliente")
    assert estado == 200 and r.getheader("Content-Type").startswith("text/html") and hoja.startswith("<!DOCTYPE html>")
    assert "{{" not in hoja and "window.print(); }, " not in hoja                     # plantilla completa; sin abrir sola la impresión
    # lo que el cliente necesita: el inmueble, la cifra, lo que hay cerca, cómo se obtuvo, quién lo atendió
    for texto in ("Apartamento en Bolivariana", "Ficha del inmueble preparada para Ana María Quintero", "80 m², 3 habitaciones, 2 baños y 1 parqueadero",
                  "Precio de referencia en el mercado", "$7.500.000 <span>por m²</span>", "cerca de <b>$600 millones</b>",
                  "Los 3 anuncios parecidos van de $7.000.000 a $8.000.000 por m²", "Referencia razonable", "Lo que hay cerca",
                  "La estación Estadio del metro queda a", "A 800 m a la redonda", "Se compararon 3 anuncios vigentes de venta del mismo tipo de inmueble, "
                  "en Bolivariana y barrios vecinos, con área entre 56 y 104 m²", "Le atendió Comercial 1, Venture One", f"Ficha {cot[:8].upper()}, versión 1",
                  "no es un avalúo ni una recomendación de compra o de inversión", "Imprimir o guardar como PDF"):
        assert texto in hoja, texto
    # nada del procesamiento ni datos de más: sin ETL, huellas, duplicados, términos técnicos ni el documento del cliente
    visible = re.sub(r"<style>.*?</style>", "", hoja, flags=re.S)
    for texto in ("ETL", "huella", "duplicad", "mediana", "comparables", "coeficiente", "almacén", "corrida", "Claude", "1017234567", "confianza",
                  "OIME", "POT", "CBML"):
        assert texto.lower() not in visible.lower(), texto
    assert not PROHIBIDO.search(hoja)
    assert "window.print(); }, " in pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha/cliente?imprimir=1")[1]
    # quién puede abrirla: el dueño y su supervisor; otro comercial no la encuentra; el administrador no ve fichas
    assert pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha/cliente", cabeceras=como(servidor, "supervisor1"), sin_sesion=True)[0] == 200
    assert pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha/cliente", cabeceras=como(servidor, "comercial2"), sin_sesion=True)[0] == 404
    assert pedir(servidor, "GET", f"/api/cotizaciones/{cot}/ficha/cliente", cabeceras=como(servidor, "admin"), sin_sesion=True)[0] == 403
    # lo que llega del usuario va escapado
    from ficha import hoja_cliente
    ficha = json.loads(sorted((p["rutas"]["fichas"] / "1017234567").glob("FICHA_*_v1.json"))[0].read_text(encoding="utf-8"))
    ficha["cotizacion"]["cliente"]["nombre"] = '<script>alert("x")</script>'
    assert "<script>alert" not in hoja_cliente.construir(ficha, p) and "&lt;script&gt;alert" in hoja_cliente.construir(ficha, p)
    # sin anuncios vigentes la hoja lo dice, sin inventar una cifra
    ficha["mercado"]["precio_m2"].update(n=0, mediana=None)
    ficha["mercado"]["comparables"] = []
    hoja = hoja_cliente.construir(ficha, p).split("<main")[1]
    assert "Hoy no hay suficientes anuncios vigentes de inmuebles parecidos" in hoja and "por m²</span>" not in hoja and "millones" not in hoja
    assert "Se buscaron anuncios vigentes de venta del mismo tipo de inmueble en la zona" in hoja and "Lo que hay cerca" in hoja


def test_la_hoja_del_cliente_redondea_y_muestra_donde_esta_la_mitad_de_los_anuncios(p):
    from ficha import hoja_cliente
    cfg = p["reglas"]["hoja_cliente"]
    precios = [6_000_000, 7_000_000, 7_400_000, 7_604_000, 8_000_000, 9_000_000, 12_000_000]
    ficha = {"inmueble": {"area_m2": 80.0, "tipo": "casa_lote", "barrio": "Laureles", "municipio": "Medellín"},
             "mercado": {"precio_m2": {"n": 7, "mediana": 7_604_000, "minimo": 6_000_000, "maximo": 12_000_000}, "confianza": {"nivel": "baja"},
                         "comparables": [{"precio_m2": x, "en_referencia": True} for x in precios] + [{"precio_m2": 99_000_000, "en_referencia": False}],
                         "criterio_comparables": {"rango_area_m2": [56.0, 104.0]}, "radio_m": 1000},
             "entorno": {}, "metadatos": {"fecha_generacion": "2026-11-23"}, "cotizacion": {"cotizacion_id": "abcdef12-0000", "version": 3}}
    r = hoja_cliente.resumen(ficha, p)
    assert cfg["rango_percentiles"] == [25, 75] and r["rango_es_tipico"] and r["n"] == 7
    assert (r["m2"], r["m2_bajo"], r["m2_alto"], r["m2_minimo"], r["m2_maximo"]) == (7_600_000, 7_200_000, 8_500_000, 6_000_000, 12_000_000)
    assert (r["total"], r["total_bajo"], r["total_alto"]) == (608_000_000, 576_000_000, 680_000_000)
    assert r["barra"] == {"desde": 20.0, "ancho": 21.7, "centro": 26.7}                  # posiciones de la franja y de la línea, en %
    hoja = hoja_cliente.construir(ficha, p)
    for texto in ("Casa lote en Laureles", "$7.600.000 <span>por m²</span>", "cerca de <b>$608 millones</b>", "23 de noviembre de 2026",
                  "La mitad de los anuncios parecidos pide entre $7.200.000 y $8.500.000 por m², es decir, entre $576 y $680 millones",
                  "Referencia orientativa", "a 1.000 m o menos del inmueble", "Ficha ABCDEF12, versión 3", "No hay datos del entorno para este punto"):
        assert texto in hoja, texto
    # sin área no se inventa el valor del inmueble completo
    ficha["inmueble"]["area_m2"] = None
    assert hoja_cliente.resumen(ficha, p)["total"] is None and "millones" not in hoja_cliente.construir(ficha, p).split("<main")[1]
