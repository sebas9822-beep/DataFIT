"""Comerciales, sesiones, clientes y cotizaciones: quién captura, para quién y con qué evidencia."""
from __future__ import annotations

import csv
import io
import json
import sqlite3

import pytest
from test_api import CLIENTES, entrar, esperar, pedir, servidor  # noqa: F401 (servidor es fixture)

from captura import acceso, bd, clientes, verificacion

DOC, DOC_TXT = "1017234567", "1.017.234.567"


def con(p):
    c = sqlite3.connect(p["rutas"]["bd"])
    c.row_factory = sqlite3.Row
    return c


def capturar(puerto, cliente_id, **extra):
    cuerpo = {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False, "cliente_id": cliente_id, **extra}
    estado, r, _ = pedir(puerto, "POST", "/api/capturas", cuerpo)
    assert estado == 202, r
    return r["cotizacion_id"], esperar(puerto, r["trabajo_id"])


# ---------------------------------------------------------------------------------------------
# Comerciales y sesiones
# ---------------------------------------------------------------------------------------------
def test_se_crean_diez_comerciales_sin_claves_en_texto(p):
    c = con(p)
    filas = c.execute("SELECT * FROM comerciales WHERE rol='comercial' ORDER BY length(usuario), usuario").fetchall()
    assert [f["usuario"] for f in filas] == [f"comercial{i}" for i in range(1, 11)]
    assert [f["nombre"] for f in filas][:2] == ["Comercial 1", "Comercial 2"]
    for f in filas:
        assert len(f["clave_hash"]) == 64 and f["clave_hash"] != f["usuario"] and f["clave_cambiada_en"] is None
        assert acceso.hash_clave(f["usuario"], f["sal"], f["iteraciones"]) == f["clave_hash"]   # clave inicial = usuario
    assert len({f["sal"] for f in filas}) == 10                                                 # sal propia por usuario
    assert acceso.sembrar_comerciales(p) == 0                                                   # arrancar otra vez no duplica


def test_resembrar_no_pisa_una_clave_cambiada_y_respeta_activo(p):
    acceso.cambiar_clave(p, "comercial3", "Nueva-clave-segura")
    p["usuarios"]["comerciales"][3]["activo"] = False                                          # comercial4 deja de estar activo
    acceso.sembrar_comerciales(p)
    assert acceso.autenticar(p, "comercial3", "Nueva-clave-segura")["usuario"] == "comercial3"
    with pytest.raises(acceso.AccesoDenegado):
        acceso.autenticar(p, "comercial3", "comercial3")
    with pytest.raises(acceso.AccesoDenegado):
        acceso.autenticar(p, "comercial4", "comercial4")
    assert "comercial3" not in acceso.con_clave_inicial(p) and "comercial4" not in acceso.con_clave_inicial(p)


def test_ingreso_por_la_api(servidor, p):
    estado, datos, r = pedir(servidor, "POST", "/api/login", {"usuario": " Comercial 2 ", "clave": "comercial2"},
                             sin_sesion=True)
    galleta = r.getheader("Set-Cookie")
    assert estado == 200 and datos == {"autenticado": True, "usuario": "comercial2", "nombre": "Comercial 2", "rol": "comercial"}
    assert "HttpOnly" in galleta and "SameSite=Strict" in galleta and "v1_sesion=" in galleta
    token = galleta.split(";", 1)[0].split("=", 1)[1]
    assert token not in json.dumps([dict(f) for f in con(p).execute("SELECT * FROM sesiones")])  # solo su huella
    estado, datos, _ = pedir(servidor, "POST", "/api/login", {"usuario": "comercial2", "clave": "otra"}, sin_sesion=True)
    assert estado == 401 and datos["error"] == "Usuario o clave incorrectos"
    estado, datos, _ = pedir(servidor, "POST", "/api/login", {"usuario": "nadie", "clave": "x"}, sin_sesion=True)
    assert estado == 401 and datos["error"] == "Usuario o clave incorrectos"                      # mismo mensaje
    eventos = [f["evento"] for f in con(p).execute("SELECT evento FROM accesos WHERE usuario='comercial2'")]
    assert eventos == ["entrada", "clave_errada"]


def test_sin_sesion_solo_se_ve_la_pagina(servidor):
    for metodo, ruta, cuerpo in (("GET", "/api/config", None), ("GET", "/api/capturas", None),
                                 ("GET", "/api/cotizaciones", None), ("GET", f"/api/clientes/{DOC}", None),
                                 ("POST", "/api/clientes", {"documento": DOC}), ("POST", "/api/capturas", {"direccion": "x"}),
                                 ("GET", "/api/no-existe", None)):
        estado, datos, _ = pedir(servidor, metodo, ruta, cuerpo, sin_sesion=True)
        assert estado == 401 and "Inicia sesión" in datos["error"], ruta
    assert pedir(servidor, "GET", "/", sin_sesion=True)[0] == 200
    assert pedir(servidor, "GET", "/health", sin_sesion=True)[0] == 200
    assert pedir(servidor, "GET", "/api/sesion", sin_sesion=True)[1] == {"autenticado": False}
    estado, datos, _ = pedir(servidor, "GET", "/api/sesion")
    assert datos == {"autenticado": True, "usuario": "comercial1", "nombre": "Comercial 1", "rol": "comercial"}


def test_salir_vencer_y_desactivar_cierran_la_sesion(servidor, p):
    otra = entrar(servidor, "comercial5", "comercial5")
    assert pedir(servidor, "GET", "/api/config", cabeceras={"Cookie": otra})[0] == 200
    estado, _, r = pedir(servidor, "POST", "/api/logout", {}, cabeceras={"Cookie": otra})
    assert estado == 200 and "Max-Age=0" in r.getheader("Set-Cookie")
    assert pedir(servidor, "GET", "/api/config", cabeceras={"Cookie": otra})[0] == 401
    otra = entrar(servidor, "comercial6", "comercial6")
    with bd.conectar(p["rutas"]["bd"]) as c:
        c.execute("UPDATE sesiones SET expira_en='2000-01-01T00:00:00+00:00' WHERE usuario='comercial6'")
    assert pedir(servidor, "GET", "/api/config", cabeceras={"Cookie": otra})[0] == 401
    otra = entrar(servidor, "comercial7", "comercial7")
    with bd.conectar(p["rutas"]["bd"]) as c:
        c.execute("UPDATE comerciales SET activo=0 WHERE usuario='comercial7'")
    assert pedir(servidor, "GET", "/api/config", cabeceras={"Cookie": otra})[0] == 401
    assert pedir(servidor, "GET", "/api/config", cabeceras={"Cookie": "v1_sesion=inventado"})[0] == 401


def test_bloqueo_por_intentos(p):
    for _ in range(p["params"]["acceso"]["intentos_max"]):
        with pytest.raises(acceso.AccesoDenegado):
            acceso.autenticar(p, "comercial8", "mala")
    with pytest.raises(acceso.AccesoDenegado, match="Demasiados intentos"):
        acceso.autenticar(p, "comercial8", "comercial8")                    # ni con la clave correcta
    assert acceso.autenticar(p, "comercial9", "comercial9")                   # los demás siguen entrando


def test_cambiar_clave(p):
    t = acceso.crear_sesion(p, "comercial1")
    with pytest.raises(ValueError, match="al menos"):
        acceso.cambiar_clave(p, "comercial1", "corta")
    with pytest.raises(ValueError, match="igual al usuario"):
        acceso.cambiar_clave(p, "comercial1", "Comercial 1")
    acceso.cambiar_clave(p, "comercial1", "otra-clave-larga")
    assert acceso.sesion(p, t) is None                                        # cambiar la clave cierra sesiones
    assert acceso.autenticar(p, "comercial1", "otra-clave-larga")["nombre"] == "Comercial 1"
    with pytest.raises(ValueError, match="No existe"):
        acceso.cambiar_clave(p, "comercial99", "otra-clave-larga")


# ---------------------------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------------------------
def test_registro_breve_del_cliente(servidor, p):
    # el fixture ya registró a "ana maría restrepo" con cédula con puntos
    estado, datos, _ = pedir(servidor, "GET", f"/api/clientes/{DOC_TXT}")
    cli = datos["cliente"]
    assert estado == 200 and datos["encontrado"] and cli["documento"] == DOC and cli["nombre"] == "Ana María Quintero"
    assert cli["documento_mascara"] == "******4567" and cli["autoriza_datos"] and cli["cotizaciones"] == 0
    fila = con(p).execute("SELECT * FROM clientes WHERE documento=?", (DOC,)).fetchone()
    assert fila["creado_por"] == "comercial1" and fila["autorizado_por"] == "comercial1"
    assert fila["texto_autorizacion_sha256"] == clientes.huella(p["reglas"]["clientes"]["texto_autorizacion"])
    # otro comercial lo actualiza: cambia el nombre, la autorización conserva fecha y autor
    otra = entrar(servidor, "comercial2", "comercial2")
    _, datos, _ = pedir(servidor, "POST", "/api/clientes", {"documento": DOC, "nombre": "Ana María Quintero Gómez"},
                        cabeceras={"Cookie": otra})
    fila2 = con(p).execute("SELECT * FROM clientes WHERE documento=?", (DOC,)).fetchone()
    assert datos["cliente"]["nombre"] == "Ana María Quintero Gómez" and fila2["autorizado_en"] == fila["autorizado_en"]
    assert fila2["creado_por"] == "comercial1"
    assert pedir(servidor, "GET", "/api/clientes/99999999")[1] == {"encontrado": False, "cliente": None}


@pytest.mark.parametrize("cuerpo, fragmento", [
    ({"documento": "12a45678", "nombre": "Pedro Pérez", "autoriza": True}, "solo números"),
    ({"documento": "123", "nombre": "Pedro Pérez", "autoriza": True}, "entre 5 y 10"),
    ({"documento": "12345678901", "nombre": "Pedro Pérez", "autoriza": True}, "entre 5 y 10"),
    ({"documento": "80123456", "nombre": "P", "autoriza": True}, "entre 3 y 120"),
    ({"documento": "80123456", "nombre": "12345", "autoriza": True}, "con letras"),
    ({"documento": "80123456", "nombre": "<script>x</script>", "autoriza": True}, "con letras"),
    ({"documento": "80123456", "nombre": "Pedro Pérez"}, "autorización"),
])
def test_cliente_invalido(servidor, cuerpo, fragmento):
    estado, datos, _ = pedir(servidor, "POST", "/api/clientes", cuerpo)
    assert estado == 422 and fragmento in datos["error"]


def test_autorizacion_configurable(p):
    p["reglas"]["clientes"]["exigir_autorizacion"] = False
    c = clientes.registrar(p, "comercial1", "80123456", "PEDRO PÉREZ", False)
    assert c["nombre"] == "Pedro Pérez" and not c["autoriza_datos"]
    assert clientes.para_capturar(p, c["cliente_id"])["documento"] == "80123456"


# ---------------------------------------------------------------------------------------------
# Cotizaciones
# ---------------------------------------------------------------------------------------------
def test_cotizacion_con_evidencia(servidor, p):
    cid = CLIENTES[servidor]
    cot1, prog1 = capturar(servidor, cid)
    cot2, prog2 = capturar(servidor, cid)                                    # mismo punto: se reabre la captura
    assert prog2["reusada"] and prog2["captura_id"] == prog1["captura_id"]
    filas = {f["cotizacion_id"]: dict(f) for f in con(p).execute("SELECT * FROM cotizaciones")}
    a, b = filas[cot1], filas[cot2]
    assert a["comercial"] == b["comercial"] == "comercial1" and a["cliente_id"] == b["cliente_id"] == cid
    assert a["cliente_nombre"] == "Ana María Quintero" and a["cliente_documento"] == DOC
    assert a["estado"] == "ok" and a["reusada"] == 0 and b["reusada"] == 1 and a["captura_id"] == b["captura_id"]
    ev = json.loads(a["evidencia_json"])
    assert ev["captura_id"] == a["captura_id"] and ev["cbml"] and ev["lugares"]["educacion"] >= 3
    assert a["evidencia_sha256"] == clientes.huella(a["evidencia_json"]) and a["huella_config"] == p["huella"]
    # lo que ve el comercial y lo que cuenta para el cliente
    _, lista, _ = pedir(servidor, "GET", "/api/cotizaciones")
    assert lista["total"] == 2 and [c["cotizacion_id"] for c in lista["cotizaciones"]] == [cot2, cot1]
    assert lista["cotizaciones"][0]["cliente_documento_mascara"] == "******4567"
    assert DOC not in json.dumps(lista)                                      # en la lista, la cédula va enmascarada
    assert pedir(servidor, "GET", f"/api/clientes/{DOC}")[1]["cliente"]["cotizaciones"] == 2
    otra = entrar(servidor, "comercial2", "comercial2")
    assert pedir(servidor, "GET", "/api/cotizaciones", cabeceras={"Cookie": otra})[1]["total"] == 0
    # la vista queda lista para agrupar
    grupos = con(p).execute("SELECT comercial, cliente_nombre, COUNT(*) n FROM v_cotizaciones "
                            "WHERE estado IN ('ok','parcial') GROUP BY comercial, cliente_id").fetchall()
    assert [tuple(g) for g in grupos] == [("comercial1", "Ana María Quintero", 2)]


def test_sin_cliente_no_hay_captura(servidor, p):
    for cuerpo in ({"direccion": "Carrera 73 # 2-18"}, {"direccion": "Carrera 73 # 2-18", "cliente_id": 999},
                   {"direccion": "Carrera 73 # 2-18", "cliente_id": "uno"}):
        estado, datos, _ = pedir(servidor, "POST", "/api/capturas", cuerpo)
        assert estado == 422 and "Registra primero al cliente" in datos["error"]
    assert con(p).execute("SELECT COUNT(*) FROM cotizaciones").fetchone()[0] == 0


def test_cotizacion_fallida_queda_registrada_y_no_cuenta(servidor, p, mundo):
    mundo.caidas.add("nominatim")
    cot, prog = capturar(servidor, CLIENTES[servidor], direccion="Calle 999 # 99-99")
    fila = con(p).execute("SELECT * FROM cotizaciones WHERE cotizacion_id=?", (cot,)).fetchone()
    assert prog["error"] and fila["estado"] == "error" and fila["captura_id"] is None and fila["mensaje"]
    assert fila["terminada_en"] and pedir(servidor, "GET", f"/api/clientes/{DOC}")[1]["cliente"]["cotizaciones"] == 0
    assert pedir(servidor, "GET", "/api/cotizaciones")[1]["cotizaciones"][0]["estado"] == "error"


def test_datos_del_cliente_no_van_a_claude_ni_a_las_exportaciones(servidor, p, claude_falso):
    _, prog = capturar(servidor, CLIENTES[servidor], curar_con_claude=True)
    assert claude_falso["recibidos"], "Claude debía recibir al menos un lote"
    enviado = json.dumps([r["lote"] for r in claude_falso["recibidos"]], ensure_ascii=False)
    assert DOC not in enviado and "Quintero" not in enviado
    for tabla in ("entorno", "ofertas", "indicadores", "bitacora", "ejecuciones", "revision", "predio"):
        estado, texto, _ = pedir(servidor, "GET", f"/api/capturas/{prog['captura_id']}/exportar?tabla={tabla}")
        assert estado == 200 and DOC not in texto and "Quintero" not in texto, tabla
    _, cap, _ = pedir(servidor, "GET", f"/api/capturas/{prog['captura_id']}")
    assert DOC not in json.dumps(cap, ensure_ascii=False)


def test_verificador_avisa_claves_iniciales(p):
    ch = {c.nombre: c for c in verificacion.equipo(p)}
    assert ch["Comerciales"].estado == "aviso" and "13 de 13" in ch["Comerciales"].detalle   # 10 comerciales + 2 supervisores + 1 administrador
    assert "iniciar.bat clave admin" in ch["Comerciales"].que_hacer     # el administrador es el primero que debe cambiarla
    for i in range(1, 11):
        acceso.cambiar_clave(p, f"comercial{i}", f"clave-propia-{i}")
    for i in (1, 2):                                   # los supervisores también deben cambiar la suya
        acceso.cambiar_clave(p, f"supervisor{i}", f"clave-de-supervision-{i}")
    assert verificacion.equipo(p)[[c.nombre for c in verificacion.equipo(p)].index("Comerciales")].estado == "aviso"   # falta el administrador
    acceso.cambiar_clave(p, "admin", "clave-de-administrador")
    assert verificacion.equipo(p)[[c.nombre for c in verificacion.equipo(p)].index("Comerciales")].estado == "ok"


def test_peticiones_seguidas_en_la_misma_conexion(servidor):
    """Un POST cuyo cuerpo la ruta no usa (salir) no debe contaminar la petición siguiente."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", servidor, timeout=10)
    otra = entrar(servidor, "comercial4", "comercial4")
    c.request("POST", "/api/logout", b"{}", {"Content-Type": "application/json", "Cookie": otra})
    r1 = c.getresponse(); r1.read()
    c.request("GET", "/api/sesion")
    r2 = c.getresponse()
    assert r1.status == 200 and r2.status == 200 and json.loads(r2.read()) == {"autenticado": False}
    c.close()
