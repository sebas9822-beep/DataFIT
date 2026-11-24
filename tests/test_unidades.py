"""Pruebas unitarias: geometría, direcciones, normalización, clasificación y duplicados."""
from __future__ import annotations

import asyncio
import math
import random

import httpx
import pytest

from conftest import LAT, LNG, pt

from captura import geo
from captura.curaduria import clasificacion, deduplicacion
from captura.curaduria.normalizacion import a_etiqueta, limpiar_nombre, nombre_clave
from captura.direcciones import normalizar
from captura.fuentes.base import depurar_texto, limpiar_personales


# ---------------- geometría ----------------
def test_subcirculos_cubren_todo_el_circulo(p):
    traslape = p["reglas"]["google"]["traslape_subcirculo"]
    subs = geo.subcirculos(LAT, LNG, 1000, traslape)
    assert len(subs) == 7
    random.seed(1)
    for _ in range(5000):
        d, ang = 1000 * math.sqrt(random.random()), random.uniform(0, 360)
        la, ln = geo.desplazar(LAT, LNG, d, ang)
        assert any(geo.haversine_m(la, ln, a, b) <= r for a, b, r in subs), (d, ang)
    la, ln = geo.desplazar(LAT, LNG, 1000, 30)          # punto más desfavorable del borde
    assert min(geo.haversine_m(la, ln, a, b) - r for a, b, r in subs) <= 0


def test_distancia_a_poligono_mide_al_borde_y_cero_adentro():
    la, ln = pt(500, 90)
    d = 50 / 111_320
    cuadro = [[[ln - d, la - d], [ln + d, la - d], [ln + d, la + d], [ln - d, la + d], [ln - d, la - d]]]
    assert geo.distancia_punto_poligono_m(la, ln, cuadro) == 0.0
    al_borde = geo.distancia_punto_poligono_m(LAT, LNG, cuadro)
    assert 440 < al_borde < 460                          # centro a 500 m, borde a ~450 m


def test_control_9377_coincide_con_haversine():
    la, ln = pt(1000, 73)
    plana = geo.distancia_9377_m(LAT, LNG, la, ln)
    assert plana is not None and abs(plana - 1000) < 1.0  # diferencia de centímetros a 1 km
    x, y = geo.a_9377(LNG, LAT)
    assert 4_700_000 < x < 4_730_000 and 2_240_000 < y < 2_260_000


def test_anillo_de():
    assert geo.anillo_de(120, [300, 500, 1000]) == 300
    assert geo.anillo_de(500, [300, 500, 1000]) == 500
    assert geo.anillo_de(1001, [300, 500, 1000]) is None
    assert geo.anillo_de(None, [300, 500, 1000]) is None


# ---------------- direcciones ----------------
def test_normalizador_de_direcciones():
    casos = {
        "Calle 10 # 43E-25, Medellín": "CL 10 # 43E - 25",
        "Cra. 43A No. 1-50 apto 301": "CR 43A # 1 - 50",
        "CR 70 44 12": "CR 70 # 44 - 12",
        "Transversal 39B #75-20 Int 201": "TV 39B # 75 - 20",
        "Circular 1 N° 70-01": "CQ 1 # 70 - 1",
        "Diagonal 75B # 2A - 80": "DG 75B # 2A - 80",
        "Calle 10 Sur # 50-30": "CL 10 SUR # 50 - 30",
        "Carrera 73 # 2-18": "CR 73 # 2 - 18",
    }
    for texto, esperado in casos.items():
        assert normalizar(texto).canonica == esperado, texto
    d = normalizar("Cra. 43A No. 1-50 apto 301")
    assert d.interior == "APTO 301" and d.ap_via == "A" and d.placa == 50
    assert normalizar("Calle 10 Sur # 50-30").sur_via is True
    assert normalizar("Laureles cerca al estadio").valida is False     # no se inventa un punto
    assert normalizar("").valida is False


# ---------------- normalización ----------------
def test_limpiar_nombre_respeta_siglas_y_no_inventa(p):
    tax = p["taxonomia"]
    assert limpiar_nombre("INSTITUCION EDUCATIVA  INEM JOSE FELIX DE RESTREPO", tax) == "Institucion Educativa INEM Jose Felix de Restrepo"
    assert limpiar_nombre("biblioteca publica piloto", tax) == "Biblioteca Publica Piloto"
    assert limpiar_nombre("Parque de El Poblado", tax) == "Parque de El Poblado"      # ya venía bien: no se toca
    assert limpiar_nombre("CAI LA CASTELLANA", tax) == "CAI La Castellana"             # artículo de nombre propio
    assert limpiar_nombre("PARQUE DE LA BOLIVARIANA", tax) == "Parque de la Bolivariana"
    assert limpiar_nombre("EDIFICIO LOS ALMENDROS", tax) == "Edificio Los Almendros"
    for vacio in (None, "", "  ", "No aplica", "NULL"):
        assert limpiar_nombre(vacio, tax) is None


def test_nombre_clave_quita_genericos_y_tildes(p):
    tax = p["taxonomia"]
    a = nombre_clave("Institución Educativa INEM José Félix de Restrepo", tax)
    b = nombre_clave("INEM JOSE FELIX DE RESTREPO", tax)
    assert a == b == "inem jose felix restrepo"
    assert nombre_clave("Parque", tax) == ""
    assert a_etiqueta("Parque Recreativo") == "parque_recreativo"
    assert a_etiqueta("Educación básica") == "educacion_basica"


# ---------------- clasificación ----------------
def test_clasificacion_geomedellin(p):
    tax = p["taxonomia"]
    assert clasificacion.geomed_equipamiento("Lo que sea", 3, tax)[0] == "seguridad"          # el código 3 manda
    assert clasificacion.geomed_equipamiento("Educación básica", 1, tax) == ("educacion", "colegio", "alta")
    assert clasificacion.geomed_equipamiento("EDUCACION BASICA", "1", tax)[0] == "educacion"   # sin tildes ni mayúsculas
    assert clasificacion.geomed_equipamiento("Equipamientos institucionales Locales", 5, tax) == (
        "otros_equipamientos", "institucional", "media")
    cat, _, conf = clasificacion.geomed_equipamiento("Tipo que no existe", 1, tax)
    assert (cat, conf) == ("otros_equipamientos", "baja")


def test_todos_los_tipos_reales_de_geomedellin_estan_cubiertos(p):
    """Tipos listados del servicio real el 20-nov-2026: ninguno debe caer en confianza baja."""
    reales = [
        "Equipamientos Institucionales Metropolitanos", "Centros de integración barrial o unidades básicas de servicios",
        "Escenario deportivo-recreativo", "Centros de innovación, investigación, ciencia y tecnología",
        "Equipamientos institucionales Nacionales", "Equipamientos de Educación para el trabajo y el desarrollo humano",
        "Centros de promoción y divulgación artística y cultural", "Equipamientos para el aseo",
        "Equipamientos para la prestación del servicio de alcantarillado", "Educación básica",
        "Equipamientos institucionales Regionales", "Equipamientos institucionales Municipales",
        "Distribución General de combustible", "Equipamientos deportivos y recreativos no tradicionales",
        "Equipamientos para el Transporte Público Colectivo y Masivo", "Transporte por cable",
        "Centros de Exposiciones y Convenciones", "Unidad Deportiva", "Casas de la cultura",
        "Centros de atención y recuperación física, psíquica y social", "Centros de desarrollo cultural",
        "Equipamientos para el Transporte aéreo", "Centros de salvaguarda del patrimonio y la memoria",
        "Parques biblioteca", "Equipamiento de Desarrollo Deportivo Integral", "Transporte de buses",
        "Unidad Deportiva Satélite", "Equipamientos institucionales Locales",
        "Equipamientos para la prestación del servicio de energía", "Ciudadelas Universitarias",
        "Morgues, anfiteatros y depósitos de cadáveres", "Educación Superior",
        "Equipamientos para la prestación del servicio de acueducto", "Plazas o galerías de mercado",
        "Equipamientos para el Transporte ferroviario", "Espacios para el culto", "Equipamiento Recreativo",
        "Equipamientos para la Atención física y psicosocial", "Equipamientos de segundo nivel de atención",
        "Equipamientos para la prestación del servicio de Telecomunicaciones",
        "Equipamientos de tercer nivel de atención", "Albergues", "Cementerios humanos", "Lugares de apoyo al culto",
        "Equipamientos de primer nivel de atención",
    ]
    tax = p["taxonomia"]
    for tipo in reales:
        cat, sub, conf = clasificacion.geomed_equipamiento(tipo, 1, tax)
        assert conf != "baja" and cat in tax["categorias"] and sub, tipo


def test_clasificacion_ciiu_toponimia_estacion_y_osm(p):
    tax = p["taxonomia"]
    assert clasificacion.ciiu("8622", "03", tax)[0] == "salud"
    assert clasificacion.ciiu("4773", "02", tax) == ("salud", "farmacia", "alta")              # gana el prefijo largo
    assert clasificacion.ciiu("4711", "02", tax)[0] == "abastecimiento"
    assert clasificacion.ciiu("9102", "05", tax) == ("cultura", "museo", "alta")
    assert clasificacion.ciiu("5613", "03", tax) == ("empresas", "servicios", "media")
    assert clasificacion.ciiu(None, None, tax) == ("empresas", "establecimiento", "baja")
    assert clasificacion.toponimia("02", tax) == ("residencial", "conjunto_residencial", "alta")
    assert clasificacion.toponimia(4, tax)[0] == "educacion"
    assert clasificacion.toponimia("46", tax)[2] == "baja"
    assert clasificacion.estacion("Línea B", "Estación", tax) == ("movilidad", "metro", "alta")
    assert clasificacion.estacion("Línea X", "Estación", tax)[2] == "media"
    assert clasificacion.osm({"amenity": "school"}, tax) == ("educacion", "colegio", "alta")
    assert clasificacion.osm({"office": "company"}, tax) == ("empresas", "oficina_company", "media")
    assert clasificacion.osm({"building": "apartments"}, tax) is None                          # sin nombre no sirve
    assert clasificacion.osm({"building": "apartments", "name": "Torre"}, tax)[0] == "residencial"
    assert clasificacion.osm({"highway": "crossing"}, tax) is None


# ---------------- datos personales ----------------
def test_datos_personales_se_eliminan(p):
    prohibidos = {c.lower() for c in p["reglas"]["datos_personales"]["campos_prohibidos"]}
    limpio = limpiar_personales({"nombre": "X", "Telefono": "300", "phone": 1, "anidado": {"correo": "a@b.co", "ok": 1}}, prohibidos)
    assert limpio == {"nombre": "X", "anidado": {"ok": 1}}
    assert depurar_texto("Llamar al 300 123 4567 o a x@y.co") == "Llamar al [tel] o a [correo]"


# ---------------- duplicados ----------------
def _reg(ref, fuente, capa, cat, nombre, dist, rumbo, p, **extra):
    la, ln = pt(dist, rumbo)
    base = {"ref": ref, "fuente": fuente, "capa": capa, "categoria": cat, "subtipo": "x", "nombre": nombre,
            "nombre_clave": nombre_clave(nombre, p["taxonomia"]), "lat": la, "lng": ln, "distancia_m": dist,
            "dedupe": True, "motivo_exclusion": None, "anillos": None}
    base.update(extra)
    return base


def test_duplicados_auto_revision_y_distintos(p):
    r = p["reglas"]["deduplicacion"]
    regs = [
        _reg("geo:a:1", "geomedellin", "a", "educacion", "Institucion Educativa INEM Jose Felix de Restrepo", 600, 90, p),
        _reg("osm:o:1", "osm", "o", "educacion", "INEM José Félix de Restrepo", 610, 90, p),          # mismo lugar
        _reg("osm:o:2", "osm", "o", "educacion", "Colegio San José de la Salle", 300, 10, p),
        _reg("cat:t:2", "catastro", "t", "educacion", "COLEGIO SAN JOSE", 340, 10, p),               # parecido: revisión
        _reg("osm:o:3", "osm", "o", "educacion", "Colegio Nuevo Horizonte", 900, 200, p),             # solo
        _reg("osm:o:4", "osm", "o", "salud", "INEM José Félix de Restrepo", 605, 90, p),              # otra categoría
        _reg("osm:o:5", "osm", "o", "educacion", "INEM José Félix de Restrepo", 950, 270, p),         # lejos
    ]
    revision = deduplicacion.enlazar(regs, p, LAT, LNG)
    por = {x["ref"]: x for x in regs}
    assert por["geo:a:1"]["entidad_id"] == por["osm:o:1"]["entidad_id"]
    assert por["geo:a:1"]["es_maestro"] == 1 and por["osm:o:1"]["es_maestro"] == 0       # manda la fuente oficial
    assert por["osm:o:1"]["coincide_con"] == "geo:a:1" and por["osm:o:1"]["puntaje_dup"] >= r["umbral_auto"]
    assert por["osm:o:1"]["regla_dup"] == "nombre+distancia"
    assert [x["par"] for x in revision] == ["osm:o:2|cat:t:2"]
    assert r["umbral_revision"] <= revision[0]["puntaje"] < r["umbral_auto"]
    for solo in ("osm:o:3", "osm:o:4", "osm:o:5", "osm:o:2", "cat:t:2"):
        assert por[solo]["es_maestro"] == 1 and por[solo]["entidad_id"] == "E:" + solo
    assert len(regs) == 7                                                                # nada se borra


def test_fusion_por_revision(p):
    regs = [_reg("osm:o:2", "osm", "o", "educacion", "Colegio San José de la Salle", 300, 10, p),
            _reg("cat:t:2", "catastro", "t", "educacion", "COLEGIO SAN JOSE", 340, 10, p)]
    deduplicacion.enlazar(regs, p, LAT, LNG)
    assert deduplicacion.fusionar(regs, "osm:o:2", "cat:t:2", p["reglas"]["deduplicacion"], "revision_claude", 0.7)
    assert regs[0]["entidad_id"] == regs[1]["entidad_id"] == "E:cat:t:2"
    assert regs[0]["coincide_con"] == "cat:t:2" and regs[1]["es_maestro"] == 1
    assert not deduplicacion.fusionar(regs, "osm:o:2", "cat:t:2", p["reglas"]["deduplicacion"], "x", None)


def test_punto_dentro_de_un_poligono_lejano_del_centroide_se_enlaza(p):
    """Un campus grande: el centroide queda a más de 80 m del punto, pero el punto está adentro."""
    la, ln = pt(600, 90)
    d = 300 / 111_320
    anillos = [[[ln - d, la - d], [ln + d, la - d], [ln + d, la + d], [ln - d, la + d], [ln - d, la - d]]]
    campus = _reg("geo:a:9", "geomedellin", "a", "educacion", "Universidad Pontificia Bolivariana", 600, 90, p, anillos=anillos)
    dentro = _reg("osm:o:9", "osm", "o", "educacion", "Universidad Pontificia Bolivariana", 800, 95, p)
    deduplicacion.enlazar([campus, dentro], p, LAT, LNG)
    assert campus["entidad_id"] == dentro["entidad_id"]


# ---------------------------------------------------------------------------------------------
# Paginación del servidor de mapas (comportamiento real verificado el 21-nov-2026)
# ---------------------------------------------------------------------------------------------
def _servidor_falso(candidatos, tope_servidor=None, fallas=None, ilegibles=()):
    """Servidor ArcGIS mínimo. `candidatos`: [(objectid, devuelve?)]. `tope_servidor`: máximo de filas
    por respuesta aunque se pidan más. `fallas`: números de solicitud que responden error en el JSON.
    `ilegibles`: llaves cuya fila hace fallar la consulta si se piden sus atributos (fecha inválida)."""
    import re
    from urllib.parse import parse_qs, urlparse
    vistas = []
    error = {"error": {"code": 400, "message": "Failed to execute query."}}

    def manejador(req: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(req.url)).query)
        vistas.append(q)
        if fallas and len(vistas) in fallas:
            return httpx.Response(200, json=error)
        m = re.search(r"objectid > (\d+)", q["where"][0])
        una = re.search(r"objectid = (\d+)", q["where"][0])
        resto = [(o, ok) for o, ok in candidatos if (o == int(una[1]) if una else o > (int(m[1]) if m else 0))]
        ini, tam = int(q.get("resultOffset", ["0"])[0]), int(q.get("resultRecordCount", [len(resto) or 1])[0])
        if tope_servidor:
            tam = min(tam, tope_servidor)
        ventana = resto[ini:ini + tam]
        leidas = resto[ini:ini + tam + 1]            # el servidor lee una fila de más para saber si quedan
        if q["outFields"] != ["objectid"] and any(o in ilegibles for o, _ in leidas):
            return httpx.Response(200, json=error)
        return httpx.Response(200, json={
            "features": [{"attributes": {"OBJECTID": o}} for o, ok in ventana if ok],
            "exceededTransferLimit": ini + tam < len(resto)})
    return manejador, vistas


def _consultar(p, manejador, **kw):
    from captura.fuentes import arcgis

    async def correr():
        async with httpx.AsyncClient(transport=httpx.MockTransport(manejador)) as cli:
            return await arcgis.consultar(cli, p, "svc/Capa", 0, punto=(6.2, -75.5), distancia_m=1010, **kw)
    return asyncio.run(correr())


@pytest.mark.parametrize("candidatos, tope", [
    ([(i, i % 12 != 0) for i in range(1, 61)], None),            # 8 % de candidatos fuera del círculo (caso real)
    ([(i, True) for i in range(1, 24)], 3),                      # el servidor entrega menos de lo que se pide
    ([(i, i <= 4 or i > 30) for i in range(1, 41)], None),       # varias páginas seguidas sin una sola fila útil
    ([(i, False) for i in range(1, 16)], None),                  # nada cae dentro del círculo
])
def test_paginacion_no_repite_ni_salta(p, candidatos, tope):
    p["params"]["arcgis"].update(tamano_pagina=5, pausa_paginas_s=0)
    manejador, vistas = _servidor_falso(candidatos, tope)
    feats, llamadas, corte = _consultar(p, manejador)
    esperados = [o for o, ok in candidatos if ok]
    assert [f["attributes"]["OBJECTID"] for f in feats] == esperados and corte == ""
    assert llamadas == len(vistas) and all(q["orderByFields"] == ["objectid"] for q in vistas)


def test_paginacion_reintenta_un_error_pasajero_y_declara_el_corte_si_persiste(p):
    p["params"]["arcgis"].update(tamano_pagina=5, pausa_paginas_s=0)
    p["params"]["red"].update(reintentos=1, espera_base_s=0)
    candidatos = [(i, True) for i in range(1, 21)]
    manejador, _ = _servidor_falso(candidatos, fallas={3})                    # falla una vez a mitad de camino
    feats, _, corte = _consultar(p, manejador, campos="nombre")
    assert len(feats) == 20 and corte == ""
    manejador, _ = _servidor_falso(candidatos, fallas=set(range(3, 200)))     # deja de responder del todo
    feats, _, corte = _consultar(p, manejador, campos="nombre")
    assert len(feats) == 10 and corte.startswith("error_servidor")            # se conserva lo leído y se dice


@pytest.mark.parametrize("candidatos, ilegibles", [
    ([(i, True) for i in range(1, 41)], {17}),                   # una fila con una fecha que el servidor no escribe
    ([(i, True) for i in range(1, 41)], {1, 23, 24, 40}),        # varias, seguidas y en los extremos
    ([(i, i != 17) for i in range(1, 41)], {17}),                # la fila ilegible ni siquiera cae en el círculo
])
def test_una_fila_ilegible_no_tumba_la_capa(p, candidatos, ilegibles):
    p["params"]["arcgis"].update(tamano_pagina=8, pausa_paginas_s=0)
    p["params"]["red"].update(reintentos=0, espera_base_s=0)
    manejador, _ = _servidor_falso(candidatos, ilegibles=ilegibles)
    feats, _, corte = _consultar(p, manejador, campos="nombre,fecha_inicio")
    esperados = [o for o, ok in candidatos if ok]
    assert [f["attributes"]["OBJECTID"] for f in feats] == esperados and corte == ""
    marcadas = {f["attributes"]["OBJECTID"] for f in feats if f["attributes"].get("_solo_llave")}
    assert marcadas == {o for o in ilegibles if o in esperados}  # solo las ilegibles; las vecinas llegan completas


def test_paginacion_respeta_topes(p):
    p["params"]["arcgis"].update(tamano_pagina=5, pausa_paginas_s=0, max_paginas=3)
    manejador, vistas = _servidor_falso([(i, True) for i in range(1, 101)])
    feats, llamadas, corte = _consultar(p, manejador)
    assert llamadas == 3 and len(feats) == 15 and corte == "tope_paginas"
    p["params"]["arcgis"]["max_paginas"] = 80
    feats, _, corte = _consultar(p, _servidor_falso([(i, True) for i in range(1, 101)])[0], max_registros=12)
    assert len(feats) == 12 and corte == "tope_registros"
    feats, _, _ = _consultar(p, manejador, campos="nombre,tipo")
    assert vistas[-1]["outFields"] == ["objectid,nombre,tipo"]     # la llave siempre viaja


# ---------------- depuración de anuncios (paso que va antes del ETL) ----------------
def _anuncio(oferta_id, sitio, area=80, precio=600_000_000, **extra):
    return {"oferta_id": oferta_id, "nk": f"portales:{oferta_id}", "capa": sitio, "operacion": "venta", "tipo_inmueble": "apartamento",
            "area": area, "precio": precio, "barrio": "Laureles", "habitaciones": 3, "banos": 2, "fecha_publicacion": None, **extra}


def test_el_mismo_inmueble_en_dos_sitios_se_enlaza_y_lo_dudoso_no(p):
    from captura.curaduria import anuncios
    cfg = p["almacen"]["bases_mercado"]["portales"]["duplicados_entre_sitios"]
    assert cfg["activa"] and cfg["accion"] == "enlazar"
    tol_a, tol_p = cfg["tolerancia_area_pct"], cfg["tolerancia_precio_pct"]
    base = _anuncio(1, "sitio_a")
    casos = [
        base,
        _anuncio(2, "sitio_b"),                                                      # iguales en otro sitio: repetido, puntaje 1
        _anuncio(3, "sitio_b", area=80 * (1 - tol_a / 2), precio=600_000_000 * (1 + tol_p / 2), barrio="LAURELES "),   # casi iguales
        _anuncio(4, "sitio_a"),                                                      # mismo sitio: pueden ser unidades iguales
        _anuncio(5, "sitio_c", banos=3),                                             # un dato del inmueble no coincide
        _anuncio(6, "sitio_c", barrio=None),                                         # sin barrio no se afirma nada
        _anuncio(7, "sitio_c", barrio="Belén"),                                      # otro barrio
        _anuncio(8, "sitio_c", area=80 * (1 + tol_a * 1.5)),                         # área fuera de la tolerancia
        _anuncio(9, "sitio_c", precio=600_000_000 * (1 + tol_p * 1.5)),              # precio fuera de la tolerancia
        _anuncio(10, "sitio_c", tipo_inmueble="casa"),                               # otro tipo
    ]
    marcas = anuncios.emparejar(casos, cfg)
    assert set(marcas) == {2, 3}
    assert marcas[2] == {"regla_dup": "R3_entre_sitios", "puntaje_dup": 1.0, "coincide_con": "portales:1"}
    assert marcas[3]["coincide_con"] == "portales:1" and 0 < marcas[3]["puntaje_dup"] < 1
    assert anuncios.emparejar(list(reversed(casos)), cfg) == marcas                 # no depende del orden en que llegan
    # se conserva el que trae fecha de publicación, aunque se haya visto después
    con_fecha = [base, _anuncio(2, "sitio_b", fecha_publicacion="2026-10-01")]
    assert anuncios.emparejar(con_fecha, cfg) == {1: {"regla_dup": "R3_entre_sitios", "puntaje_dup": 1.0, "coincide_con": "portales:2"}}
    # el barrio deja de exigirse solo si la regla lo dice
    assert set(anuncios.emparejar([base, _anuncio(6, "sitio_c", barrio=None)], dict(cfg, exigir_mismo_barrio=False))) == {6}


def test_la_resta_de_meses_es_la_misma_que_usa_la_ficha():
    from datetime import date, timedelta

    import pandas as pd

    from captura.curaduria import anuncios
    dia = date(2024, 1, 1)
    while dia < date(2027, 3, 1):                       # incluye fines de mes y un año bisiesto
        for meses in (1, 6, 12):
            assert anuncios.restar_meses(dia, meses) == (pd.Timestamp(dia) - pd.DateOffset(months=meses)).date(), (dia, meses)
        dia += timedelta(days=1)


def test_la_vigencia_de_los_anuncios_se_mide_con_la_regla_de_la_ficha(p):
    from datetime import date

    from captura.curaduria import anuncios
    cfg_v = p["almacen"]["vigencia"]
    hoy = date(2026, 11, 23)
    corte = anuncios.restar_meses(hoy, cfg_v["max_meses_publicacion"]).isoformat()
    lote = [{"fecha_publicacion": corte, "obtenido_en": "2026-11-23T10:00:00+00:00"},          # justo en el borde: vigente
            {"fecha_publicacion": "2025-12-20", "obtenido_en": "2026-11-23T10:00:00+00:00"},  # publicado hace 11 meses
            {"fecha_publicacion": None, "obtenido_en": "2026-11-23T10:00:00+00:00"},          # sin fecha: vale por hoy
            {"fecha_publicacion": None, "obtenido_en": "2025-01-10T10:00:00+00:00"}]          # sin fecha y visto hace casi dos años
    v = anuncios.clasificar_vigencia(lote, cfg_v, hoy)
    assert (v["corte"], v["con_fecha_vigentes"], v["sin_fecha_vigentes"], v["fuera_de_plazo"], v["vigentes"]) == (corte, 1, 1, 2, 2)
    estricta = anuncios.clasificar_vigencia(lote, dict(cfg_v, sin_fecha_publicacion="excluir"), hoy)
    assert estricta["sin_fecha_excluidos"] == 2 and estricta["vigentes"] == 1
