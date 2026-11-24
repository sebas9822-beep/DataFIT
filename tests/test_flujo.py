"""Captura de punta a punta con fuentes simuladas: lo que entra, lo que se guarda y cómo se guarda."""
from __future__ import annotations

import asyncio
import sqlite3

from conftest import CBML, LAT, LNG

from captura import bd, consultas, servicio
from captura.fuentes import base


def capturar(p, **entrada):
    prog = servicio.Progreso()
    datos = {"direccion": "Circular 2 # 71-62", "lat": LAT, "lng": LNG, "origen_coordenada": "pin_usuario",
             "curar_con_claude": False}
    datos.update(entrada)
    cid = asyncio.run(servicio.capturar(datos, p, prog))
    return cid, prog


def con(p):
    c = sqlite3.connect(p["rutas"]["bd"])
    c.row_factory = sqlite3.Row
    return c


def test_captura_completa_solo_fuentes_abiertas(p, mundo):
    cid, prog = capturar(p)
    r = consultas.leer_captura(p, cid)
    assert r["estado"] == "ok" and all(x["estado"] in ("ok", "omitido") for x in prog.pasos.values())

    # --- predio y norma (capa oficial) ---
    pr = r["predio"]
    assert pr["cbml"] == CBML and pr["barrio_nombre"] == "Bolivariana" and pr["comuna_nombre"] == "Laureles"
    assert pr["es_ph"] == 1 and pr["pisos_max"] == 12 and pr["area_construida"] == 3140.5
    assert pr["pot_tratamiento"] == "Consolidación Nivel 4" and pr["pot_codigo"] == "Z4_CN4_12"
    assert pr["estado_norma"] == "ok" and pr["zge_valor_m2"] == 3200210 and pr["tipo_valor_zge"] == "referencia_zgh"
    assert pr["via_cercana"] == "CQ 2" and pr["placa_cercana"] == "71-62" and 20 < pr["dist_placa_m"] < 25
    assert pr["estacion_masiva"] == "Estadio" and 690 < pr["estacion_masiva_m"] < 710
    assert pr["cuadrante_policia"] == "CAI LA CASTELLANA"
    assert r["punto"]["direccion_catastral"] == "CQ 2 # 71-62" and r["punto"]["precision"] == "P2"

    # --- conjunto: gana el nombre oficial del mismo lote ---
    assert r["conjunto"] == {"nombre": "Edificio Los Almendros", "tipo": "edificio", "origen": "catastro"}

    c = con(p)
    items = {f"{x['fuente']}:{x['capa']}:{x['id_externo']}": x for x in c.execute("SELECT * FROM entorno_items")}
    # --- nada fuera del radio; lo descartado queda contado en la ejecución ---
    assert all(x["distancia_m"] <= 1000 for x in items.values() if x["distancia_m"] is not None)
    assert "geomedellin:equipamientos:1:14" not in items and "osm:overpass:node/4" not in items
    ej = {(x["fuente"], x["capa"]): x for x in c.execute("SELECT * FROM ejecuciones")}
    assert ej[("geomedellin", "equipamientos")]["descartados"] == 1 and ej[("osm", "overpass")]["descartados"] == 1
    assert all(x["estado"] == "ok" for k, x in ej.items() if k[0] != "google")
    assert ej[("google", "places_nearby")]["estado"] == "omitida"            # sin llave: se declara, no se oculta

    # --- clasificación ---
    assert items["geomedellin:equipamientos:1:11"]["categoria"] == "seguridad"
    assert items["geomedellin:equipamientos:1:11"]["subtipo"] == "policia"
    assert items["geomedellin:equipamientos:1:11"]["nombre"] == "CAI La Castellana"
    assert items["geomedellin:equipamientos:1:12"]["categoria"] == "educacion"
    raro = items["geomedellin:equipamientos:1:13"]
    assert raro["categoria"] == "otros_equipamientos" and "clasificacion_dudosa" in raro["banderas_json"]
    assert items["geomedellin:espacio_publico:2:21"]["subtipo"] == "parque_recreativo"
    assert items["geomedellin:estaciones_masivo:11:1"]["subtipo"] == "metro"
    assert items["osm:overpass:node/3"]["categoria"] == "empresas"
    assert "osm:overpass:node/5" not in items                                   # fuera de la taxonomía

    # --- normalización con bitácora ---
    assert items["geomedellin:bibliotecas:0:61"]["nombre"] == "Biblioteca Publica Piloto"
    assert items["geomedellin:bibliotecas:0:61"]["nombre_original"] == "biblioteca publica piloto"
    log = c.execute("SELECT * FROM curaduria_log WHERE item_ref='geomedellin:bibliotecas:0:61'").fetchone()
    assert log["valor_antes"] == "biblioteca publica piloto" and log["origen"] == "regla"

    # --- paraderos: una fila por paradero aunque la capa traiga una por ruta ---
    assert sum(1 for k in items if k.startswith("geomedellin:paradas_bus")) == 2
    assert "2 filas repetidas" not in (ej[("geomedellin", "paradas_bus")]["mensaje"] or "")
    assert "1 filas repetidas" in ej[("geomedellin", "paradas_bus")]["mensaje"]

    # --- duplicados entre fuentes: se enlazan, no se borran ---
    inem = [x for x in items.values() if "inem" in (x["nombre_clave"] or "")]
    assert len(inem) == 3 and len({x["entidad_id"] for x in inem}) == 1
    assert sum(x["es_maestro"] for x in inem) == 1
    maestro = next(x for x in inem if x["es_maestro"])
    assert maestro["fuente"] == "geomedellin" and all(x["coincide_con"] for x in inem if not x["es_maestro"])
    almendros = [x for x in items.values() if "almendros" in (x["nombre_clave"] or "")]
    assert len(almendros) == 2 and len({x["entidad_id"] for x in almendros}) == 1
    rev = [dict(x) for x in c.execute("SELECT * FROM cola_revision")]
    assert [x["par"] for x in rev] == ["catastro:toponimia:0:502|osm:overpass:node/8"] and rev[0]["decision"] is None

    # --- capas masivas: se guardan completas y se resumen aparte ---
    est = [x for x in items.values() if x["capa"] == "establecimientos"]
    assert len(est) == 4 and all(x["en_mapa"] == 0 for x in est)                 # ni repetidos ni faltantes al paginar
    sin_local = items["registro_ic:establecimientos:6:A1"]
    assert sin_local["motivo_exclusion"] == "sin_establecimiento_fisico" and sin_local["nombre"] is None
    assert items["registro_ic:establecimientos:6:A2"]["categoria"] == "salud"
    assert items["registro_ic:establecimientos:6:A4"]["categoria"] == "abastecimiento"
    assert ej[("registro_ic", "establecimientos")]["llamadas"] == 4              # 6 candidatos del servidor, de a 2
    ind = r["indicadores"]
    assert ind["establecimientos_activos"]["valor"] == 3
    assert ind["establecimientos_activos"]["detalle"]["sin_establecimiento_fisico"] == 1
    assert ind["camaras_cctv"]["valor"] == 2

    # --- indicadores sobre lugares únicos (los duplicados cuentan una vez) ---
    assert ind["conteo_educacion_1000m"]["valor"] == 4          # INEM (3 fuentes = 1) + 2 en revisión + 1 solo
    assert ind["conteo_seguridad_1000m"]["valor"] == 1          # las cámaras no inflan la categoría
    assert ind["cobertura_basica_educacion"]["valor"] == 1
    assert ind["estacion_masiva_mas_cercana_m"]["detalle"]["dentro_del_radio"] is True
    # Haversine (esfera) y EPSG:9377 (elipsoide) difieren unos metros a 1 km: se reporta, no se oculta.
    assert 0 < ind["control_distancia_9377_max_dif_m"]["valor"] < 6.0
    cat = {x["id"]: x for x in r["categorias"]}
    assert cat["seguridad"]["lugares"] == 1 and cat["residencial"]["lugares"] == 1
    assert r["base_indicadores"] == "abiertas" and r["llamadas_google"] == 0

    # --- ofertas OIME: precio pedido, sin datos personales, lo inválido con motivo ---
    of = {x["id_externo"]: dict(x) for x in c.execute("SELECT * FROM ofertas")}
    assert len(of) == 6 and all(x["tipo_precio"] == "pedido" for x in of.values())
    assert of["1:1001"]["precio_m2"] == 7_500_000 and of["1:1001"]["area_tipo_usada"] == "privada"
    assert of["1:1004"]["motivo_exclusion"] == "sin_precio" and of["1:1006"]["motivo_exclusion"] == "sin_area"
    assert of["1:1005"]["operacion"] == "arriendo"
    mediana = ind["oferta_pedido_m2_mediana_apartamento"]
    assert mediana["valor"] == 7_500_000 and mediana["detalle"]["n"] == 3 and mediana["detalle"]["tipo_precio"] == "pedido"
    volcado = " ".join(str(v) for fila in c.execute("SELECT * FROM ofertas") for v in tuple(fila))
    volcado += " ".join(str(v) for fila in c.execute("SELECT * FROM entorno_items") for v in tuple(fila))
    for prohibido in ("3001234567", "Inmobiliaria X", "Llamar al", "6041234567", "6042222222", "x@y.co"):
        assert prohibido not in volcado, prohibido

    # --- invariante: nada se pierde ---
    total = c.execute("SELECT COUNT(*) FROM entorno_items").fetchone()[0]
    del_barrido = sum(x["registros"] for k, x in ej.items() if not k[1].startswith("punto_") and k[1] != "oferta_oime")
    assert total == del_barrido == r["curaduria"]["registros"] and r["curaduria"]["enlazados"] == 3
    assert r["curaduria"]["en_revision"] == 1 and r["curaduria"]["por_claude"] == 0
    assert any("OpenStreetMap" in a for a in r["atribuciones"]) and any("Catastro" in a for a in r["atribuciones"])
    assert "inversión" in r["aviso_alcance"]


def test_reuso_y_captura_nueva(p, mundo):
    cid, _ = capturar(p)
    n = len(mundo.solicitudes)
    cid2, prog2 = capturar(p)
    assert cid2 == cid and prog2.reusada and len(mundo.solicitudes) == n      # cero llamadas nuevas
    cid3, prog3 = capturar(p, reusar=False)
    assert cid3 != cid and not prog3.reusada


def test_geocodificacion_oficial_p1_y_p2(p, mundo):
    async def geo_dir(texto):
        async with base.nuevo_cliente(p) as cli:
            return await servicio.geocodificar(cli, p, texto)
    exacta = asyncio.run(geo_dir("Carrera 73 # 2-18, Medellín"))
    assert exacta["ok"] and exacta["precision"] == "P1" and exacta["cbml"] == "11070100006"
    assert exacta["direccion_catastral"] == "CR 73 # 2-18" and exacta["origen"] == "catastro"
    cercana = asyncio.run(geo_dir("Cra 73 No. 2-21 apto 502"))
    assert cercana["precision"] == "P2" and "2-20" in cercana["metodo"]
    con_apendice = asyncio.run(geo_dir("Cra 73 # 2A-30"))
    assert con_apendice["precision"] == "P1" and con_apendice["cbml"] == "11070100009"
    libre = asyncio.run(geo_dir("Por el parque de Laureles"))                  # cae a Nominatim: P3 y aviso
    assert libre["ok"] and libre["precision"] == "P3" and libre["origen"] == "nominatim" and libre["avisos"]


def test_direccion_sin_coordenadas_usa_la_nomenclatura_oficial(p, mundo):
    cid, _ = capturar(p, direccion="Carrera 73 # 2-18", lat=None, lng=None, origen_coordenada=None)
    r = consultas.leer_captura(p, cid)
    assert r["punto"]["origen"] == "catastro" and r["punto"]["precision"] == "P1"
    assert r["punto"]["coord_expira_en"] is None and r["punto"]["direccion_catastral"] == "CR 73 # 2-18"


def test_fuente_caida_no_tumba_la_captura_ni_se_disfraza_de_cero(p, mundo):
    mundo.caidas = {"overpass", "VC_Sedes"}
    cid, prog = capturar(p)
    r = consultas.leer_captura(p, cid)
    assert r["estado"] == "parcial" and prog.pasos["fuentes"]["estado"] == "parcial"
    ej = {(x["fuente"], x["capa"]): x["estado"] for x in r["ejecuciones"]}
    assert ej[("osm", "overpass")] == "error" and ej[("geomedellin", "sedes_educativas")] == "error"
    assert ej[("geomedellin", "equipamientos")] == "ok"
    assert sum("no significa que no existan" in a for a in r["avisos"]) == 3   # sedes, superior (mismo servicio) y OSM
    assert r["predio"]["cbml"] == CBML


def test_fuente_bloqueada_se_detiene_sin_reintentar(p, mundo):
    p["params"]["red"]["reintentos"] = 3
    mundo.bloqueadas = {"VM_Red_Bibliotecas"}
    cid, _ = capturar(p)
    r = consultas.leer_captura(p, cid)
    ej = {(x["fuente"], x["capa"]): x for x in r["ejecuciones"]}
    assert ej[("geomedellin", "bibliotecas")]["estado"] == "bloqueado"
    assert sum("VM_Red_Bibliotecas" in u for u in mundo.solicitudes) == 1     # un solo intento


def test_fuera_de_medellin_no_inventa_predio(p, mundo):
    cid, prog = capturar(p, lat=6.1720, lng=-75.6090, direccion="Itagüí")
    r = consultas.leer_captura(p, cid)
    assert r["punto"]["en_cobertura"] == 0 and r["predio"]["estado_norma"] == "pendiente_fuente_municipal"
    assert r["predio"]["cbml"] is None and prog.pasos["predio"]["estado"] == "omitido"
    assert any("fuera de Medellín" in a for a in r["avisos"])
    assert any(e["estado"] == "omitida" for e in r["ejecuciones"])


def test_google_nombres_solo_en_sesion_y_purga_a_30_dias(p, mundo):
    p["secretos"]["google_servidor"] = "LLAVE_FALSA"
    mundo.google_satura = True
    cid, _ = capturar(p, origen_coordenada="google_autocomplete", direccion="Sin nomenclatura")
    r = consultas.leer_captura(p, cid)
    grupos = sum(len(g) for g in p["taxonomia"]["google"].values())
    assert grupos == 10 and r["llamadas_google"] == grupos + 7 + 1             # +7 subcírculos, +1 conjunto
    assert r["punto"]["coord_expira_en"] and r["punto"]["precision"] == "P3"
    c = con(p)
    g = [dict(x) for x in c.execute("SELECT * FROM entorno_items WHERE fuente='google'")]
    assert len(g) == 20 + 7 * 3 + 1 and all(x["nombre"] is None and x["nombre_original"] is None for x in g)
    assert all(x["nombre_clave"] is None and x["categoria_original"] is None and x["expira_en"] for x in g)
    assert not any(x["id_externo"] == "G_LEJOS" for x in g)
    en_respuesta = [x for x in r["items"] if x["fuente"] == "google" and x["nombre"]]
    assert en_respuesta                                                        # el nombre viaja solo en la sesión
    cole = next(x for x in g if x["id_externo"] == "G_COLE")
    assert cole["es_maestro"] == 0 and cole["coincide_con"].startswith("geomedellin")   # enlazado con la fuente oficial
    assert r["base_indicadores"] == "todas"
    assert c.execute("SELECT COUNT(*) FROM indicadores WHERE base='todas' AND expira_en IS NULL").fetchone()[0] == 0

    # Otra sesión (servidor reiniciado): los nombres de Google ya no existen en ningún lado.
    servicio.NOMBRES_SESION.clear()
    r2 = consultas.leer_captura(p, cid)
    assert not [x for x in r2["items"] if x["fuente"] == "google" and x["nombre"]]

    # Purga: vencidos pierden coordenadas; se conservan place_id y categoría propia.
    c.execute("UPDATE entorno_items SET expira_en='2000-01-01' WHERE fuente='google'")
    c.execute("UPDATE capturas SET coord_expira_en='2000-01-01'")
    c.execute("UPDATE resumen_categoria SET expira_en='2000-01-01' WHERE expira_en IS NOT NULL")
    c.execute("UPDATE indicadores SET expira_en='2000-01-01' WHERE expira_en IS NOT NULL")
    c.commit()
    hecho = bd.purgar_expirados(p)
    assert hecho["items_depurados"] == len(g) and hecho["capturas_depuradas"] == 1 and hecho["indicadores_borrados"] > 0
    despues = [dict(x) for x in c.execute("SELECT * FROM entorno_items WHERE fuente='google'")]
    assert all(x["lat"] is None and x["subtipo"] is None and x["id_externo"] and x["categoria"] for x in despues)
    assert c.execute("SELECT COUNT(*) FROM entorno_items WHERE fuente!='google' AND lat IS NULL AND motivo_exclusion IS NULL").fetchone()[0] == 0
    assert c.execute("SELECT lat FROM capturas").fetchone()[0] is None
    assert servicio.buscar_reciente(p, LAT, LNG, 1000) is None                 # ya no se puede reusar
    assert consultas.leer_captura(p, cid)["base_indicadores"] == "abiertas"


def test_google_con_direccion_oficial_adopta_el_punto_del_catastro(p, mundo):
    p["secretos"]["google_servidor"] = "LLAVE_FALSA"
    cid, _ = capturar(p, origen_coordenada="google_autocomplete", direccion="Carrera 73 # 2-18", place_id="PID")
    r = consultas.leer_captura(p, cid)
    assert r["punto"]["origen"] == "catastro" and r["punto"]["precision"] == "P1" and r["punto"]["coord_expira_en"] is None
    assert any("nomenclatura oficial" in a for a in r["avisos"])
    assert con(p).execute("SELECT google_place_id FROM capturas").fetchone()[0] == "PID"


def test_conjunto_escrito_por_el_usuario_manda(p, mundo):
    cid, _ = capturar(p, conjunto_nombre="  Torres de la Bolivariana  ")
    assert consultas.leer_captura(p, cid)["conjunto"] == {"nombre": "Torres de la Bolivariana", "tipo": None, "origen": "usuario"}


def test_exportaciones(p, mundo):
    cid, _ = capturar(p)
    csv = consultas.exportar_csv(p, cid, "entorno")
    assert csv.startswith("﻿item_id,captura_id,fuente") and "Biblioteca Publica Piloto" in csv
    assert consultas.exportar_csv(p, cid, "ofertas").count("\n") == 7
    assert consultas.exportar_csv(p, cid, "capturas") is None                  # tabla no permitida
    assert [x["captura_id"] for x in consultas.listar_capturas(p)] == [cid]
    assert consultas.leer_captura(p, "00000000-0000-0000-0000-000000000000") is None
