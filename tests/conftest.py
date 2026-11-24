"""Infraestructura de pruebas: base temporal y fuentes simuladas (sin salir a la red).

El transporte simulado responde como el servidor de mapas de la Alcaldía, Overpass, Nominatim y
Google. Los datos reproducen las formas reales verificadas el 20-nov-2026 (nombres de campo incluidos).
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

from captura import config, geo, servicio  # noqa: E402
from captura.curaduria import claude_cliente  # noqa: E402
from captura.fuentes import base  # noqa: E402

LAT, LNG = 6.2450, -75.5905          # Bolivariana (Laureles)
CBML = "11070030006"


def pt(dist_m: float, rumbo: float) -> tuple[float, float]:
    return geo.desplazar(LAT, LNG, dist_m, rumbo)


def punto(attrs: dict, dist_m: float, rumbo: float) -> dict:
    la, ln = pt(dist_m, rumbo)
    return {"attributes": dict(attrs, latitud=la, longitud=ln), "geometry": {"x": ln, "y": la}}


def poligono(attrs: dict, dist_m: float, rumbo: float, lado_m: float = 40) -> dict:
    la, ln = pt(dist_m, rumbo)
    d = lado_m / 2 / 111_320
    anillo = [[ln - d, la - d], [ln + d, la - d], [ln + d, la + d], [ln - d, la + d], [ln - d, la - d]]
    return {"attributes": attrs, "geometry": {"rings": [anillo]}}


def paginar_como_servidor(candidatos: list[tuple[int, dict | None]], q: dict) -> dict:
    """Paginación del servidor de mapas real: `resultOffset` y `resultRecordCount` cuentan candidatos
    (antes del filtro exacto de distancia), no filas devueltas."""
    donde = q.get("where", ["1=1"])[0]
    m = re.search(r"objectid > (\d+)", donde)
    resto = [(o, f) for o, f in candidatos if o > (int(m[1]) if m else 0)]
    ini = int(q.get("resultOffset", ["0"])[0])
    tam = int(q.get("resultRecordCount", [str(len(resto) or 1)])[0])
    return {"features": [f for _, f in resto[ini:ini + tam] if f], "exceededTransferLimit": ini + tam < len(resto)}


class Mundo:
    """Estado de las fuentes simuladas; cada prueba puede alterarlo."""

    def __init__(self) -> None:
        self.caidas: set[str] = set()           # fragmentos de URL que responden 503
        self.bloqueadas: set[str] = set()       # fragmentos de URL que responden 403
        self.solicitudes: list[str] = []
        self.google_satura = False

    # ---------------- ArcGIS ----------------
    def arcgis(self, servicio_capa: str, q: dict) -> dict:
        donde = q.get("where", ["1=1"])[0]
        con_punto = "geometry" in q
        con_dist = "distance" in q
        fuera = False
        if con_punto:
            ln, la = (float(x) for x in q["geometry"][0].split(","))
            fuera = geo.haversine_m(la, ln, LAT, LNG) > 3000
        if fuera:
            return {"features": []}
        f = []
        if servicio_capa == "ServiciosCatastro/Base_Catastral/1":
            f = [{"attributes": {"objectid": 1, "comuna": "11", "barrio": "07", "codigo": "1107",
                                 "nombre_barrio": "Bolivariana", "nombre_comuna": "LAURELES"}}]
        elif servicio_capa == "ServiciosCatastro/ConsultaOperadorCatastral/7":
            f = [{"attributes": {"objectid": 9, "cbml": CBML, "tipo_lote": "E", "area_lote": 417.0,
                                 "condicion_predio": 9, "juridica_predio": "PH", "subtipo_lote": 1}}]
        elif servicio_capa == "ServiciosCatastro/Base_Catastral/5":
            f = [{"attributes": {"numero_pisos": 12.0, "numero_sotanos": 1.0, "area_construida": 3100.5}},
                 {"attributes": {"numero_pisos": 1.0, "numero_sotanos": 0.0, "area_construida": 40.0}}]
        elif servicio_capa == "ServiciosCatastro/Base_Catastral/12":
            f = [{"attributes": {"comuna": "11", "valor_m2": 3200210, "cod_valor": 895, "vigencia": 2026}}]
        elif servicio_capa == "ordenamiento_ter/VM_22_Tratamientos_Urbanos/0":
            f = [{"attributes": {"codigo_tramiento": "Z4_CN4_12", "tratamiento": "Consolidación Nivel 4",
                                 "tipo": "CN4", "categoria_tratamiento": "Consolidación", "clase_suelo": "Urbano"}}]
        elif servicio_capa == "ordenamiento_ter/VM_23_Uso_General_Suelo_Urbano/2":
            f = [{"attributes": {"areagraluso": "Áreas y corredores de media mixtura"}}]
        elif servicio_capa == "seguridad_defensa/VM_Policia_MAP5/2":
            f = [{"attributes": {"distrito": "CAI LA CASTELLANA", "estacion": "LAURELES"}}]
        elif servicio_capa == "ServiciosCatastro/ConsultaOperadorCatastral/1":
            if con_dist:
                f = [punto({"objectid": 1, "cbml": CBML, "via": "CQ 2", "placa": "71-62", "numero_placa": "62",
                            "codigo_postal": "050031"}, 22, 300),
                     punto({"objectid": 2, "cbml": CBML, "via": "CQ 2", "placa": "71-68", "numero_placa": "68",
                            "codigo_postal": "050031"}, 35, 300)]
            elif "numero_via=73" in donde and "tipo_via='CR'" in donde:
                la, ln = pt(120, 250)
                comun = {"apendice_via": "", "orientacion_via": "", "apendice_cruce": "", "orientacion_cruce": None,
                         "via": "CR 73", "codigo_postal": "050031"}
                f = [{"attributes": dict(comun, cbml="11070100006", placa="2-18", numero_placa="18", latitud=la, longitud=ln)},
                     {"attributes": dict(comun, cbml="11070100006", placa="2-20", numero_placa="20", latitud=la + 1e-5, longitud=ln)},
                     {"attributes": dict(comun, cbml="11070100009", placa="2A-30", numero_placa="30", apendice_cruce="A",
                                         latitud=la, longitud=ln)}]
        elif servicio_capa == "ServiciosCatastro/ConsultaOperadorCatastral/0":      # toponimia
            if "cbml=" in donde:
                f = [punto({"objectid": 501, "nombre": "EDIFICIO LOS ALMENDROS", "tipo_sitio": "01", "cbml": CBML}, 8, 10)]
            else:
                f = [punto({"objectid": 501, "nombre": "EDIFICIO LOS ALMENDROS", "tipo_sitio": "01", "cbml": CBML}, 8, 10),
                     punto({"objectid": 502, "nombre": "COLEGIO SAN JOSE", "tipo_sitio": "04", "cbml": "11070020001"}, 410, 95),
                     punto({"objectid": 503, "nombre": "SITIO RARO", "tipo_sitio": "46", "cbml": "11070020002"}, 500, 200)]
                if con_dist and float(q["distance"][0]) < 200:
                    f = f[:1]
        elif servicio_capa == "transporte/VC_Infraest_Gestion_Transporte/11":
            f = [punto({"objectid": 1, "nombre": "Estadio", "linea": "Línea B", "estado": 1, "tipo": 1, "tipo_est": "Estación"}, 700, 20),
                 punto({"objectid": 2, "nombre": "Floresta", "linea": "Línea B", "estado": 1, "tipo": 1, "tipo_est": "Estación"}, 1900, 300)]
            if con_dist:
                f = f[:1]
        elif servicio_capa == "mapas_nacionales/VC_Infraestructura_Fisica/1":
            f = [poligono({"objectid": 11, "nombre": "CAI LA CASTELLANA", "tipo": "Equipamientos de Policía", "categoria": 3,
                           "fecha_actualizacion": 1700000000000}, 300, 0),
                 poligono({"objectid": 12, "nombre": "INSTITUCION EDUCATIVA INEM JOSE FELIX DE RESTREPO", "tipo": "Educación básica",
                           "categoria": 1, "fecha_actualizacion": 1262304000000}, 600, 90, 120),
                 poligono({"objectid": 13, "nombre": "CENTRO RARO", "tipo": "Tipo que no existe", "categoria": 1}, 450, 180),
                 poligono({"objectid": 14, "nombre": "LEJOS", "tipo": "Educación básica", "categoria": 1}, 1400, 45)]
        elif servicio_capa == "mapas_nacionales/VC_Infraestructura_Fisica/2":
            f = [poligono({"objectid": 21, "nombre": "PARQUE DE LA BOLIVARIANA", "funcion": "Parque Recreativo"}, 250, 135, 60)]
        elif servicio_capa == "educacion/VC_Sedes/0":
            f = [punto({"objectid": 31, "sede_educa": "Institucion Educativa Inem Jose Felix de Restrepo", "nombre_est": "INEM",
                        "sector": "Oficial", "clasificac": "Oficial-Principal", "telefono": "6041234567", "estado": "Activo"}, 610, 90)]
        elif servicio_capa == "transporte/VC_Infraest_Gestion_Transporte/0":
            f = [punto({"objectid": 41, "id_paradero": 52057, "id_ruta": 1, "direccion": "CR 70 x CL 47D", "nombre_ruta": "R1"}, 350, 10),
                 punto({"objectid": 42, "id_paradero": 52057, "id_ruta": 2, "direccion": "CR 70 x CL 47D", "nombre_ruta": "R2"}, 350, 10),
                 punto({"objectid": 43, "id_paradero": 52058, "id_ruta": 1, "direccion": "CR 70 x CL 44", "nombre_ruta": "R1"}, 500, 15)]
        elif servicio_capa == "seguridad_defensa/VM_Policia_MAP5/10":
            f = [punto({"objectid": 51, "id_camara": 3144, "estado": "Buen Estado", "tipo_camara": "PTZ"}, 200, 30),
                 punto({"objectid": 52, "id_camara": 3145, "estado": "Buen Estado", "tipo_camara": "PTZ"}, 800, 200)]
        elif servicio_capa == "cultura/VM_Red_Bibliotecas/0":
            f = [punto({"objectid": 61, "nombre": "biblioteca publica piloto", "tipo": "Biblioteca", "telefono": "6042222222",
                        "correo": "x@y.co"}, 900, 60)]
        elif servicio_capa == "ccio_ind_turism/VA_Establecimiento_Comercial/6":
            # Como el servidor real: cada página toma N candidatos (los del "sobre" del círculo) y solo
            # devuelve los que pasan el filtro exacto. None = candidato que ocupa lugar pero no vuelve.
            todos = [(71, punto({"objectid": 71, "id_contrato": "A1", "nombre_establecimiento": "NO POSEE ESTABLECIMIENTOS",
                                 "codigociiu": "7410", "grupo_actividad": "03"}, 100, 0)),
                     (72, None),
                     (73, punto({"objectid": 73, "id_contrato": "A2", "nombre_establecimiento": "CLINICA DENTAL SONRISA",
                                 "codigociiu": "8622", "grupo_actividad": "03"}, 150, 90)),
                     (74, punto({"objectid": 74, "id_contrato": "A3", "nombre_establecimiento": "YOLO CAFFEE",
                                 "codigociiu": "5613", "grupo_actividad": "03"}, 220, 180)),
                     (75, None),
                     (76, punto({"objectid": 76, "id_contrato": "A4", "nombre_establecimiento": "SUPERMERCADO LA 70",
                                 "codigociiu": "4711", "grupo_actividad": "02"}, 300, 270))]
            return paginar_como_servidor(todos, q)
        elif servicio_capa == "vivienda_ciudad_terri/VM_Oferta_Comercial_Oime/1":
            def oferta(i, precio, area, dist, tipo="SELL", **extra):
                return punto(dict({"objectid": i, "id": 1000 + i, "commercial_type": tipo, "tipo_inmueble": "Apartamento",
                                   "status": "USADO", "stratum": 5, "private_area_m2": area, "total_area_m2": 0,
                                   "price": precio, "bedrooms_number": 3, "bathrooms_number": 2, "parking_number": 1,
                                   "link": f"https://ejemplo.invalid/{i}", "year": 2023, "created_at": 1680000000000,
                                   "source_name": "ORIGEN 1", "phone": 3001234567, "agency": "Inmobiliaria X",
                                   "description": "Llamar al 300 123 4567"}, **extra), dist, 40 * i)
            f = [oferta(1, 600_000_000, 80, 200), oferta(2, 700_000_000, 100, 400), oferta(3, 480_000_000, 60, 600),
                 oferta(4, 0, 70, 300), oferta(5, 2_500_000, 70, 500, tipo="RENT"), oferta(6, 650_000_000, 0, 350)]
        return {"features": f}

    # ---------------- Overpass ----------------
    def overpass(self) -> dict:
        def nodo(i, dist, rumbo, tags):
            la, ln = pt(dist, rumbo)
            return {"type": "node", "id": i, "lat": la, "lon": ln, "tags": tags}
        la, ln = pt(260, 135)
        return {"elements": [
            nodo(1, 615, 90, {"amenity": "school", "name": "INEM José Félix de Restrepo", "phone": "123"}),
            {"type": "way", "id": 2, "center": {"lat": la, "lon": ln}, "tags": {"leisure": "park"}},
            nodo(3, 150, 60, {"office": "company", "name": "Oficina X"}),
            nodo(4, 1500, 0, {"amenity": "police"}),                           # fuera del radio
            nodo(5, 100, 100, {"highway": "crossing"}),                        # no pertenece a la taxonomía
            nodo(6, 30, 20, {"building": "apartments", "name": "Edificio Los Almendros"}),
            nodo(7, 520, 185, {"amenity": "school", "name": "Colegio Nuevo Horizonte"}),
            nodo(8, 420, 100, {"amenity": "school", "name": "Colegio San José de la Salle"}),
            {"type": "area", "id": 3600001, "tags": {"landuse": "residential", "name": "Urbanización Bolivariana"}},
        ]}

    # ---------------- Google ----------------
    def google(self, cuerpo: dict) -> dict:
        tipos = cuerpo["includedTypes"]
        c = cuerpo["locationRestriction"]["circle"]
        principal = abs(c["center"]["latitude"] - LAT) < 1e-9 and c["radius"] >= 999

        def lugar(pid, nombre, dist, rumbo, tipo):
            la, ln = pt(dist, rumbo)
            return {"id": pid, "displayName": {"text": nombre}, "location": {"latitude": la, "longitude": ln},
                    "types": [tipo], "primaryType": tipo}
        if "apartment_complex" in tipos:
            return {"places": [lugar("G_CONJ", "Conjunto Los Almendros P.H.", 12, 0, "apartment_complex")]}
        if "pharmacy" in tipos:
            if self.google_satura and principal:
                return {"places": [lugar(f"G_F{i}", f"Farmacia {i}", 40 * i + 5, 17 * i, "pharmacy") for i in range(20)]}
            if self.google_satura:
                k = f"{c['center']['latitude']:.5f}_{c['center']['longitude']:.5f}"
                return {"places": [lugar(f"G_FS_{k}_{j}", "F", 0, 0, "pharmacy") | {
                    "location": {"latitude": c["center"]["latitude"], "longitude": c["center"]["longitude"] + j * 1e-4}}
                    for j in range(3)] + [lugar("G_F0", "Farmacia 0", 5, 0, "pharmacy")]}
            return {"places": [lugar("G_F1", "Farmacia Pasteur", 180, 45, "pharmacy")]}
        if "school" in tipos:
            return {"places": [lugar("G_COLE", "INEM José Félix de Restrepo", 612, 90, "school"),
                               lugar("G_LEJOS", "Colegio Lejano", 1300, 90, "school")]}
        return {}


def crear_manejador(mundo: Mundo):
    def manejador(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        mundo.solicitudes.append(url)
        if any(x in url for x in mundo.caidas):
            return httpx.Response(503, text="Service Unavailable")
        if any(x in url for x in mundo.bloqueadas):
            return httpx.Response(403, text="Forbidden")
        u = urlparse(url)
        if "servidormapas" in url:
            partes = u.path.split("/rest/services/")[1].split("/MapServer/")
            clave = f"{partes[0]}/{partes[1].split('/')[0]}"
            return httpx.Response(200, json=mundo.arcgis(clave, parse_qs(u.query)))
        if "overpass" in url:
            assert "VentureOne-CapturaEntorno" in req.headers.get("User-Agent", "")
            consulta = parse_qs(req.content.decode())["data"][0]
            assert "out center tags;" in consulta and "around:1010," in consulta
            return httpx.Response(200, json=mundo.overpass())
        if "nominatim" in url:
            return httpx.Response(200, json=[{"lat": str(LAT), "lon": str(LNG), "display_name": "Bolivariana, Medellín"}])
        if "places.googleapis.com" in url:
            assert req.headers["X-Goog-FieldMask"].startswith("places.id")
            return httpx.Response(200, json=mundo.google(json.loads(req.content)))
        if "maps.googleapis.com" in url:
            return httpx.Response(200, json={"status": "OK", "results": [{
                "formatted_address": "Cra. 73 #2-18, Medellín", "place_id": "GEO_PID",
                "geometry": {"location": {"lat": LAT, "lng": LNG}, "location_type": "ROOFTOP"}}]})
        return httpx.Response(404)
    return manejador


@pytest.fixture
def mundo():
    m = Mundo()
    base.TRANSPORTE = httpx.MockTransport(crear_manejador(m))
    yield m
    base.TRANSPORTE = None


@pytest.fixture
def p(tmp_path, monkeypatch):
    """Configuración real con base temporal, sin esperas y sin Claude ni Google (salvo que la prueba los active)."""
    conf = copy.deepcopy(config.cargar())
    conf["rutas"]["bd"] = tmp_path / "captura.db"
    conf["rutas"]["almacen"] = tmp_path / "almacen.db"
    conf["rutas"]["fichas"] = tmp_path / "fichas_clientes"
    conf["rutas"]["trabajo_claude"] = tmp_path / "claude"
    conf["rutas"]["config"] = tmp_path / "config"          # el administrador escribe aquí: nunca en la configuración real
    conf["rutas"]["respaldos"] = tmp_path / "respaldos"
    # Las pruebas de la captura (Momento 1) no generan ficha ni exigen los datos del inmueble;
    # las de integración (test_integracion.py) vuelven a encender ambas reglas.
    conf["reglas"]["fichas"]["generar_al_cotizar"] = False
    conf["reglas"]["inmueble"]["exigir_al_cotizar"] = False
    conf["reglas"]["busqueda"]["exigir_al_cotizar"] = False
    conf["reglas"]["clientes"]["celular"]["obligatorio"] = False
    conf["params"]["rpa"]["activo"] = False
    # Catálogo corto y fijo: las pruebas no dependen de la lista real de inmobiliarias (se revisa aparte).
    conf["inmobiliarias"] = {"descartadas": [], "sitios": [
        {"id": "fincaraiz", "nombre": "Fincaraíz", "dominio": "fincaraiz.com.co", "tipo": "portal", "nivel": "A", "permitida": True,
         "por_defecto": True, "busqueda": "https://www.fincaraiz.com.co/venta/apartamentos/laureles/medellin"},
        {"id": "metrocuadrado", "nombre": "Metrocuadrado", "dominio": "metrocuadrado.com", "tipo": "portal", "nivel": "A",
         "permitida": True, "por_defecto": True},
        {"id": "ciencuadras", "nombre": "Ciencuadras", "dominio": "ciencuadras.com", "tipo": "portal", "nivel": "A",
         "permitida": False, "por_defecto": False, "nota": "Sus términos prohíben robots y bases derivadas: no se consulta"},
        {"id": "habi", "nombre": "Habi", "dominio": "habi.co", "tipo": "portal", "nivel": "A", "permitida": True, "por_defecto": False}]}
    conf["_ficha"] = None
    conf["params"]["red"].update(reintentos=0, espera_base_s=0)
    conf["params"]["arcgis"].update(pausa_paginas_s=0, tamano_pagina=2)   # página mínima: obliga a paginar
    conf["params"]["claude"]["proveedor"] = "ninguno"
    conf["secretos"] = {"google_servidor": "", "google_navegador": "", "google_map_id": "DEMO_MAP_ID", "anthropic": ""}
    servicio.NOMBRES_SESION.clear()
    monkeypatch.setattr(config, "cargar", lambda: conf)
    from captura import bd
    bd.iniciar(conf)
    return conf


@pytest.fixture
def claude_falso(p, monkeypatch):
    """Sustituye a Claude por una función que la prueba controla. Devuelve la lista de lotes recibidos."""
    recibidos: list[dict] = []
    respuesta = {"fn": None}

    def invocar(_p, sistema, mensaje, esquema):
        lote = json.loads(mensaje)
        recibidos.append({"lote": lote, "sistema": sistema, "esquema": esquema})
        datos = respuesta["fn"](lote) if respuesta["fn"] else {"items": [], "duplicados": [],
                                                              "conjunto": {"candidato": "", "nombre": "", "motivo": ""},
                                                              "observaciones": []}
        return {"datos": datos, "modelo": "falso", "costo_usd": 0.0, "proveedor": "prueba"}

    monkeypatch.setattr(claude_cliente, "detectar", lambda _p: {"proveedor": "prueba", "disponible": True,
                                                                "modelo": "falso", "detalle": "simulado"})
    monkeypatch.setattr(claude_cliente, "invocar", invocar)
    return {"recibidos": recibidos, "respuesta": respuesta}
