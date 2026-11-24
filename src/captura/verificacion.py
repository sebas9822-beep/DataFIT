"""Verificación de prerrequisitos: ¿está este equipo listo para capturar?

Uso:   iniciar.bat verificar
       python -m captura.verificacion [--sin-claude] [--sin-red]

Revisa el equipo (Python, paquetes, carpeta, base, puerto, .env), la conexión con Claude y cada
fuente con la consulta más pequeña posible. No cambia la base ni la configuración.

Llamadas que cuestan algo: una a Claude (fracción mínima del plan; se omite con --sin-claude) y,
solo si hay llave de servidor de Google, una a Geocoding y una a Places. Las llaves nunca se
imprimen ni se guardan en el informe.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import __version__, acceso, config
from .curaduria import claude_cliente
from .fuentes import arcgis, base

ETIQUETA = {"ok": " OK  ", "aviso": "AVISO", "falla": "FALLA", "omitido": " --  "}
PAQUETES = [("httpx", "httpx", True), ("yaml", "PyYAML", True), ("rapidfuzz", "rapidfuzz", True),
            ("pandas", "pandas", True), ("pyproj", "pyproj", True), ("pytest", "pytest", False)]
SISTEMA_PRUEBA = "Eres una prueba de conexión. Devuelve solo la salida estructurada pedida, con ok en true."
ESQUEMA_PRUEBA = {"type": "object", "additionalProperties": False, "required": ["ok"],
                  "properties": {"ok": {"type": "boolean"}}}


@dataclass
class Chequeo:
    grupo: str
    nombre: str
    estado: str                    # ok | aviso | falla | omitido
    detalle: str = ""
    que_hacer: str = ""
    segundos: float | None = field(default=None)


# ---------------------------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------------------------
def motivo_red(e: Exception) -> str:
    """Explica una falla de red sin repetir el texto de la excepción (puede traer la URL con la llave)."""
    texto = f"{type(e).__name__} {e}"
    if isinstance(e, httpx.ConnectTimeout):
        return "no conecta: se agotó el tiempo"
    if isinstance(e, httpx.ConnectError):
        if any(s in texto for s in ("getaddrinfo", "Name or service", "nodename", "No address", "11001")):
            return "no encuentra el servidor (DNS): revisa la conexión a internet o la VPN"
        return "no conecta: la red, una VPN o un firewall lo impide"
    if isinstance(e, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "conecta pero no responde a tiempo (servidor ocupado o caído)"
    if isinstance(e, httpx.RemoteProtocolError):
        return "cortó la conexión a mitad de la respuesta"
    return f"error de red ({type(e).__name__})"


def ocultar(texto: str, p: dict) -> str:
    """Última barrera: ningún secreto sale en pantalla ni en el archivo del informe."""
    for valor in (p.get("secretos") or {}).values():
        if valor and len(valor) >= 8 and valor != "DEMO_MAP_ID":
            texto = texto.replace(valor, "****")
    return texto


async def _medir(coro):
    t = time.perf_counter()
    try:
        return await coro, None, round(time.perf_counter() - t, 1)
    except Exception as e:  # noqa: BLE001 - se clasifica, no se propaga
        return None, e, round(time.perf_counter() - t, 1)


# ---------------------------------------------------------------------------------------------
# 1. Equipo
# ---------------------------------------------------------------------------------------------
def equipo(p: dict) -> list[Chequeo]:
    g, v = "Equipo", p["params"]["verificacion"]
    out: list[Chequeo] = []
    raiz = Path(p["raiz"])

    py = sys.version_info
    exe = Path(sys.executable)
    if py < (3, 11):
        out.append(Chequeo(g, "Python", "falla", f"{py.major}.{py.minor} en {exe}",
                           "Se necesita Python 3.11 o superior: usa el entorno conda venture-one."))
    elif "venture-one" not in str(exe).lower():
        out.append(Chequeo(g, "Python", "aviso", f"{py.major}.{py.minor}.{py.micro} en {exe}",
                           "No es el entorno venture-one. Funciona si tiene los paquetes, pero el lanzador busca ese."))
    else:
        out.append(Chequeo(g, "Python", "ok", f"{py.major}.{py.minor}.{py.micro} · entorno venture-one"))

    faltan, versiones = [], []
    for modulo, dist, obligatorio in PAQUETES:
        try:
            versiones.append(f"{dist} {importlib.metadata.version(dist)}")
        except importlib.metadata.PackageNotFoundError:
            faltan.append((dist, obligatorio))
    obligatorios = [d for d, o in faltan if o]
    if obligatorios:
        out.append(Chequeo(g, "Paquetes", "falla", "faltan: " + ", ".join(obligatorios),
                           "Con el entorno activo:  pip install -r requirements.txt"))
    elif faltan:
        out.append(Chequeo(g, "Paquetes", "aviso", "falta pytest (solo para las pruebas automáticas)",
                           "pip install pytest  (la aplicación funciona sin él)"))
    else:
        out.append(Chequeo(g, "Paquetes", "ok", " · ".join(versiones)))

    host = p["params"]["servidor"]["host"]
    if host not in ("127.0.0.1", "localhost"):
        out.append(Chequeo(g, "Configuración", "falla", f"el servidor escucha en {host}",
                           "Debe ser 127.0.0.1 (decisión D-02): corrige params.yaml › servidor.host."))
    else:
        out.append(Chequeo(g, "Configuración", "ok", f"4 archivos YAML · huella {p['huella']} · versión {__version__}"))

    web = Path(p["rutas"]["web"])
    datos = Path(p["rutas"]["bd"]).parent
    try:
        datos.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=datos, prefix=".prueba_", delete=True):
            pass
        escribe = True
    except OSError:
        escribe = False
    if not web.exists():
        out.append(Chequeo(g, "Carpeta", "falla", f"falta {web.name} en {raiz}",
                           "La carpeta está incompleta: vuelve a abrir iniciar.bat para instalar desde el paquete."))
    elif not escribe:
        out.append(Chequeo(g, "Carpeta", "falla", f"no se puede escribir en {datos}",
                           "Revisa permisos de la carpeta o si la sincroniza OneDrive y está bloqueada."))
    else:
        out.append(Chequeo(g, "Carpeta", "ok", str(raiz)))

    libre = shutil.disk_usage(raiz).free / 1024 ** 3
    out.append(Chequeo(g, "Disco", "ok" if libre >= v["disco_minimo_gb"] else "aviso", f"{libre:,.1f} GB libres",
                       "" if libre >= v["disco_minimo_gb"] else "Cada captura ocupa unos 10 a 15 MB en la base: libera espacio."))

    bd = Path(p["rutas"]["bd"])
    if not bd.exists():
        out.append(Chequeo(g, "Base de datos", "ok", "aún no existe: se crea al primer arranque"))
    else:
        try:
            con = sqlite3.connect(f"{bd.as_uri()}?mode=ro", uri=True)
            estado = con.execute("PRAGMA quick_check").fetchone()[0]
            n = con.execute("SELECT COUNT(*) FROM capturas").fetchone()[0]
            con.close()
            tam = bd.stat().st_size / 1024 ** 2
            if estado == "ok":
                out.append(Chequeo(g, "Base de datos", "ok", f"{n} capturas · {tam:,.1f} MB · íntegra"))
            else:
                out.append(Chequeo(g, "Base de datos", "falla", f"revisión de integridad: {estado[:80]}",
                                   "Haz una copia de data/captura.db y avisa antes de seguir capturando."))
        except sqlite3.Error as e:
            out.append(Chequeo(g, "Base de datos", "falla", f"no se puede leer ({type(e).__name__})",
                               "¿Otra herramienta la tiene abierta? Ciérrala y repite."))

    if bd.exists():
        try:
            iniciales = acceso.con_clave_inicial(p)
            con = sqlite3.connect(f"{bd.as_uri()}?mode=ro", uri=True)
            activos = con.execute("SELECT COUNT(*) FROM comerciales WHERE activo = 1").fetchone()[0]
            con.close()
            if iniciales:
                out.append(Chequeo(g, "Comerciales", "aviso",
                                   f"{activos} activos · {len(iniciales)} de {activos} con la clave inicial (igual al usuario)",
                                   f"Sirve para pruebas. Antes de uso real cambia cada clave:  iniciar.bat clave {iniciales[0]}  (A-14)."))
            else:
                out.append(Chequeo(g, "Comerciales", "ok", f"{activos} activos · todos cambiaron la clave inicial"))
        except sqlite3.Error:
            out.append(Chequeo(g, "Comerciales", "ok", "se crean al próximo arranque de la aplicación"))
    out.append(_almacen(p))
    out.append(_carpeta_fichas(p))
    out.append(_puerto(p))

    archivo_env = raiz / ".env"
    s = p["secretos"]
    definidas = [n for n, k in (("GOOGLE_SERVER_KEY", "google_servidor"), ("GOOGLE_BROWSER_KEY", "google_navegador"),
                                ("ANTHROPIC_API_KEY", "anthropic")) if s.get(k)]
    if s.get("google_map_id") and s["google_map_id"] != "DEMO_MAP_ID":
        definidas.append("GOOGLE_MAP_ID")
    contacto_propio = "contacto: " + p["params"]["red"]["contacto_por_defecto"] not in p["user_agent"]
    if contacto_propio:
        definidas.append("CONTACTO")
    detalle = ("existe" if archivo_env.exists() else "no existe") + (
        " · definidas: " + ", ".join(definidas) if definidas else " · sin llaves (la aplicación trabaja sin Google)")
    if contacto_propio:
        out.append(Chequeo(g, "Archivo .env", "ok", detalle))
    else:
        out.append(Chequeo(g, "Archivo .env", "aviso", detalle,
                           "OpenStreetMap pide un correo real de contacto: copia .env.example como .env y llena "
                           "CONTACTO= (decisión A-07)."))
    return out


def _almacen(p: dict) -> Chequeo:
    """El almacén en esquema estrella: que exista y que su última corrida del ETL no tenga fallas."""
    g, ruta = "Equipo", Path(p["rutas"]["almacen"])
    if not ruta.exists():
        return Chequeo(g, "Almacén", "aviso", "todavía no existe",
                       "Se crea solo al abrir la aplicación. Para cargarlo completo:  iniciar.bat etl")
    try:
        con = sqlite3.connect(f"{ruta.as_uri()}?mode=ro", uri=True)
        fila = con.execute("SELECT corrida_id, estado, modo, fin FROM etl_corrida ORDER BY corrida_id DESC LIMIT 1").fetchone()
        fallas = con.execute("SELECT COUNT(*) FROM etl_calidad WHERE resultado='falla' AND corrida_id="
                             "(SELECT MAX(corrida_id) FROM etl_corrida WHERE estado <> 'error')").fetchone()[0]
        hechos = con.execute("SELECT (SELECT COUNT(*) FROM fact_cotizacion) || ' cotizaciones · ' || "
                             "(SELECT COUNT(*) FROM fact_oferta) || ' ofertas · ' || (SELECT COUNT(*) FROM fact_ficha) || ' fichas'").fetchone()[0]
        con.close()
    except sqlite3.Error as e:
        return Chequeo(g, "Almacén", "falla", f"no se puede leer ({type(e).__name__})",
                       "Cierra la aplicación y vuelve a cargarlo:  iniciar.bat etl")
    if fila is None:
        return Chequeo(g, "Almacén", "aviso", "sin corridas del ETL", "Cárgalo con  iniciar.bat etl")
    if fila[1] == "error" or fallas:
        return Chequeo(g, "Almacén", "falla", f"corrida {fila[0]} ({fila[2]}): {fila[1]} · {fallas} controles de calidad con falla",
                       "Revisa el panel Calidad de datos (supervisor) y vuelve a correr  iniciar.bat etl")
    return Chequeo(g, "Almacén", "ok", f"{hechos} · corrida {fila[0]} ({fila[1].replace('_', ' ')})")


def _carpeta_fichas(p: dict) -> Chequeo:
    g, carpeta = "Equipo", Path(p["rutas"]["fichas"])
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        prueba = carpeta / ".escritura"
        prueba.write_text("ok", encoding="utf-8")
        prueba.unlink()
    except OSError as e:
        return Chequeo(g, "Carpeta de fichas", "falla", f"no se puede escribir en {carpeta} ({type(e).__name__})",
                       "Las fichas se guardan ahí, una carpeta por NIT o cédula: revisa los permisos.")
    clientes = sum(1 for d in carpeta.iterdir() if d.is_dir())
    return Chequeo(g, "Carpeta de fichas", "ok", f"{carpeta.name}: {clientes} clientes con fichas")


def _puerto(p: dict) -> Chequeo:
    g = "Equipo"
    host, puerto = p["params"]["servidor"]["host"], p["params"]["servidor"]["puerto"]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        libre = s.connect_ex((host, puerto)) != 0
    if libre:
        return Chequeo(g, f"Puerto {puerto}", "ok", "libre (la aplicación está cerrada)")
    try:
        with urllib.request.urlopen(f"http://{host}:{puerto}/health", timeout=3) as r:
            salud = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        salud = None
    if not salud or "version" not in salud:
        return Chequeo(g, f"Puerto {puerto}", "falla", "lo usa otro programa",
                       f"Ciérralo, o arranca con  iniciar.bat --puerto {puerto + 1}")
    if Path(salud.get("bd", "")).resolve() == Path(p["rutas"]["bd"]).resolve():
        return Chequeo(g, f"Puerto {puerto}", "ok", f"la aplicación ya está abierta (esta copia, v{salud['version']})")
    return Chequeo(g, f"Puerto {puerto}", "aviso", f"hay OTRA copia abierta, que guarda en {salud.get('bd')}",
                   "Cierra esa ventana negra y abre esta copia con iniciar.bat: lo que captures allá no llega aquí.")


# ---------------------------------------------------------------------------------------------
# 2. Claude
# ---------------------------------------------------------------------------------------------
def sesion_claude(exe: str) -> dict | None:
    """Estado de la sesión de Claude Code (`claude auth status`). None si no se pudo leer."""
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return None


def claude(p: dict, en_vivo: bool = True) -> list[Chequeo]:
    g, v = "Claude", p["params"]["verificacion"]
    det = claude_cliente.detectar(p)
    if det["proveedor"] == "ninguno":
        return [Chequeo(g, "Proveedor", "omitido", "desactivado en params.yaml (claude.proveedor: ninguno)",
                        "Si quieres la curaduría con Claude: claude.proveedor: auto")]
    if not det["disponible"]:
        return [Chequeo(g, "Proveedor", "aviso", det["detalle"],
                        "Sin Claude la captura funciona y se depura solo con reglas. Para activarlo instala Claude "
                        "Code e inicia sesión, o pon ANTHROPIC_API_KEY en .env y ejecuta  pip install anthropic.")]
    out = [Chequeo(g, "Proveedor", "ok", f"{det['detalle']} · modelo {det['modelo']}")]

    if det["proveedor"] == "claude_code":
        ses = sesion_claude(det["ejecutable"])
        if ses is None:
            out.append(Chequeo(g, "Sesión", "aviso", "no se pudo leer el estado de la sesión",
                               "La llamada de prueba dirá si funciona."))
        elif not ses.get("loggedIn"):
            out.append(Chequeo(g, "Sesión", "falla", "Claude Code no tiene sesión iniciada",
                               "Abre una terminal, escribe  claude  y usa  /login  con la cuenta del plan."))
            en_vivo = False
        else:
            detalle, estado, hacer = f"iniciada ({ses.get('authMethod', '?')})", "ok", ""
            vence = ses.get("loginExpiresAt")
            if vence:
                try:
                    dias = (datetime.fromisoformat(vence.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
                    detalle += f" · vence en {dias} días ({vence[:10]})"
                    if dias < v["aviso_sesion_claude_dias"]:
                        estado, hacer = "aviso", "Renueva la sesión: en una terminal  claude  y luego  /login."
                except ValueError:
                    pass
            out.append(Chequeo(g, "Sesión", estado, detalle, hacer))

    if not en_vivo:
        out.append(Chequeo(g, "Llamada de prueba", "omitido", "no se hizo"))
        return out
    t = time.perf_counter()
    try:
        r = claude_cliente.invocar(p, SISTEMA_PRUEBA, json.dumps({"prueba": "conexion"}), ESQUEMA_PRUEBA)
        seg = round(time.perf_counter() - t, 1)
        if (r.get("datos") or {}).get("ok") is True:
            costo = f" · costo informado {r['costo_usd']:.4f} USD" if r.get("costo_usd") is not None else ""
            out.append(Chequeo(g, "Llamada de prueba", "ok", f"respondió con salida estructurada ({r.get('modelo')}){costo}",
                               segundos=seg))
        else:
            out.append(Chequeo(g, "Llamada de prueba", "aviso", "respondió, pero sin la estructura esperada",
                               "Actualiza Claude Code (claude update) y repite.", seg))
    except claude_cliente.ClaudeNoDisponible as e:
        out.append(Chequeo(g, "Llamada de prueba", "falla", str(e)[:160],
                           "Revisa la sesión (claude, luego /login). Mientras tanto la captura queda solo con reglas.",
                           round(time.perf_counter() - t, 1)))
    return out


# ---------------------------------------------------------------------------------------------
# 3. Fuentes
# ---------------------------------------------------------------------------------------------
async def _servidor_mapas(cli, p) -> list[Chequeo]:
    g, v = "Servidor de mapas (Alcaldía)", p["params"]["verificacion"]
    raiz = p["params"]["arcgis"]["base"]
    r, e, seg = await _medir(cli.get(raiz, params={"f": "json"}, timeout=v["timeout_s"]))
    if e is not None or r.status_code != 200:
        motivo = motivo_red(e) if e is not None else f"respondió HTTP {r.status_code}"
        return [Chequeo(g, "Directorio de servicios", "falla", motivo,
                        "Sin este servidor no hay catastro ni capas oficiales: revisa internet o la VPN y repite.", seg)]
    try:
        version = r.json().get("currentVersion", "?")
    except ValueError:
        return [Chequeo(g, "Directorio de servicios", "falla", "respondió algo que no es el servidor de mapas",
                        "Puede ser un portal de la red (wifi de invitados, VPN): revisa la conexión.", seg)]
    out = [Chequeo(g, "Directorio de servicios", "ok", f"versión {version}", segundos=seg)]

    todas = {k: dict(c, id=k, fuente="catastro") for k, c in p["fuentes"]["capas_predio"].items()}
    todas.update({c["id"]: c for c in p["fuentes"]["capas_entorno"]})
    fuentes_activas = {k for k, f in p["fuentes"]["fuentes"].items() if f.get("activa")}

    async def una(clave):
        c = todas[clave]
        nombre = f"{clave} ({c.get('fuente', '?')})"
        if c.get("fuente") not in fuentes_activas or c.get("activa") is False:
            return Chequeo(g, nombre, "omitido", "fuente o capa desactivada en fuentes.yaml")
        url = arcgis.url_capa(p, c["servicio"], c["capa"]).removesuffix("/query")
        r, e, seg = await _medir(cli.get(url, params={"f": "json"}, timeout=v["timeout_s"]))
        if e is not None:
            return Chequeo(g, nombre, "falla", motivo_red(e), "Corre  iniciar.bat probar  para el detalle por capa.", seg)
        try:
            meta = r.json()
        except ValueError:
            meta = {"error": {"message": f"HTTP {r.status_code}, respuesta no JSON"}}
        if "error" in meta:
            return Chequeo(g, nombre, "falla", f"el servidor respondió: {str(meta['error'].get('message'))[:90]}",
                           "La capa pudo cambiar de número o de servicio: revisa fuentes.yaml con  iniciar.bat probar.", seg)
        ref = (meta.get("extent") or {}).get("spatialReference") or meta.get("sourceSpatialReference") or {}
        crs = ref.get("latestWkid") or ref.get("wkid") or "?"
        return Chequeo(g, nombre, "ok", f"{meta.get('name', '?')} · CRS nativo EPSG:{crs}", segundos=seg)

    out += await asyncio.gather(*[una(k) for k in v["capas_muestra"] if k in todas])
    return out


async def _osm(cli, p) -> list[Chequeo]:
    g, v, o = "OpenStreetMap", p["params"]["verificacion"], p["params"]["osm"]
    if not p["fuentes"]["fuentes"]["osm"].get("activa"):
        return [Chequeo(g, "Overpass", "omitido", "fuente desactivada en fuentes.yaml")]
    consulta = "[out:json][timeout:15];node(1);out ids;"

    async def instancia(url):
        tiempo = httpx.Timeout(v["timeout_overpass_s"], connect=o["timeout_conexion_s"])
        r, e, seg = await _medir(cli.post(url, data={"data": consulta}, timeout=tiempo))
        host = url.split("/")[2]
        if e is not None:
            return Chequeo(g, f"Overpass · {host}", "falla", motivo_red(e), segundos=seg)
        if r.status_code == 429:
            return Chequeo(g, f"Overpass · {host}", "falla", "rechazó por exceso de consultas (429)", segundos=seg)
        if r.status_code != 200:
            return Chequeo(g, f"Overpass · {host}", "falla", f"respondió HTTP {r.status_code}", segundos=seg)
        lento = seg > o["timeout_http_s"] / 2
        return Chequeo(g, f"Overpass · {host}", "aviso" if lento else "ok",
                       "responde" + (" pero lento: puede pasarse del tiempo en una captura" if lento else ""), segundos=seg)

    filas = list(await asyncio.gather(*[instancia(u) for u in o["overpass_urls"]]))
    vivas = [f for f in filas if f.estado in ("ok", "aviso")]
    if vivas:
        for f in filas:                      # con una viva basta: las demás son respaldo, no bloquean
            if f.estado == "falla":
                f.estado, f.detalle = "aviso", f.detalle + " (hay otra instancia que sí responde)"
    else:                                    # OpenStreetMap complementa: sin él la captura sigue
        for f in filas:
            f.estado = "aviso"
        filas[-1].que_hacer = ("Ninguna instancia de Overpass responde. Es un servicio comunitario sin garantía: "
                               "al capturar apaga el botón OpenStreetMap (la captura sigue con las fuentes oficiales) "
                               "y repite más tarde.")

    url_estado = o["nominatim_url"].rsplit("/", 1)[0] + "/status"
    r, e, seg = await _medir(cli.get(url_estado, params={"format": "json"}, timeout=v["timeout_s"]))
    try:
        vivo = e is None and r.status_code == 200 and (r.json() or {}).get("status") == 0
    except ValueError:
        vivo = False
    if vivo:
        filas.append(Chequeo(g, "Nominatim (respaldo de direcciones)", "ok", "responde", segundos=seg))
    else:
        filas.append(Chequeo(g, "Nominatim (respaldo de direcciones)", "aviso",
                             motivo_red(e) if e is not None else f"respondió HTTP {r.status_code} o una página inesperada",
                             "Solo se usa si la nomenclatura oficial no encuentra la dirección.", seg))
    return filas


async def _navegador(cli, p) -> list[Chequeo]:
    g, v = "Página (navegador)", p["params"]["verificacion"]
    nombres = {"unpkg.com": "Librería del mapa (Leaflet)", "tile.openstreetmap.org": "Teselas del mapa OpenStreetMap",
               "fonts.googleapis.com": "Tipografías de la marca"}

    async def uno(url):
        host = url.split("/")[2]
        r, e, seg = await _medir(cli.get(url, timeout=v["timeout_s"]))
        nombre = nombres.get(host, host)
        if e is None and r.status_code == 200:
            return Chequeo(g, nombre, "ok", host, segundos=seg)
        motivo = motivo_red(e) if e is not None else f"respondió HTTP {r.status_code}"
        hacer = ("El mapa no abrirá en la página; la captura por dirección sigue funcionando."
                 if "Tipograf" not in nombre else "La página se verá con letra genérica; todo lo demás funciona.")
        return Chequeo(g, nombre, "aviso", motivo, hacer, seg)

    return list(await asyncio.gather(*[uno(u) for u in v["recursos_navegador"]]))


async def _google(cli, p) -> list[Chequeo]:
    g, v, cfg, s = "Google Maps Platform", p["params"]["verificacion"], p["params"]["google"], p["secretos"]
    out: list[Chequeo] = []
    servidor, navegador = s.get("google_servidor"), s.get("google_navegador")

    if not navegador:
        out.append(Chequeo(g, "Clave de navegador", "omitido", "sin GOOGLE_BROWSER_KEY: el mapa es OpenStreetMap",
                           "Opcional. Pasos en docs/conectar_fuentes.md, sección 3."))
    else:
        if not (navegador.startswith("AIza") and len(navegador) == 39):
            out.append(Chequeo(g, "Clave de navegador", "aviso", "no tiene el formato de una clave de Google",
                               "Revisa que copiaste la clave completa en GOOGLE_BROWSER_KEY."))
        elif navegador == servidor:
            out.append(Chequeo(g, "Clave de navegador", "aviso", "es la misma clave de servidor",
                               "Usa dos claves: la de navegador viaja a la página y debe estar restringida por sitio."))
        else:
            out.append(Chequeo(g, "Clave de navegador", "ok",
                               "definida · Google la valida al abrir la página (si la rechaza, la página avisa y usa OpenStreetMap)"))
        if s.get("google_map_id", "DEMO_MAP_ID") == "DEMO_MAP_ID":
            out.append(Chequeo(g, "Map ID", "aviso", "se usa DEMO_MAP_ID (solo para pruebas)",
                               "Crea un Map ID en Google Cloud › Google Maps Platform › Administración de mapas y ponlo en GOOGLE_MAP_ID."))
        else:
            out.append(Chequeo(g, "Map ID", "ok", "definido"))

    if not servidor:
        out.append(Chequeo(g, "Clave de servidor", "omitido", "sin GOOGLE_SERVER_KEY: Google Places no entra al barrido",
                           "Opcional. Pasos en docs/conectar_fuentes.md, sección 3."))
        return out
    if not (servidor.startswith("AIza") and len(servidor) == 39):
        out.append(Chequeo(g, "Clave de servidor", "aviso", "no tiene el formato de una clave de Google",
                           "Revisa que copiaste la clave completa en GOOGLE_SERVER_KEY."))

    r, e, seg = await _medir(cli.get(cfg["geocode_url"], timeout=v["timeout_s"],
                                     params={"address": v["direccion_prueba"], "region": "co", "key": servidor}))
    if e is not None:
        out.append(Chequeo(g, "Geocoding API", "falla", motivo_red(e), segundos=seg))
    else:
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        estado = d.get("status", f"HTTP {r.status_code}")
        if estado == "OK":
            out.append(Chequeo(g, "Geocoding API", "ok", "la clave funciona (1 llamada de prueba)", segundos=seg))
        elif estado == "ZERO_RESULTS":
            out.append(Chequeo(g, "Geocoding API", "ok", "la clave funciona; la dirección de prueba no dio resultado", segundos=seg))
        else:
            out.append(Chequeo(g, "Geocoding API", "falla", f"{estado}: {str(d.get('error_message', ''))[:140]}",
                               "Habilita Geocoding API en el proyecto y revisa la restricción de la clave "
                               "(API permitidas e IP de este equipo).", seg))

    pv = v["punto_prueba"]
    cuerpo = {"includedTypes": ["school"], "maxResultCount": 1,
              "locationRestriction": {"circle": {"center": {"latitude": pv["lat"], "longitude": pv["lng"]}, "radius": pv["radio_m"]}}}
    r, e, seg = await _medir(cli.post(cfg["places_url"], json=cuerpo, timeout=v["timeout_s"],
                                      headers={"X-Goog-Api-Key": servidor, "X-Goog-FieldMask": "places.id"}))
    if e is not None:
        out.append(Chequeo(g, "Places API (New)", "falla", motivo_red(e), segundos=seg))
    elif r.status_code == 200:
        out.append(Chequeo(g, "Places API (New)", "ok", "la clave funciona (1 llamada de prueba)", segundos=seg))
    else:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        out.append(Chequeo(g, "Places API (New)", "falla", f"HTTP {r.status_code}: {msg[:140]}",
                           "Habilita Places API (New) — no la versión heredada — y permítela en la restricción de la clave.",
                           seg))
    return out


async def fuentes(p: dict) -> list[Chequeo]:
    async with base.nuevo_cliente(p) as cli:
        bloques = await asyncio.gather(_servidor_mapas(cli, p), _osm(cli, p), _navegador(cli, p), _google(cli, p))
    out = [c for b in bloques for c in b]
    cat = p["fuentes"]["fuentes"]
    if not cat.get("portales", {}).get("activa"):
        out.append(Chequeo("Catálogo", "Portales de anuncios", "omitido",
                           "desactivados a propósito: sin ficha de viabilidad (decisión A-08)"))
    if cat.get("oime", {}).get("activa"):
        out.append(Chequeo("Catálogo", "Ofertas OIME 2023", "ok",
                           "activa · condiciones de uso por confirmar con Catastro antes de entregables (A-01)"))
    return out


# ---------------------------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------------------------
def informe(chequeos: list[Chequeo], p: dict) -> str:
    lineas = [f"Venture One · Verificación de prerrequisitos · {datetime.now():%Y-%m-%d %H:%M}",
              f"Carpeta: {p['raiz']}", ""]
    grupo = None
    for c in chequeos:
        if c.grupo != grupo:
            grupo = c.grupo
            lineas += ["", grupo, "-" * len(grupo)]
        seg = f"  ({c.segundos:.1f} s)" if c.segundos is not None else ""
        lineas.append(f"[{ETIQUETA[c.estado]}] {c.nombre:<38} {c.detalle}{seg}")
    pendientes = [c for c in chequeos if c.que_hacer and c.estado in ("falla", "aviso")]
    if pendientes:
        lineas += ["", "Qué hacer", "---------"]
        for i, c in enumerate(sorted(pendientes, key=lambda x: x.estado != "falla"), 1):
            lineas.append(f"{i}. [{c.estado.upper()}] {c.nombre}: {c.que_hacer}")
    n = {e: sum(1 for c in chequeos if c.estado == e) for e in ETIQUETA}
    veredicto = ("Hay fallas: revisa 'Qué hacer'." if n["falla"]
                 else "Listo para capturar." + (" Hay avisos que no impiden capturar." if n["aviso"] else ""))
    lineas += ["", f"Resumen: {n['ok']} en orden · {n['aviso']} avisos · {n['falla']} fallas · {n['omitido']} sin revisar.",
               veredicto]
    return ocultar("\n".join(lineas), p)


def correr(p: dict, en_vivo_claude: bool = True, con_red: bool = True) -> list[Chequeo]:
    chequeos = equipo(p)

    async def todo():
        tarea_claude = asyncio.to_thread(claude, p, en_vivo_claude)
        if con_red:
            c, f = await asyncio.gather(tarea_claude, fuentes(p))
        else:
            c, f = await tarea_claude, [Chequeo("Fuentes", "Red", "omitido", "no se revisó (--sin-red)")]
        return c + f
    return chequeos + asyncio.run(todo())


def main(argv: list[str] | None = None) -> int:
    a = argparse.ArgumentParser(description="Verifica prerrequisitos y fuentes de la aplicación.")
    a.add_argument("--sin-claude", action="store_true", help="no hacer la llamada de prueba a Claude")
    a.add_argument("--sin-red", action="store_true", help="no revisar las fuentes en internet")
    args = a.parse_args(argv)
    p = config.cargar()
    print("Revisando el equipo, Claude y las fuentes (hasta un minuto)...\n", flush=True)
    chequeos = correr(p, not args.sin_claude, not args.sin_red)
    texto = informe(chequeos, p)
    print(texto)
    carpeta = Path(p["rutas"]["exportaciones"])
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        archivo = carpeta / f"verificacion_{datetime.now():%Y%m%d_%H%M}.txt"
        archivo.write_text(texto + "\n", encoding="utf-8")
        print(f"\nInforme guardado en {archivo}")
    except OSError:
        pass
    return 1 if any(c.estado == "falla" for c in chequeos) else 0


if __name__ == "__main__":
    sys.exit(main())
