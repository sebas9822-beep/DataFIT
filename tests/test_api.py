"""La API local: lo que ve el navegador. Servidor real en un puerto efímero, fuentes simuladas."""
from __future__ import annotations

import csv
import http.client
import io
import json
import threading
import time

import pytest

from captura import api


SESIONES: dict[int, str] = {}          # puerto -> cabecera Cookie del comercial que entró
CLIENTES: dict[int, int] = {}          # puerto -> cliente registrado para capturar


def entrar(puerto, usuario="comercial1", clave="comercial1"):
    estado, datos, r = pedir(puerto, "POST", "/api/login", {"usuario": usuario, "clave": clave}, sin_sesion=True)
    assert estado == 200, datos
    return r.getheader("Set-Cookie").split(";", 1)[0]


@pytest.fixture
def servidor(p, mundo):
    srv = api.crear_servidor(p, 0)
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    puerto = srv.server_address[1]
    SESIONES[puerto] = entrar(puerto)
    _, datos, _ = pedir(puerto, "POST", "/api/clientes", {"documento": "1.017.234.567", "nombre": "ana maría quintero",
                                                        "autoriza": True})
    CLIENTES[puerto] = datos["cliente"]["cliente_id"]
    yield puerto
    srv.shutdown()
    srv.server_close()


def pedir(puerto, metodo, ruta, cuerpo=None, cabeceras=None, sin_sesion=False):
    con = http.client.HTTPConnection("127.0.0.1", puerto, timeout=30)
    h = {"Content-Type": "application/json"} if cuerpo is not None else {}
    if not sin_sesion and SESIONES.get(puerto):
        h["Cookie"] = SESIONES[puerto]
    h.update(cabeceras or {})
    con.request(metodo, ruta, None if cuerpo is None else json.dumps(cuerpo).encode("utf-8"), h)
    r = con.getresponse()
    datos = r.read()
    con.close()
    tipo = r.getheader("Content-Type") or ""
    return r.status, (json.loads(datos) if "json" in tipo else datos.decode("utf-8")), r


def esperar(puerto, trabajo_id):
    for _ in range(400):
        _, prog, _ = pedir(puerto, "GET", f"/api/trabajos/{trabajo_id}")
        if prog["terminado"]:
            return prog
        time.sleep(0.02)
    raise AssertionError("la captura no terminó")


def test_pagina_y_configuracion_no_exponen_secretos(servidor, p):
    p["secretos"]["google_servidor"] = "LLAVE-DE-SERVIDOR-SECRETA"
    p["secretos"]["anthropic"] = "sk-ant-secreta"
    estado, html, r = pedir(servidor, "GET", "/")
    assert estado == 200 and "Venture One" in html and "text/html" in r.getheader("Content-Type")
    assert r.getheader("Cache-Control") == "no-store" and r.getheader("X-Content-Type-Options") == "nosniff"
    estado, cfg, _ = pedir(servidor, "GET", "/api/config")
    assert estado == 200 and cfg["radio_m"] == 1000 and cfg["anillos_m"] == [300, 500, 1000]
    assert [f["id"] for f in cfg["fuentes"]] == ["geomedellin", "catastro", "registro_ic", "oime", "osm", "google"]
    assert {c["id"] for c in cfg["categorias"]} == set(p["taxonomia"]["categorias"])
    assert cfg["claude"]["disponible"] is False and cfg["mapa"]["centro"]["lat"] == pytest.approx(6.2442)
    plano = json.dumps(cfg)
    assert "LLAVE-DE-SERVIDOR-SECRETA" not in plano and "sk-ant-secreta" not in plano
    assert "LLAVE-DE-SERVIDOR-SECRETA" not in html
    estado, salud, _ = pedir(servidor, "GET", "/health")
    assert estado == 200 and salud["ok"] is True


def test_solo_atiende_al_equipo_local(servidor):
    estado, datos, _ = pedir(servidor, "GET", "/api/config", cabeceras={"Host": "sitio-ajeno.example"})
    assert estado == 403 and "127.0.0.1" in datos["error"]
    estado, _, _ = pedir(servidor, "POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18"},
                         cabeceras={"Host": "sitio-ajeno.example"})
    assert estado == 403


def test_geocodificar_y_lectura_del_punto(servidor):
    estado, g, _ = pedir(servidor, "POST", "/api/geocodificar", {"direccion": "Cra 73 # 2 - 18"})
    assert estado == 200 and g["ok"] and g["precision"] == "P1" and g["origen"] == "catastro"
    assert g["direccion_catastral"] == "CR 73 # 2-18"
    estado, pt, _ = pedir(servidor, "GET", f"/api/punto?lat={g['lat']}&lng={g['lng']}")
    assert estado == 200 and pt["en_cobertura"] is True and pt["barrio"] and pt["cbml"]
    assert pt["conjunto"]["nombre"] == "Edificio Los Almendros" and pt["conjunto"]["origen"] == "catastro"
    estado, mal, _ = pedir(servidor, "POST", "/api/geocodificar", {"direccion": "x"})
    assert estado == 422


@pytest.mark.parametrize("cuerpo, fragmento", [
    ({}, "dirección"),
    ({"direccion": "ab"}, "dirección"),
    ({"lat": 6.2}, "juntos"),
    ({"lat": "norte", "lng": -75.5}, "número"),
    ({"lat": 96, "lng": -75.5}, "rango"),
    ({"direccion": "Carrera 73 # 2-18", "fuentes": ["portal_inventado"]}, "fuentes"),
    ({"direccion": "Carrera 73 # 2-18", "fuentes": []}, "fuentes"),
    ({"lat": 6.2, "lng": -75.5, "origen_coordenada": "adivinado"}, "origen"),
    ({"direccion": "a" * 400}, "larga"),
])
def test_entradas_invalidas_dan_422(servidor, cuerpo, fragmento):
    estado, datos, _ = pedir(servidor, "POST", "/api/capturas", cuerpo)
    assert estado == 422 and fragmento in datos["error"]


def test_cuerpo_que_no_es_json(servidor):
    con = http.client.HTTPConnection("127.0.0.1", servidor, timeout=10)
    con.request("POST", "/api/capturas", b"{no es json", {"Content-Type": "application/json", "Cookie": SESIONES[servidor]})
    assert con.getresponse().status == 422
    con.close()
    con = http.client.HTTPConnection("127.0.0.1", servidor, timeout=10)
    con.request("POST", "/api/capturas", b"direccion=x", {"Content-Type": "application/x-www-form-urlencoded",
                                                         "Cookie": SESIONES[servidor]})
    assert con.getresponse().status == 422        # un formulario de otra página no puede lanzar capturas
    con.close()


def test_captura_completa_por_la_api(servidor, p):
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False,
                                                            "cliente_id": CLIENTES[servidor]})
    assert estado == 202
    prog = esperar(servidor, r["trabajo_id"])
    assert prog["error"] is None and prog["captura_id"] and not prog["reusada"]
    # dos bloques: el entorno del predio (se reusa) y el mercado de la cotización (del día)
    assert [(x["id"], x["grupo"]) for x in prog["pasos"]] == [
        ("ubicacion", "entorno"), ("predio", "entorno"), ("fuentes", "entorno"), ("reglas", "entorno"), ("guardado", "entorno"),
        ("claude", "entorno"), ("indicadores", "entorno"),
        ("inmobiliarias", "mercado"), ("depuracion", "mercado"), ("almacen", "mercado"), ("ficha", "mercado")]
    etiquetas = {x["id"]: x["etiqueta"] for x in prog["pasos"]}
    assert etiquetas["guardado"] == "Guardado provisional" and etiquetas["indicadores"] == "Indicadores y cierre"
    assert etiquetas["depuracion"] == "Depuración de anuncios"
    dias = p["reglas"]["captura"]["reusar_captura_dias"]
    assert prog["grupos"] == [{"id": "entorno", "etiqueta": "Entorno del predio", "nota": f"se reusa {dias} días"},
                              {"id": "mercado", "etiqueta": "Mercado de la cotización", "nota": "siempre del día"}]
    assert {x["estado"] for x in prog["pasos"]} <= {"ok", "parcial", "omitido"}
    assert prog["capas"] and {"capa", "estado", "registros"} <= set(prog["capas"][0])
    cid = prog["captura_id"]

    estado, cap, _ = pedir(servidor, "GET", f"/api/capturas/{cid}")
    assert estado == 200 and cap["punto"]["precision"] == "P1" and cap["predio"]["barrio_nombre"]
    assert cap["conjunto"]["nombre"] == "Edificio Los Almendros"
    assert len(cap["categorias"]) == len(p["taxonomia"]["categorias"])
    edu = next(c for c in cap["categorias"] if c["id"] == "educacion")
    assert edu["lugares"] >= 3 and set(edu["por_fuente"]) <= {"geomedellin", "catastro", "osm"}
    seg = next(c for c in cap["categorias"] if c["id"] == "seguridad")
    assert sum(seg["por_fuente"].values()) < 10                 # las cámaras (capa masiva) no inflan la categoría
    assert cap["indicadores"]["camaras_cctv"]["valor"] >= 1
    assert all(x["fuente"] != "registro_ic" for x in cap["items"])
    assert "inversión" in cap["aviso_alcance"] and cap["atribuciones"]

    estado, lista, _ = pedir(servidor, "GET", "/api/capturas")
    assert [c["captura_id"] for c in lista["capturas"]] == [cid] and lista["capturas"][0]["lugares"] > 0

    # misma dirección: se reusa la captura guardada, sin volver a consultar las fuentes
    _, r2, _ = pedir(servidor, "POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False,
                                                        "cliente_id": CLIENTES[servidor]})
    prog2 = esperar(servidor, r2["trabajo_id"])
    assert prog2["reusada"] is True and prog2["captura_id"] == cid

    # exportaciones
    for tabla, columna in (("entorno", "motivo_exclusion"), ("ofertas", "tipo_precio"), ("bitacora", "regla"),
                           ("ejecuciones", "estado"), ("indicadores", "indicador"), ("predio", "cbml")):
        estado, texto, resp = pedir(servidor, "GET", f"/api/capturas/{cid}/exportar?tabla={tabla}")
        assert estado == 200 and "attachment" in resp.getheader("Content-Disposition")
        filas = list(csv.DictReader(io.StringIO(texto.lstrip("﻿"))))
        assert filas and columna in filas[0], tabla
    _, texto, _ = pedir(servidor, "GET", f"/api/capturas/{cid}/exportar?tabla=ofertas")
    assert {f["tipo_precio"] for f in csv.DictReader(io.StringIO(texto.lstrip("﻿")))} == {"pedido"}
    assert "phone" not in texto and "agency" not in texto
    estado, _, _ = pedir(servidor, "GET", f"/api/capturas/{cid}/exportar?tabla=sqlite_master")
    assert estado == 422

    # corrección manual del conjunto: queda en la bitácora como edición del usuario
    estado, ok, _ = pedir(servidor, "POST", f"/api/capturas/{cid}/conjunto", {"nombre": "Conjunto Los Almendros P.H."})
    assert estado == 200 and ok["ok"]
    _, cap, _ = pedir(servidor, "GET", f"/api/capturas/{cid}")
    assert cap["conjunto"] == {"nombre": "Conjunto Los Almendros P.H.", "tipo": cap["conjunto"]["tipo"], "origen": "usuario"}
    _, bit, _ = pedir(servidor, "GET", f"/api/capturas/{cid}/exportar?tabla=bitacora")
    assert "editado_por_usuario" in bit


def test_error_de_ubicacion_se_informa_sin_inventar(servidor, mundo):
    mundo.caidas.add("nominatim")                 # ni la nomenclatura ni el respaldo la encuentran
    _, r, _ = pedir(servidor, "POST", "/api/capturas", {"direccion": "Calle 999 # 99-99", "cliente_id": CLIENTES[servidor]})
    prog = esperar(servidor, r["trabajo_id"])
    assert prog["error"] and "Marca el punto" in prog["error"] and prog["captura_id"] is None
    assert next(x for x in prog["pasos"] if x["id"] == "ubicacion")["estado"] == "error"


def test_rutas_inexistentes(servidor):
    assert pedir(servidor, "GET", "/api/capturas/00000000-0000-0000-0000-000000000000")[0] == 404
    assert pedir(servidor, "GET", "/api/trabajos/00000000-0000-0000-0000-000000000000")[0] == 404
    assert pedir(servidor, "GET", "/../config/params.yaml")[0] == 404
    assert pedir(servidor, "GET", "/api/no-existe")[0] == 404
    assert pedir(servidor, "POST", "/api/capturas/00000000-0000-0000-0000-000000000000/conjunto", {"nombre": "x"})[0] == 404


def test_segundo_arranque_reconoce_la_aplicacion_abierta(servidor, p, monkeypatch, capsys):
    """Doble clic dos veces en el lanzador: la segunda no falla, abre el navegador sobre la primera."""
    assert api.ya_abierta("127.0.0.1", servidor) is True
    libre = http.client.HTTPConnection  # noqa: F841  (solo para dejar claro que no se abre otra conexión)
    abiertos = []
    monkeypatch.setattr(api.webbrowser, "open", lambda url: abiertos.append(url))
    monkeypatch.setattr(api.sys, "argv", ["captura.api", "--puerto", str(servidor)])
    api.main()                                             # el puerto está ocupado por la misma aplicación
    assert abiertos == [f"http://127.0.0.1:{servidor}"] and "ya está abierta" in capsys.readouterr().out


def test_puerto_ocupado_por_otro_programa_se_explica(p, monkeypatch):
    import socket
    otro = socket.socket(); otro.bind(("127.0.0.1", 0)); otro.listen(1)
    puerto = otro.getsockname()[1]
    try:
        assert api.ya_abierta("127.0.0.1", puerto) is False
        monkeypatch.setattr(api.sys, "argv", ["captura.api", "--puerto", str(puerto), "--sin-navegador"])
        with pytest.raises(SystemExit) as e:
            api.main()
        assert str(puerto) in str(e.value) and "--puerto" in str(e.value)
    finally:
        otro.close()
