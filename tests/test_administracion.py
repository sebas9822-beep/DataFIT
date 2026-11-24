"""Rol administrador: usuarios, catálogo de inmobiliarias, respaldos y bitácora.

El administrador gestiona la plataforma y no ve cotizaciones ni clientes. Nada se borra en silencio: un usuario
eliminado conserva su historial, una inmobiliaria eliminada queda en descartadas con su motivo y cada cambio
queda en la bitácora, sin claves.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest
import yaml
from test_api import CLIENTES, entrar, esperar, pedir, servidor  # noqa: F401 (servidor es fixture)

from captura import acceso, admin, bd

CATALOGO = """# Inmobiliarias de prueba.
#
# Este comentario de cabecera debe seguir igual después de cada cambio.

sitios:
  - id: fincaraiz
    nombre: "Fincaraíz"
    dominio: fincaraiz.com.co
    tipo: portal
    nivel: A
    plataforma: "Portal"
    busqueda: "https://www.fincaraiz.com.co/venta/apartamentos/laureles/medellin"
    permitida: true
    por_defecto: true
    lectura: ok        # probado el 2026-11-23
    revision_terminos: pendiente
    nota: "Portal núcleo"
  - id: metrocuadrado
    nombre: "Metrocuadrado"
    dominio: metrocuadrado.com
    tipo: portal
    nivel: A
    busqueda: "https://www.metrocuadrado.com/apartamentos/venta/medellin/laureles/"
    permitida: true
    por_defecto: false       # no se dejó leer en la prueba; se puede encender a mano
    lectura: no_legible        # probado el 2026-11-23
    revision_terminos: pendiente

# No se consultan: quedan aquí con su motivo.
descartadas:
  - {nombre: "Maxibienes", dominio: "maxibienes.com", motivo: "Protección anti-bot del hosting. No apto para RPA"}
"""


@pytest.fixture
def catalogo(p):
    """Un catálogo pequeño en la carpeta de configuración de la prueba (nunca la real)."""
    ruta = p["rutas"]["config"] / "inmobiliarias.yaml"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(CATALOGO, encoding="utf-8", newline="\n")
    datos = yaml.safe_load(CATALOGO)
    p["inmobiliarias"].clear()
    p["inmobiliarias"].update(datos)
    return ruta


def como(puerto, usuario, clave=None):
    return {"Cookie": entrar(puerto, usuario, clave or usuario)}


def adm(puerto, metodo, ruta, cuerpo=None, galleta=None):
    return pedir(puerto, metodo, ruta, cuerpo, cabeceras=galleta, sin_sesion=True)


def test_el_administrador_entra_y_no_ve_cotizaciones_ni_clientes(servidor, p):
    a = como(servidor, "admin")
    estado, s, _ = adm(servidor, "GET", "/api/sesion", galleta=a)
    assert estado == 200 and s["rol"] == "admin" and s["nombre"] == "Administrador"
    assert adm(servidor, "GET", "/api/config", galleta=a)[1]["roles"] == {"comercial": "Comercial", "supervisor": "Supervisor", "admin": "Administrador"}
    # lo suyo: usuarios, inmobiliarias, respaldos, bitácora y el resumen del sistema
    for ruta in ("usuarios", "inmobiliarias", "respaldos", "bitacora", "sistema"):
        assert adm(servidor, "GET", f"/api/admin/{ruta}", galleta=a)[0] == 200, ruta
    # lo del negocio no: ni listas, ni un cliente por su documento, ni registrar, ni cotizar, ni el tablero
    for metodo, ruta, cuerpo in (("GET", "/api/cotizaciones", None), ("GET", "/api/clientes/1017234567", None), ("GET", "/api/capturas", None),
                                 ("GET", "/api/tablero", None), ("GET", "/api/supervision/clientes", None), ("GET", "/api/mi-gestion", None),
                                 ("POST", "/api/clientes", {"documento": "123456", "nombre": "Alguien", "autoriza": True}),
                                 ("POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18", "cliente_id": CLIENTES[servidor]})):
        estado, r, _ = adm(servidor, metodo, ruta, cuerpo, a)
        assert estado == 403 and "no ve cotizaciones ni clientes" in r["error"], ruta
    # y nadie más entra a la administración
    for usuario in ("comercial1", "supervisor1"):
        g = como(servidor, usuario)
        assert adm(servidor, "GET", "/api/admin/usuarios", galleta=g)[0] == 403
        assert adm(servidor, "POST", "/api/admin/usuarios", {"usuario": "intruso", "nombre": "X", "rol": "admin"}, g)[0] == 403
        assert adm(servidor, "POST", "/api/admin/respaldos", {}, g)[0] == 403
    assert adm(servidor, "GET", "/api/admin/usuarios")[0] == 401
    sistema = adm(servidor, "GET", "/api/admin/sistema", galleta=a)[1]
    assert sistema["usuarios"] == {"admin": 1, "comercial": 10, "supervisor": 2} and sistema["clientes"] == 1
    assert "Quintero" not in json.dumps(sistema, ensure_ascii=False) and "1017234567" not in json.dumps(sistema)


def test_el_administrador_crea_cambia_y_elimina_usuarios(servidor, p):
    a = como(servidor, "admin")
    # crear: nace con la clave inicial (igual al usuario) y ya puede entrar
    estado, r, _ = adm(servidor, "POST", "/api/admin/usuarios", {"usuario": " Comercial11 ", "nombre": "  laura   gómez ", "rol": "comercial"}, a)
    assert estado == 201 and r["usuario"]["usuario"] == "comercial11" and r["usuario"]["nombre"] == "laura gómez"
    assert r["usuario"]["clave_inicial"] is True and r["usuario"]["creado_por"] == "admin"
    g11 = como(servidor, "comercial11")
    assert adm(servidor, "GET", "/api/sesion", galleta=g11)[1]["rol"] == "comercial"
    # con clave propia, y un supervisor con equipo
    estado, r, _ = adm(servidor, "POST", "/api/admin/usuarios", {"usuario": "supervisor3", "nombre": "Supervisor 3", "rol": "supervisor",
                                                             "supervisa": ["comercial1", "comercial11"], "clave": "clave-de-supervision"}, a)
    assert estado == 201 and r["usuario"]["supervisa"] == ["comercial1", "comercial11"] and r["usuario"]["clave_inicial"] is False
    g_sup = como(servidor, "supervisor3", "clave-de-supervision")
    assert [c["usuario"] for c in adm(servidor, "GET", "/api/supervision/comerciales", galleta=g_sup)[1]["comerciales"]] == ["comercial1", "comercial11"]
    # validaciones
    for cuerpo, fragmento in (({"usuario": "x", "nombre": "Corto", "rol": "comercial"}, "entre 3 y 30"),
                              ({"usuario": "con espacio y ñ", "nombre": "Raro", "rol": "comercial"}, "letras minúsculas"),
                              ({"usuario": "comercial1", "nombre": "Repetido", "rol": "comercial"}, "ya existe"),
                              ({"usuario": "nuevo1", "nombre": "", "rol": "comercial"}, "nombre"),
                              ({"usuario": "nuevo1", "nombre": "Nuevo", "rol": "gerente"}, "rol"),
                              ({"usuario": "nuevo1", "nombre": "Nuevo", "rol": "comercial", "clave": "corta"}, "al menos 8"),
                              ({"usuario": "nuevo1", "nombre": "Nuevo", "rol": "comercial", "clave": "nuevo1"}, "al menos 8"),
                              ({"usuario": "nuevo1", "nombre": "Nuevo", "rol": "supervisor", "supervisa": ["nadie"]}, "No son comerciales")):
        estado, r, _ = adm(servidor, "POST", "/api/admin/usuarios", cuerpo, a)
        assert estado == 422 and fragmento in r["error"], (cuerpo, r)
    # cambiar: nombre y rol; al cambiar el rol tiene que volver a entrar
    estado, r, _ = adm(servidor, "POST", "/api/admin/usuarios/comercial11", {"nombre": "Laura Gómez", "rol": "supervisor", "supervisa": "todos"}, a)
    assert estado == 200 and r["usuario"]["nombre"] == "Laura Gómez" and r["usuario"]["rol"] == "supervisor" and r["usuario"]["supervisa"] == "todos"
    assert adm(servidor, "GET", "/api/sesion", galleta=g11)[1]["autenticado"] is False
    assert adm(servidor, "POST", "/api/admin/usuarios/noexiste", {"nombre": "X"}, a)[0] == 404
    # restablecer la clave: definida o de vuelta a la inicial; cierra las sesiones del usuario
    assert adm(servidor, "POST", "/api/admin/usuarios/comercial2/clave", {"clave": "corta"}, a)[0] == 422
    assert adm(servidor, "POST", "/api/admin/usuarios/comercial2/clave", {"clave": "clave-nueva-2"}, a)[1]["usuario"]["clave_inicial"] is False
    g2 = como(servidor, "comercial2", "clave-nueva-2")
    assert pedir(servidor, "POST", "/api/login", {"usuario": "comercial2", "clave": "comercial2"}, sin_sesion=True)[0] == 401
    assert adm(servidor, "POST", "/api/admin/usuarios/comercial2/clave", {}, a)[1]["usuario"]["clave_inicial"] is True
    assert adm(servidor, "GET", "/api/sesion", galleta=g2)[1]["autenticado"] is False
    como(servidor, "comercial2")

    # eliminar: deja de entrar, su historial se conserva y sigue en la lista como eliminado
    estado, r, _ = pedir(servidor, "POST", "/api/capturas", {"direccion": "Carrera 73 # 2-18", "curar_con_claude": False, "cliente_id": CLIENTES[servidor]})
    cot = r["cotizacion_id"]; esperar(servidor, r["trabajo_id"])
    g1 = como(servidor, "comercial1")
    estado, r, _ = adm(servidor, "POST", "/api/admin/usuarios/comercial1/eliminar", {}, a)
    assert estado == 200 and r["usuario"]["eliminado"] and not r["usuario"]["activo"] and r["usuario"]["cotizaciones"] == 1
    assert adm(servidor, "GET", "/api/sesion", galleta=g1)[1]["autenticado"] is False
    assert pedir(servidor, "POST", "/api/login", {"usuario": "comercial1", "clave": "comercial1"}, sin_sesion=True)[0] == 401
    with bd.conectar(p["rutas"]["bd"]) as con:
        assert con.execute("SELECT comercial FROM cotizaciones WHERE cotizacion_id=?", (cot,)).fetchone()[0] == "comercial1"
        assert con.execute("SELECT COUNT(*) FROM clientes").fetchone()[0] == 1
    lista = {u["usuario"]: u for u in adm(servidor, "GET", "/api/admin/usuarios", galleta=a)[1]["usuarios"]}
    assert lista["comercial1"]["eliminado"] and lista["comercial11"]["rol"] == "supervisor" and len(lista) == 15
    assert adm(servidor, "POST", "/api/admin/usuarios", {"usuario": "comercial1", "nombre": "Otro", "rol": "comercial"}, a)[1]["error"].startswith("El usuario comercial1 ya existió")
    # el arranque siguiente no deshace lo que hizo el administrador (usuarios.yaml dice que comercial1 está activo)
    acceso.sembrar_comerciales(p)
    lista = {u["usuario"]: u for u in admin.listar_usuarios(p)}
    assert lista["comercial1"]["eliminado"] and lista["comercial11"]["nombre"] == "Laura Gómez" and lista["comercial3"]["activo"]
    # reactivar lo devuelve con su historial
    assert adm(servidor, "POST", "/api/admin/usuarios/comercial1", {"activo": True}, a)[1]["usuario"]["eliminado"] is False
    como(servidor, "comercial1")

    # el administrador no se quita a sí mismo ni deja la plataforma sin administrador
    assert "tu propio usuario" in adm(servidor, "POST", "/api/admin/usuarios/admin/eliminar", {}, a)[1]["error"]
    assert "a ti mismo" in adm(servidor, "POST", "/api/admin/usuarios/admin", {"rol": "comercial"}, a)[1]["error"]
    assert adm(servidor, "POST", "/api/admin/usuarios", {"usuario": "admin2", "nombre": "Segundo", "rol": "admin"}, a)[0] == 201
    a2 = como(servidor, "admin2")
    assert adm(servidor, "POST", "/api/admin/usuarios/admin/eliminar", {}, a2)[0] == 200         # otro administrador sí puede
    assert "a ti mismo" in adm(servidor, "POST", "/api/admin/usuarios/admin2", {"activo": False}, a2)[1]["error"]
    assert adm(servidor, "GET", "/api/admin/usuarios", galleta=a)[0] == 401                    # el eliminado ya no entra

    # bitácora: todo quedó, con quién lo hizo, y ninguna clave
    eventos = adm(servidor, "GET", "/api/admin/bitacora", galleta=a2)[1]["eventos"]
    acciones = [e["accion"] for e in eventos]
    assert {"usuario_creado", "usuario_editado", "usuario_eliminado", "clave_restablecida"} <= set(acciones)
    texto = json.dumps(eventos, ensure_ascii=False)
    assert "clave-nueva-2" not in texto and "clave-de-supervision" not in texto
    volcado = "\n".join(sqlite3.connect(p["rutas"]["bd"]).iterdump())
    assert "clave-nueva-2" not in volcado and "clave-de-supervision" not in volcado


def test_no_queda_la_plataforma_sin_administrador(p):
    bd.iniciar(p)
    admin.crear_usuario(p, "admin", {"usuario": "admin2", "nombre": "Segundo", "rol": "admin"})
    admin.eliminar_usuario(p, "admin2", "admin")
    for accion in (lambda: admin.eliminar_usuario(p, "admin", "admin2"), lambda: admin.editar_usuario(p, "admin", "admin2", {"activo": False}),
                   lambda: admin.editar_usuario(p, "admin", "admin2", {"rol": "supervisor"})):
        with pytest.raises(admin.AdminInvalido, match="al menos un administrador"):
            accion()


def test_el_administrador_ajusta_el_catalogo_de_inmobiliarias(servidor, p, catalogo):
    a = como(servidor, "admin")
    antes = catalogo.read_text(encoding="utf-8")
    r = adm(servidor, "GET", "/api/admin/inmobiliarias", galleta=a)[1]
    assert [s["id"] for s in r["sitios"]] == ["fincaraiz", "metrocuadrado"] and r["descartadas"][0]["nombre"] == "Maxibienes"

    # agregar: el dominio se limpia, el identificador sale del dominio y queda "sin probar"
    estado, r, _ = adm(servidor, "POST", "/api/admin/inmobiliarias", {
        "nombre": "Inmobiliaria Los Robles", "dominio": "https://www.LosRobles.com.co/", "tipo": "inmobiliaria",
        "busqueda": "https://www.losrobles.com.co/venta?barrio=laureles", "nota": 'Trae "comillas" y: dos puntos'}, a)
    assert estado == 201 and r["sitio"]["id"] == "losrobles" and r["sitio"]["dominio"] == "losrobles.com.co"
    assert r["sitio"]["lectura"] == "sin_probar" and r["sitio"]["revision_terminos"] == "pendiente" and r["sitio"]["por_defecto"] is True
    # cambiar: solo cambian las líneas de ese sitio; el resultado de la prueba de lectura se conserva
    estado, r, _ = adm(servidor, "POST", "/api/admin/inmobiliarias/fincaraiz", {"por_defecto": False, "revision_terminos": "2026-11-23", "nota": "Términos revisados"}, a)
    assert estado == 200 and r["sitio"]["por_defecto"] is False and r["sitio"]["lectura"] == "ok" and r["sitio"]["revision_terminos"] == "2026-11-23"
    # eliminar: pasa a descartadas con su motivo (y el motivo es obligatorio)
    assert adm(servidor, "POST", "/api/admin/inmobiliarias/metrocuadrado/eliminar", {"motivo": "no"}, a)[0] == 422
    estado, r, _ = adm(servidor, "POST", "/api/admin/inmobiliarias/metrocuadrado/eliminar", {"motivo": "No se deja leer"}, a)
    assert estado == 200 and r["descartada"]["dominio"] == "metrocuadrado.com" and "No se deja leer" in r["descartada"]["motivo"]
    # un descartado que vuelve sale de descartadas
    estado, r, _ = adm(servidor, "POST", "/api/admin/inmobiliarias", {"nombre": "Maxibienes", "dominio": "maxibienes.com",
                                                                  "busqueda": "https://maxibienes.com/venta"}, a)
    assert estado == 201 and r["sitio"]["id"] == "maxibienes"

    # el archivo: se lee, dice lo esperado y lo que no se tocó sigue igual, comentarios incluidos
    despues = catalogo.read_text(encoding="utf-8")
    datos = yaml.safe_load(despues)
    assert [s["id"] for s in datos["sitios"]] == ["fincaraiz", "losrobles", "maxibienes"]
    assert [d["nombre"] for d in datos["descartadas"]] == ["Metrocuadrado"]
    assert despues.startswith("# Inmobiliarias de prueba.\n#\n# Este comentario de cabecera debe seguir igual después de cada cambio.\n\nsitios:\n")
    assert "    lectura: ok        # probado el 2026-11-23\n" in despues and "# No se consultan: quedan aquí con su motivo.\ndescartadas:\n" in despues
    assert '    nota: "Trae \\"comillas\\" y: dos puntos"\n' in despues and '    revision_terminos: "2026-11-23"\n' in despues
    assert datos["sitios"][1]["nota"] == 'Trae "comillas" y: dos puntos'
    assert len(list((catalogo.parent / "historial").glob("inmobiliarias_*.yaml"))) == 4          # una copia por cada cambio
    assert sorted(catalogo.parent.glob("historial/*.yaml"))[0].read_text(encoding="utf-8") == antes
    # la aplicación lo ve sin reiniciar: el comercial ya tiene el sitio nuevo en su lista y el eliminado no
    lista = {x["id"]: x for x in pedir(servidor, "GET", "/api/config")[1]["inmobiliarias"]["lista"]}
    assert set(lista) == {"fincaraiz", "losrobles", "maxibienes"} and lista["fincaraiz"]["por_defecto"] is False

    # validaciones: nada de esto cambia el archivo
    for ruta, cuerpo, fragmento in (
            ("/api/admin/inmobiliarias", {"nombre": "Otra", "dominio": "losrobles.com.co", "busqueda": "https://losrobles.com.co/x"}, "ya está en el catálogo"),
            ("/api/admin/inmobiliarias", {"nombre": "Otra", "dominio": "otra.com", "busqueda": "https://distinto.com/venta"}, "del dominio otra.com"),
            ("/api/admin/inmobiliarias", {"nombre": "Otra", "dominio": "otra.com", "busqueda": "http://otra.com/venta"}, "https://"),
            ("/api/admin/inmobiliarias", {"nombre": "Otra", "dominio": "no es un dominio", "busqueda": "https://otra.com/venta"}, "dominio del sitio"),
            ("/api/admin/inmobiliarias", {"nombre": "", "dominio": "otra.com", "busqueda": "https://otra.com/venta"}, "nombre del sitio"),
            ("/api/admin/inmobiliarias", {"nombre": "Otra", "dominio": "otra.com", "busqueda": "https://otra.com/venta", "tipo": "banco"}, "Tipo de sitio"),
            ("/api/admin/inmobiliarias/fincaraiz", {"permitida": False, "nota": ""}, "por qué"),
            ("/api/admin/inmobiliarias/fincaraiz", {"revision_terminos": "ayer"}, "AAAA-MM-DD")):
        estado, r, _ = adm(servidor, "POST", ruta, cuerpo, a)
        assert estado == 422 and fragmento in r["error"], (cuerpo, r)
    assert adm(servidor, "POST", "/api/admin/inmobiliarias/noexiste", {"nombre": "X"}, a)[0] == 404
    assert catalogo.read_text(encoding="utf-8") == despues
    # un sitio que deja de permitirse nunca queda encendido
    r = adm(servidor, "POST", "/api/admin/inmobiliarias/losrobles", {"permitida": False, "nota": "Sus términos prohíben robots"}, a)[1]["sitio"]
    assert r["permitida"] is False and r["por_defecto"] is False
    acciones = [e["accion"] for e in adm(servidor, "GET", "/api/admin/bitacora", galleta=a)[1]["eventos"]]
    assert acciones.count("sitio_agregado") == 2 and acciones.count("sitio_editado") == 2 and acciones.count("sitio_eliminado") == 1


def test_el_editor_del_catalogo_conserva_el_archivo_real(p, tmp_path):
    """Sobre una copia del catálogo real: cambiar un sitio toca solo sus líneas y el archivo sigue cumpliendo sus reglas."""
    from conftest import RAIZ
    real = (RAIZ / "config" / "inmobiliarias.yaml").read_text(encoding="utf-8")
    ruta = p["rutas"]["config"] / "inmobiliarias.yaml"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(real, encoding="utf-8", newline="\n")
    bd.iniciar(p)
    original = yaml.safe_load(real)
    primero = original["sitios"][0]["id"]
    admin.guardar_sitio(p, "admin", {"nota": "Nota nueva del administrador"}, primero)
    nuevo = ruta.read_text(encoding="utf-8")
    cambiadas = [(x, y) for x, y in zip(real.split("\n"), nuevo.split("\n")) if x != y]
    assert len(real.split("\n")) == len(nuevo.split("\n")) and len(cambiadas) == 1 and cambiadas[0][1] == '    nota: "Nota nueva del administrador"'
    leido = yaml.safe_load(nuevo)
    assert len(leido["sitios"]) == len(original["sitios"]) and leido["descartadas"] == original["descartadas"]
    assert admin.guardar_sitio(p, "admin", {}, primero)["nota"] == "Nota nueva del administrador"      # sin cambios: no escribe
    assert len(list((ruta.parent / "historial").iterdir())) == 1


def test_el_administrador_crea_y_elimina_respaldos(servidor, p, catalogo):
    a = como(servidor, "admin")
    (p["rutas"]["fichas"] / "1017234567").mkdir(parents=True)
    (p["rutas"]["fichas"] / "1017234567" / "FICHA_prueba_v1.md").write_text("# Ficha de prueba", encoding="utf-8")
    (p["rutas"]["config"] / ".env").write_text("GOOGLE_SERVER_KEY=LLAVE-QUE-NO-SE-COPIA", encoding="utf-8")
    assert adm(servidor, "GET", "/api/admin/respaldos", galleta=a)[1]["respaldos"] == []
    estado, r, _ = adm(servidor, "POST", "/api/admin/respaldos", {}, a)
    assert estado == 201 and r["respaldo"]["filas"]["clientes"] == 1 and r["respaldo"]["filas"]["comerciales"] == 13
    archivo = p["rutas"]["respaldos"] / r["respaldo"]["nombre"]
    with zipfile.ZipFile(archivo) as z:
        nombres = set(z.namelist())
        assert {"data/captura.db", "config/inmobiliarias.yaml", "fichas_clientes/1017234567/FICHA_prueba_v1.md", "manifiesto.json"} <= nombres
        assert not any(".env" in n or "almacen" in n for n in nombres)
        manifiesto = json.loads(z.read("manifiesto.json"))
        assert manifiesto["creado_por"] == "admin" and manifiesto["filas"]["clientes"] == 1 and manifiesto["archivos_de_fichas"] == 1
        (p["rutas"]["respaldos"] / "copia.db").write_bytes(z.read("data/captura.db"))
    copia = sqlite3.connect(p["rutas"]["respaldos"] / "copia.db")
    assert copia.execute("SELECT nombre FROM clientes").fetchone()[0] == "Ana María Quintero"        # la base del respaldo se puede abrir
    copia.close()
    assert sorted(f.name for f in p["rutas"]["respaldos"].iterdir()) == ["copia.db", archivo.name]     # sin archivos temporales
    lista = adm(servidor, "GET", "/api/admin/respaldos", galleta=a)[1]["respaldos"]
    assert [x["nombre"] for x in lista] == [archivo.name] and lista[0]["bytes"] == archivo.stat().st_size
    assert adm(servidor, "GET", "/api/admin/sistema", galleta=a)[1]["ultimo_respaldo"]["nombre"] == archivo.name
    # eliminar: solo un respaldo con nombre válido
    assert adm(servidor, "POST", "/api/admin/respaldos/copia.db/eliminar", {}, a)[0] == 404
    assert adm(servidor, "POST", f"/api/admin/respaldos/{archivo.name}/eliminar", {}, a)[0] == 200 and not archivo.exists()
    assert adm(servidor, "POST", f"/api/admin/respaldos/{archivo.name}/eliminar", {}, a)[0] == 404
    with pytest.raises(admin.AdminInvalido):
        admin.eliminar_respaldo(p, "admin", "../captura.db")
    acciones = [e["accion"] for e in adm(servidor, "GET", "/api/admin/bitacora", galleta=a)[1]["eventos"]]
    assert acciones[:2] == ["respaldo_eliminado", "respaldo_creado"]
