"""Verificador de prerrequisitos (iniciar.bat verificar): red simulada, sin llamadas reales."""
from __future__ import annotations

import json
import socket
import threading

import httpx
import pytest

from captura import api, verificacion
from captura.curaduria import claude_cliente
from captura.fuentes import base

LLAVE_SRV = "AIza" + "S" * 35
LLAVE_NAV = "AIza" + "N" * 35


def red(caidos=(), lentos=(), google=None):
    """Internet simulado. `caidos`: hosts que no conectan. `lentos`: hosts que no responden a tiempo.
    `google`: respuestas de Geocoding y Places ({'geocode': (codigo, json), 'places': (codigo, json)})."""
    google = google or {"geocode": (200, {"status": "OK", "results": [{}]}), "places": (200, {"places": [{"id": "x"}]})}
    vistos = []

    def manejador(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        vistos.append(str(req.url))
        if host in caidos:
            raise httpx.ConnectError(f"[Errno 11001] getaddrinfo failed para {req.url}", request=req)
        if host in lentos:
            raise httpx.ReadTimeout("sin respuesta", request=req)
        if host == "www.medellin.gov.co":
            if req.url.path.endswith("/services"):
                return httpx.Response(200, json={"currentVersion": 11.1, "folders": ["ServiciosCatastro"]})
            return httpx.Response(200, json={"name": "Capa de prueba", "extent": {"spatialReference": {"wkid": 9377}}})
        if host.startswith("overpass"):
            return httpx.Response(200, json={"elements": [{"type": "node", "id": 1}]})
        if host == "nominatim.openstreetmap.org":
            return httpx.Response(200, json={"status": 0, "message": "OK"})
        if host in ("unpkg.com", "tile.openstreetmap.org", "fonts.googleapis.com"):
            return httpx.Response(200, content=b"x")
        if host == "maps.googleapis.com":
            codigo, cuerpo = google["geocode"]
            return httpx.Response(codigo, json=cuerpo)
        if host == "places.googleapis.com":
            codigo, cuerpo = google["places"]
            return httpx.Response(codigo, json=cuerpo)
        raise AssertionError(f"host inesperado {host}")
    return manejador, vistos


@pytest.fixture
def v(p, monkeypatch, tmp_path):
    """Configuración lista para verificar: puerto libre, informes a una carpeta temporal, Claude simulado."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p["params"]["servidor"]["puerto"] = s.getsockname()[1]
    p["rutas"]["exportaciones"] = tmp_path / "salidas"
    p["params"]["claude"]["proveedor"] = "auto"
    monkeypatch.setattr(claude_cliente, "detectar", lambda _p: {
        "proveedor": "claude_code", "disponible": True, "modelo": "haiku", "detalle": "Claude Code", "ejecutable": "claude"})
    monkeypatch.setattr(verificacion, "sesion_claude", lambda exe: {
        "loggedIn": True, "authMethod": "claude.ai", "loginExpiresAt": "2099-01-01T00:00:00Z"})
    llamadas = []

    def invocar(_p, sistema, mensaje, esquema):
        llamadas.append(esquema)
        return {"datos": {"ok": True}, "modelo": "haiku", "costo_usd": 0.0006, "proveedor": "claude_code"}
    monkeypatch.setattr(claude_cliente, "invocar", invocar)
    p["_llamadas_claude"] = llamadas
    return p


def estados(chequeos):
    return {c.nombre: c.estado for c in chequeos}


def test_todo_en_orden_y_sin_secretos(v, monkeypatch, capsys):
    v["secretos"].update(google_servidor=LLAVE_SRV, google_navegador=LLAVE_NAV, google_map_id="mi-mapa-propio")
    manejador, vistos = red()
    monkeypatch.setattr(base, "TRANSPORTE", httpx.MockTransport(manejador))
    codigo = verificacion.main([])
    salida = capsys.readouterr().out
    assert codigo == 0 and "Listo para capturar" in salida and "FALLA" not in salida
    for nombre in ("Directorio de servicios", "nomenclatura (catastro)", "establecimientos (registro_ic)",
                   "Geocoding API", "Places API (New)", "Llamada de prueba", "Sesión"):
        assert nombre in salida
    assert "EPSG:9377" in salida and "vence en" in salida
    assert v["_llamadas_claude"] == [verificacion.ESQUEMA_PRUEBA]
    # una sola llamada de prueba a cada servicio de Google, y la llave no aparece en ningún lado
    assert sum("maps.googleapis.com" in u for u in vistos) == 1 and sum("places.googleapis.com" in u for u in vistos) == 1
    informe = next((v["rutas"]["exportaciones"]).glob("verificacion_*.txt")).read_text(encoding="utf-8")
    for texto in (salida, informe):
        assert LLAVE_SRV not in texto and LLAVE_NAV not in texto


def test_sin_internet_se_explica_y_falla(v, monkeypatch):
    hosts = ("www.medellin.gov.co", "overpass-api.de", "overpass.kumi.systems", "nominatim.openstreetmap.org",
             "unpkg.com", "tile.openstreetmap.org", "fonts.googleapis.com")
    monkeypatch.setattr(base, "TRANSPORTE", httpx.MockTransport(red(caidos=hosts)[0]))
    ch = verificacion.correr(v)
    e = estados(ch)
    assert e["Directorio de servicios"] == "falla" and "nomenclatura (catastro)" not in e
    mapa = next(c for c in ch if c.nombre == "Directorio de servicios")
    assert "DNS" in mapa.detalle and "VPN" in mapa.que_hacer
    assert e["Overpass · overpass-api.de"] == e["Overpass · overpass.kumi.systems"] == "aviso"   # no impide capturar
    assert "apaga el botón OpenStreetMap" in next(c for c in ch if c.nombre == "Overpass · overpass.kumi.systems").que_hacer
    assert e["Teselas del mapa OpenStreetMap"] == "aviso"
    assert "Hay fallas" in verificacion.informe(ch, v)


def test_basta_una_instancia_de_overpass(v, monkeypatch):
    monkeypatch.setattr(base, "TRANSPORTE", httpx.MockTransport(red(lentos=("overpass-api.de",))[0]))
    e = estados(verificacion.correr(v, en_vivo_claude=False))
    assert e["Overpass · overpass-api.de"] == "aviso" and e["Overpass · overpass.kumi.systems"] == "ok"
    assert "falla" not in e.values()


def test_google_rechaza_la_clave_sin_filtrarla(v, monkeypatch):
    v["secretos"]["google_servidor"] = LLAVE_SRV
    negado = {"geocode": (200, {"status": "REQUEST_DENIED",
                                "error_message": "This API project is not authorized to use this API."}),
              "places": (403, {"error": {"code": 403, "message": "Places API (New) has not been used in project 123"}})}
    monkeypatch.setattr(base, "TRANSPORTE", httpx.MockTransport(red(google=negado)[0]))
    ch = verificacion.correr(v, en_vivo_claude=False)
    geo = next(c for c in ch if c.nombre == "Geocoding API")
    pla = next(c for c in ch if c.nombre == "Places API (New)")
    assert geo.estado == pla.estado == "falla"
    assert "not authorized" in geo.detalle and "Habilita Geocoding" in geo.que_hacer
    assert "has not been used" in pla.detalle and "no la versión heredada" in pla.que_hacer
    # una excepción de red trae la URL con la llave: no debe aparecer en el informe
    monkeypatch.setattr(base, "TRANSPORTE", httpx.MockTransport(red(caidos=("maps.googleapis.com",))[0]))
    texto = verificacion.informe(verificacion.correr(v, en_vivo_claude=False), v)
    assert LLAVE_SRV not in texto and "Geocoding API" in texto


def test_claves_mal_puestas(v, monkeypatch):
    v["secretos"].update(google_navegador=LLAVE_SRV, google_servidor=LLAVE_SRV)
    monkeypatch.setattr(base, "TRANSPORTE", httpx.MockTransport(red()[0]))
    ch = verificacion.correr(v, en_vivo_claude=False)
    nav = next(c for c in ch if c.nombre == "Clave de navegador")
    assert nav.estado == "aviso" and "misma clave" in nav.detalle
    assert next(c for c in ch if c.nombre == "Map ID").estado == "aviso"
    v["secretos"].update(google_navegador="pegada-a-medias", google_servidor="")
    ch = verificacion.correr(v, en_vivo_claude=False)
    assert "formato" in next(c for c in ch if c.nombre == "Clave de navegador").detalle
    assert next(c for c in ch if c.nombre == "Clave de servidor").estado == "omitido"


def test_sesion_de_claude(v, monkeypatch):
    monkeypatch.setattr(verificacion, "sesion_claude", lambda exe: {"loggedIn": False})
    ch = verificacion.claude(v)
    assert estados(ch)["Sesión"] == "falla" and "/login" in ch[1].que_hacer
    assert estados(ch)["Llamada de prueba"] == "omitido" and v["_llamadas_claude"] == []
    monkeypatch.setattr(verificacion, "sesion_claude", lambda exe: {
        "loggedIn": True, "authMethod": "claude.ai", "loginExpiresAt": "2000-01-01T00:00:00Z"})
    assert estados(verificacion.claude(v, en_vivo=False))["Sesión"] == "aviso"


def test_claude_ausente_o_caido_no_impide_capturar(v, monkeypatch):
    monkeypatch.setattr(claude_cliente, "detectar", lambda _p: {
        "proveedor": "auto", "disponible": False, "modelo": None, "detalle": "no se encontró Claude Code"})
    ch = verificacion.claude(v)
    assert len(ch) == 1 and ch[0].estado == "aviso" and "solo con reglas" in ch[0].que_hacer

    def cae(*a, **k):
        raise claude_cliente.ClaudeNoDisponible("Claude Code terminó con error: sesión vencida")
    monkeypatch.setattr(claude_cliente, "detectar", lambda _p: {
        "proveedor": "claude_code", "disponible": True, "modelo": "haiku", "detalle": "Claude Code", "ejecutable": "claude"})
    monkeypatch.setattr(claude_cliente, "invocar", cae)
    prueba = next(c for c in verificacion.claude(v) if c.nombre == "Llamada de prueba")
    assert prueba.estado == "falla" and "sesión vencida" in prueba.detalle


def test_puerto(v):
    puerto = v["params"]["servidor"]["puerto"]
    assert verificacion._puerto(v).estado == "ok"
    otro = socket.socket(); otro.bind(("127.0.0.1", puerto)); otro.listen(1)
    try:
        c = verificacion._puerto(v)
        assert c.estado == "falla" and "otro programa" in c.detalle and f"--puerto {puerto + 1}" in c.que_hacer
    finally:
        otro.close()
    srv = api.crear_servidor(v, puerto)
    hilo = threading.Thread(target=srv.serve_forever, daemon=True); hilo.start()
    try:
        assert "esta copia" in verificacion._puerto(v).detalle
        ajena = dict(v, rutas=dict(v["rutas"], bd=v["rutas"]["bd"].parent / "otra" / "captura.db"))
        c = verificacion._puerto(ajena)
        assert c.estado == "aviso" and "OTRA copia" in c.detalle
    finally:
        srv.shutdown(); srv.server_close()


def test_equipo_y_sin_red(v, monkeypatch):
    def prohibido(req):
        raise AssertionError("no debía salir a la red")
    monkeypatch.setattr(base, "TRANSPORTE", httpx.MockTransport(prohibido))
    ch = verificacion.correr(v, en_vivo_claude=False, con_red=False)
    e = estados(ch)
    assert e["Paquetes"] == "ok" and e["Configuración"] == "ok" and e["Carpeta"] == "ok"
    assert e["Base de datos"] == "ok" and e["Red"] == "omitido"
    env = next(c for c in ch if c.nombre == "Archivo .env")
    assert env.estado == "aviso" and "CONTACTO" in env.que_hacer          # sin correo propio para OpenStreetMap
    v["user_agent"] = v["user_agent"].replace(v["params"]["red"]["contacto_por_defecto"], "datos@ejemplo.co")
    assert next(c for c in verificacion.equipo(v) if c.nombre == "Archivo .env").estado == "ok"
    v["params"]["servidor"]["host"] = "0.0.0.0"
    assert estados(verificacion.equipo(v))["Configuración"] == "falla"
