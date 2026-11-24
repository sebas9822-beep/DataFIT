"""Servidor local de la aplicación única: interfaz HTML + API.

Un solo servidor atiende los dos momentos: la captura del predio y su entorno (Momento 1) y la ficha
del inmueble armada desde el almacén en esquema estrella (Momento 2), más el tablero de supervisión.

Usa solo la biblioteca estándar (no hay que instalar un servidor web). Escucha únicamente en
127.0.0.1: la herramienta es de uso local y las llaves de servidor nunca salen de este equipo.

Quién ve qué:
- comercial: registra clientes, cotiza y ve SOLO sus cotizaciones, capturas y fichas.
- supervisor: ve el consolidado de los comerciales que tiene a cargo, fija metas, registra seguimiento
  y revisa la calidad del almacén. No cotiza (reglas.yaml › supervision.supervisor_puede_cotizar).

Arranque:  python -m captura.api   (o doble clic en iniciar.bat)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import urllib.request
import uuid
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from almacen import etl
from ficha import servicio as fichas
from ficha import hoja_cliente
from gestion import estado_cliente, metas, seguimiento, tablero
from gestion.metas import GestionInvalida

from . import __version__, acceso, admin, bd, clientes, config, consultas, servicio
from .curaduria import anuncios, claude_cliente
from .curaduria.normalizacion import limpiar_nombre
from .fuentes import arcgis, base, catastro, google, portales
from . import geo

TRABAJOS: dict[str, servicio.Progreso] = {}
ORIGENES = {"catastro", "pin_usuario", "google_autocomplete", "google_geocoding", "nominatim"}
ANFITRIONES = {"127.0.0.1", "localhost", "[::1]"}
# Lo único que se atiende sin sesión: la página (que muestra el ingreso), el estado y el ingreso.
PUBLICAS = {("GET", "/"), ("GET", "/index.html"), ("GET", "/health"), ("GET", "/api/sesion"),
            ("POST", "/api/login"), ("POST", "/api/logout")}
# Lo único que atiende la aplicación a un administrador fuera de /api/admin/: no ve cotizaciones ni clientes.
RUTAS_DEL_ADMIN = {"/", "/index.html", "/health", "/api/sesion", "/api/config", "/api/login", "/api/logout"}
USUARIO_RUTA = r"([a-z0-9][a-z0-9._-]{0,59})"
SITIO_RUTA = r"([a-z0-9][a-z0-9_-]{0,59})"


UUID = r"([0-9a-f-]{36})"
PASOS_FICHA = [("inmobiliarias", "Inmobiliarias (RPA con Claude Code)"), ("depuracion", "Depuración de anuncios"),
               ("almacen", "Carga al almacén (ETL)"), ("ficha", "Ficha del inmueble")]


class Invalido(Exception):
    """Entrada que no pasa validación (HTTP 422)."""


class Prohibido(Exception):
    """El usuario tiene sesión pero su rol no puede hacer esto, o el dato es de otro comercial (HTTP 403)."""


def grupos_del_avance(p: dict) -> list[dict]:
    """Los dos bloques del avance: el entorno es del predio y se reusa; el mercado es de la cotización y es del día."""
    dias = p["reglas"]["captura"]["reusar_captura_dias"]
    return [{"id": servicio.GRUPO_ENTORNO, "etiqueta": "Entorno del predio", "nota": f"se reusa {dias} días"},
            {"id": servicio.GRUPO_MERCADO, "etiqueta": "Mercado de la cotización", "nota": "siempre del día"}]


def _contexto_rpa(p: dict, q: dict, barrio: str | None, comuna: str | None, municipio: str | None) -> dict:
    """Lo único que sale hacia la búsqueda: zona, tipos pedidos y rango de área. Ningún dato del cliente."""
    cfg_base = p["almacen"]["bases_mercado"][portales.FUENTE]
    tolerancia = etl.params_ficha(p)["reglas"]["comparables"]["tolerancia_area"]
    area = q["inm_area_m2"]
    return {"fecha": tablero._hoy_local(p).isoformat(), "operacion": cfg_base["operacion_comparable"],
            "municipio": municipio or p["almacen"]["municipio_por_defecto"], "barrio": barrio, "comuna": comuna,
            "tipos": clientes.busqueda_de(p, q["cotizacion_id"]), "tipo_cotizado": q["inm_tipo"],
            "rango_area": [round(area * (1 - tolerancia), 1), round(area * (1 + tolerancia), 1)] if area else None}


def _leer_inmobiliarias(p: dict, contexto: dict, elegidos: list[dict], marca) -> dict:
    """La búsqueda en sí (solo red y Claude Code; no toca la base). Nunca lanza."""
    try:
        marca("en_curso", f"Claude Code busca en {len(elegidos)} sitios lo que pidió el cliente…")
        return portales.buscar(p, contexto, elegidos, avance=lambda hechos, total: marca(
            "en_curso", f"Claude Code leyó {hechos} de {total} grupos de sitios ({len(elegidos)} sitios)…"))
    except Exception as e:                           # noqa: BLE001 - se muestra; no tumba la cotización
        return {"estado": "error", "mensaje": f"{type(e).__name__}: {str(e)[:200]}", "anuncios": [], "por_portal": []}


def _registrar_busqueda(p: dict, captura_id: str, cotizacion_id: str, res: dict, operacion: str, marca) -> dict:
    """Deja en la base lo que trajo la búsqueda y marca el paso. Nunca lanza."""
    try:
        if res.get("anuncios") or res.get("por_portal"):
            portales.guardar(p, captura_id, cotizacion_id, res, operacion)
        clientes.guardar_rpa(p, cotizacion_id, resumen=res)
        marca({"ok": "ok", "sin_resultados": "ok", "omitido": "omitido"}.get(res["estado"], "error"), res["mensaje"])
        return res
    except Exception as e:                           # noqa: BLE001 - se muestra; no tumba la cotización
        detalle = f"{type(e).__name__}: {str(e)[:200]}"
        marca("error", detalle)
        return {"estado": "error", "mensaje": detalle}


def buscar_inmobiliarias(p: dict, cotizacion_id: str, seleccion=None, prog: servicio.Progreso | None = None) -> dict:
    """Anuncios vigentes de lo que pidió el cliente, leídos por Claude Code en las inmobiliarias encendidas.

    Corre en cada cotización, también cuando la captura del entorno se reusó: los anuncios son de hoy.
    Nunca lanza: la cotización y la captura ya están guardadas.
    """
    def marca(estado: str, detalle: str) -> None:
        if prog is not None:
            prog.marcar("inmobiliarias", estado, detalle)

    try:
        q = clientes.cotizacion(p, cotizacion_id)
        if not q or not q["captura_id"]:
            marca("omitido", "la captura no terminó")
            return {"estado": "omitido", "mensaje": "la captura no terminó"}
        elegidos = portales.elegir(p, seleccion if seleccion is not None else bd.de_json(q["portales_json"], None))
        contexto = _contexto_rpa(p, q, q["barrio_nombre"], q["comuna_nombre"], q["municipio_captura"])
    except Exception as e:                           # noqa: BLE001 - se muestra; no tumba la cotización
        detalle = f"{type(e).__name__}: {str(e)[:200]}"
        marca("error", detalle)
        return {"estado": "error", "mensaje": detalle}
    res = _leer_inmobiliarias(p, contexto, elegidos, marca)
    return _registrar_busqueda(p, q["captura_id"], cotizacion_id, res, contexto["operacion"], marca)


def depurar_anuncios(p: dict, cotizacion_id: str, prog: servicio.Progreso | None = None) -> dict:
    """Paso que sigue a la búsqueda: repetidos entre sitios y vigencia de los anuncios de la captura.

    Va antes del ETL para que al almacén lleguen ya enlazados. Nunca lanza.
    """
    def marca(estado: str, detalle: str) -> None:
        if prog is not None:
            prog.marcar("depuracion", estado, detalle)

    try:
        q = clientes.cotizacion(p, cotizacion_id)
        if not q or not q["captura_id"]:
            marca("omitido", "la captura no terminó")
            return {"estado": "omitido", "mensaje": "la captura no terminó"}
        marca("en_curso", "buscando el mismo inmueble publicado en dos sitios y midiendo la vigencia…")
        res = anuncios.depurar(p, q["captura_id"], tablero._hoy_local(p))
        res["mensaje"] = anuncios.texto(res)
        clientes.guardar_depuracion(p, cotizacion_id, res)
        marca("ok" if res["anuncios_validos"] else "omitido", res["mensaje"])
        return res
    except Exception as e:                           # noqa: BLE001 - se muestra; no tumba la cotización
        detalle = f"{type(e).__name__}: {str(e)[:200]}"
        marca("error", detalle)
        return {"estado": "error", "mensaje": detalle}


def mercado_de_la_cotizacion(p: dict, cotizacion_id: str, seleccion=None, prog: servicio.Progreso | None = None) -> dict:
    """Búsqueda en inmobiliarias y depuración de lo encontrado, en ese orden. Devuelve el resultado de la búsqueda."""
    res = buscar_inmobiliarias(p, cotizacion_id, seleccion, prog)
    res["depuracion"] = {k: v for k, v in depurar_anuncios(p, cotizacion_id, prog).items() if k != "pares"}
    return res


def generar_ficha(p: dict, prog: servicio.Progreso, cotizacion_id: str) -> None:
    """Después de la captura: ETL al almacén y ficha del inmueble. Nunca lanza: la captura ya está guardada."""
    if prog.error or not prog.captura_id:
        for k, _ in PASOS_FICHA:
            prog.marcar(k, "omitido", "la captura no terminó")
        return
    if not p["reglas"]["fichas"]["generar_al_cotizar"]:
        for k in ("almacen", "ficha"):
            prog.marcar(k, "omitido", "desactivada en reglas.yaml › fichas")
        return
    prog.marcar("almacen", "en_curso", "extrayendo, depurando y cargando al esquema estrella…")
    try:
        reg = fichas.generar_para_cotizacion(p, cotizacion_id)
    except Exception as e:                           # noqa: BLE001 - se muestra; no tumba la cotización
        detalle = f"{type(e).__name__}: {str(e)[:200]}"
        prog.marcar("almacen", "error", detalle)
        prog.marcar("ficha", "error", "no se generó: falló la carga al almacén")
        return
    if not reg.get("ficha_id"):
        prog.marcar("almacen", "omitido", reg.get("mensaje") or "")
        prog.marcar("ficha", "omitido", reg.get("mensaje") or "")
        return
    prog.marcar("almacen", "ok", f"corrida {reg['corrida_etl']} del ETL")
    res = reg["resumen"]
    if reg["estado"] == "ok":
        prog.marcar("ficha", "ok", f"{res['n']} comparables · confianza {res['confianza']} · {reg['ruta_md']}")
    else:
        prog.marcar("ficha", "parcial", f"{reg['mensaje']} · {reg['ruta_md']}")
    prog.ficha = {k: reg[k] for k in ("ficha_id", "version", "estado", "mensaje", "ruta_md")}


class _BusquedaAdelantada:
    """La búsqueda en inmobiliarias corriendo al tiempo con la captura del entorno.

    Arranca cuando la captura ya sabe el barrio y la comuna (paso "Predio y norma"). Mientras corre no toca la
    base: lo que trae se guarda cuando la captura cierra bien. Si la captura falla, el resultado se descarta.
    """

    def __init__(self, p: dict, cotizacion_id: str, prog: servicio.Progreso) -> None:
        self.p, self.cotizacion_id, self.prog = p, cotizacion_id, prog
        self.hilo: threading.Thread | None = None
        self.resultado: dict | None = None
        self.operacion: str | None = None
        self.vigente = True                          # falso = ya no se muestra su avance (la captura falló)
        self.candado = threading.Lock()

    def marca(self, estado: str, detalle: str) -> None:
        with self.candado:
            if self.vigente:
                self.prog.marcar("inmobiliarias", estado, detalle)

    def arrancar(self, captura_id: str, predio: dict | None, cubierto) -> None:
        """Lo llama la captura al terminar "Predio y norma". Nunca lanza: un tropiezo aquí no tumba la captura."""
        try:
            p = self.p
            q = clientes.cotizacion(p, self.cotizacion_id)
            elegidos = portales.elegir(p, bd.de_json(q["portales_json"], None))
            contexto = _contexto_rpa(p, q, (predio or {}).get("barrio_nombre"), (predio or {}).get("comuna_nombre"),
                                     servicio.municipio_de(cubierto))
            self.operacion = contexto["operacion"]

            def trabajo() -> None:
                self.resultado = _leer_inmobiliarias(p, contexto, elegidos, self.marca)
                self.marca("en_curso", f"lectura terminada: {self.resultado.get('mensaje') or self.resultado['estado']} · "
                                       "se guarda cuando cierre el entorno del predio")

            self.hilo = threading.Thread(target=trabajo, daemon=True, name="rpa-adelantado")
            self.hilo.start()
        except Exception:                            # noqa: BLE001 - sin adelanto: la búsqueda correrá después de la captura
            self.hilo = None

    def recoger(self, captura_id: str) -> dict:
        """Espera el resultado y lo deja en la base (la captura ya cerró bien)."""
        self.hilo.join()
        return _registrar_busqueda(self.p, captura_id, self.cotizacion_id, self.resultado, self.operacion, self.marca)

    def descartar(self) -> None:
        with self.candado:
            self.vigente = False


def _correr(trabajo_id: str, entrada: dict, p: dict, cotizacion_id: str | None = None) -> None:
    prog = TRABAJOS[trabajo_id]
    adelantada = _BusquedaAdelantada(p, cotizacion_id, prog) if cotizacion_id else None
    if adelantada:
        prog.al_ubicar_predio = adelantada.arrancar
    try:
        asyncio.run(servicio.capturar(entrada, p, prog))
    except Exception as e:                           # el error se muestra; no se disfraza de "sin datos"
        prog.error = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
        for paso in prog.pasos.values():
            if paso["estado"] == "en_curso" and paso["grupo"] == servicio.GRUPO_ENTORNO:
                paso.update(estado="error", detalle=prog.error[:200])
        if prog.captura_id and not prog.reusada:
            with bd.conectar(p["rutas"]["bd"]) as con:
                con.execute("UPDATE capturas SET estado='error', avisos_json=? WHERE captura_id=? AND estado='en_curso'",
                            (bd.a_json([prog.error]), prog.captura_id))
    finally:
        if cotizacion_id:
            try:
                clientes.cerrar_cotizacion(p, cotizacion_id, prog.captura_id, prog.reusada, prog.error)
            except Exception as e:                   # noqa: BLE001 - la captura ya quedó guardada
                prog.error = prog.error or f"no se pudo cerrar la cotización: {type(e).__name__}"
            if not prog.error and prog.captura_id:
                if adelantada.hilo is not None:      # la búsqueda arrancó con la captura: solo falta guardarla
                    adelantada.recoger(prog.captura_id)
                else:                                # captura reusada (no hubo paso "Predio y norma"): se busca ahora
                    adelantada.descartar()
                    buscar_inmobiliarias(p, cotizacion_id, prog=prog)
                depurar_anuncios(p, cotizacion_id, prog)
            else:
                adelantada.descartar()               # la captura no terminó: lo que haya traído la búsqueda no se guarda
            generar_ficha(p, prog, cotizacion_id)
        prog.terminado = True


def _numero(valor, minimo: float, maximo: float, nombre: str):
    if valor is None:
        return None
    try:
        f = float(valor)
    except (TypeError, ValueError):
        raise Invalido(f"{nombre} debe ser un número") from None
    if not (minimo <= f <= maximo):
        raise Invalido(f"{nombre} fuera de rango")
    return f


def validar_entrada(cuerpo: dict, p: dict) -> dict:
    if not isinstance(cuerpo, dict):
        raise Invalido("se esperaba un objeto JSON")
    direccion = str(cuerpo.get("direccion") or "").strip()
    lat = _numero(cuerpo.get("lat"), -90, 90, "lat")
    lng = _numero(cuerpo.get("lng"), -180, 180, "lng")
    if (lat is None) != (lng is None):
        raise Invalido("lat y lng van juntos")
    if lat is None and len(direccion) < 3:
        raise Invalido("escribe una dirección o marca un punto en el mapa")
    if len(direccion) > 300:
        raise Invalido("dirección demasiado larga")
    origen = cuerpo.get("origen_coordenada") or ("pin_usuario" if lat is not None else None)
    if origen is not None and origen not in ORIGENES:
        raise Invalido("origen_coordenada no reconocido")
    catalogo = p["fuentes"]["fuentes"]
    fuentes = cuerpo.get("fuentes")
    if fuentes is not None:
        if not isinstance(fuentes, list) or not fuentes or any(f not in catalogo for f in fuentes):
            raise Invalido("lista de fuentes inválida")
    return {"direccion": direccion, "lat": lat, "lng": lng, "origen_coordenada": origen,
            "precision": cuerpo.get("precision") if cuerpo.get("precision") in ("P1", "P2", "P3") else None,
            "place_id": (str(cuerpo["place_id"])[:300] if cuerpo.get("place_id") else None),
            "direccion_normalizada": (str(cuerpo["direccion_normalizada"])[:300] if cuerpo.get("direccion_normalizada") else None),
            "direccion_catastral": (str(cuerpo["direccion_catastral"])[:120] if cuerpo.get("direccion_catastral") else None),
            "conjunto_nombre": str(cuerpo.get("conjunto_nombre") or "").strip()[:120] or None,
            "fuentes": fuentes, "curar_con_claude": bool(cuerpo.get("curar_con_claude", True)),
            "reusar": bool(cuerpo.get("reusar", True))}


def configuracion_publica(p: dict) -> dict:
    """Lo que la página necesita saber. Nunca incluye llaves de servidor."""
    det = claude_cliente.detectar(p)
    cat = p["fuentes"]["fuentes"]
    con_google = google.habilitado(p)
    fuentes = []
    for idf in ("geomedellin", "catastro", "registro_ic", "oime", "osm", "google"):
        f = cat.get(idf)
        if not f:
            continue
        disponible = bool(f.get("activa")) and (idf != "google" or con_google)
        fuentes.append({"id": idf, "nombre": f["nombre"], "disponible": disponible,
                        "nota": "falta GOOGLE_SERVER_KEY en .env" if idf == "google" and not con_google else ""})
    return {
        "version": __version__, "radio_m": p["reglas"]["captura"]["radio_m"],
        "anillos_m": p["reglas"]["captura"]["anillos_m"], "mapa": p["params"]["mapa"],
        "google": {"mapa": bool(p["secretos"]["google_navegador"]), "llave_navegador": p["secretos"]["google_navegador"],
                   "map_id": p["secretos"]["google_map_id"], "places": con_google},
        "claude": {"disponible": det["disponible"], "proveedor": det["proveedor"], "modelo": det.get("modelo"),
                   "detalle": det["detalle"]},
        "fuentes": fuentes,
        "categorias": [{"id": k, **v} for k, v in p["taxonomia"]["categorias"].items()],
        "huella_config": p["huella"],
        "clientes": {k: p["reglas"]["clientes"][k] for k in (
            "documento_min_digitos", "documento_max_digitos", "nombre_min", "nombre_max",
            "exigir_autorizacion", "texto_autorizacion", "tipo_por_defecto", "tipos_documento", "celular")},
        "inmueble": {k: p["reglas"]["inmueble"][k] for k in (
            "obligatorios", "exigir_al_cotizar", "tipos", "area_m2", "etiquetas_estado", "campos_por_tipo", "max_extras",
            "extra_nombre_max", "extra_valor_max", "max_habitaciones", "max_banos", "max_parqueaderos")},
        "busqueda": {k: p["reglas"]["busqueda"][k] for k in ("exigir_al_cotizar", "min_tipos", "barrio_min_letras")},
        "inmobiliarias": {"activo": bool(p["params"]["rpa"]["activo"]), "descartadas": len(p["inmobiliarias"]["descartadas"]),
                          "lista": [{k: x.get(k) for k in ("id", "nombre", "dominio", "tipo", "nivel", "permitida", "por_defecto",
                                                           "lectura", "nota")} for x in portales.catalogo(p)]},
        "vigencia": p["almacen"]["vigencia"],
        "gestion_cliente": estado_cliente.catalogo(p),
        "administracion": {k: p["reglas"]["administracion"][k] for k in (
            "usuario_min", "usuario_max", "nombre_max", "nota_max", "motivo_min_letras", "tipos_sitio", "niveles_sitio")},
        "roles": acceso.ETIQUETA_ROL, "clave_minima": p["reglas"]["acceso"]["clave_minima"],
        "supervision": {k: p["reglas"]["supervision"][k] for k in (
            "tipos_seguimiento", "etiquetas_seguimiento", "nota_max", "dias_tablero_por_defecto", "supervisor_puede_cotizar")},
        "ficha": {"radio_m": etl.params_ficha(p)["reglas"]["entorno"]["radio_busqueda_m"],
                  "tolerancia_area": etl.params_ficha(p)["reglas"]["comparables"]["tolerancia_area"]},
    }


async def _geocodificar(p: dict, direccion: str) -> dict:
    async with base.nuevo_cliente(p) as cli:
        return await servicio.geocodificar(cli, p, direccion)


async def _barrios(p: dict, texto: str) -> list[dict]:
    """Barrios y comunas oficiales cuyo nombre contiene `texto`, con el centro de cada barrio (capa del Catastro)."""
    cfg, capa = p["reglas"]["busqueda"], p["fuentes"]["capas_predio"]["barrio"]
    limpio = re.sub(r"[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 .]", " ", texto).strip().upper()      # va dentro de un LIKE: sin comillas
    if len(limpio) < cfg["barrio_min_letras"]:
        raise Invalido(f"escribe al menos {cfg['barrio_min_letras']} letras del barrio o la comuna")
    plano = limpio.translate(str.maketrans("ÁÉÍÓÚÜ", "AEIOUU"))
    patrones = sorted({limpio, plano})
    donde = " OR ".join(f"UPPER({campo}) LIKE '%{x}%'" for campo in ("nombre_barrio", "nombre_comuna") for x in patrones)
    async with base.nuevo_cliente(p) as cli:
        feats, _, _ = await arcgis.consultar(cli, p, capa["servicio"], capa["capa"], donde=donde,
                                             campos="codigo,nombre_barrio,comuna,nombre_comuna", geometria=True)
    salida = []
    for f in feats:
        a, anillos = arcgis.atributos(f), ((f.get("geometry") or {}).get("rings") or [])
        if not anillos or not a.get("nombre_barrio"):
            continue
        lng, lat = geo.centroide_anillo(max(anillos, key=len))
        salida.append({"barrio": str(a["nombre_barrio"]).title(), "comuna": str(a.get("nombre_comuna") or "").title() or None,
                       "codigo": a.get("codigo"), "lat": round(lat, 7), "lng": round(lng, 7),
                       "coincide_barrio": any(x in str(a["nombre_barrio"]).upper() for x in patrones)})
    salida.sort(key=lambda x: (not x["coincide_barrio"], x["barrio"]))
    return salida[:cfg["barrios_en_lista"]]


async def _punto(p: dict, lat: float, lng: float) -> dict:
    """Lectura rápida del punto: dirección catastral más cercana, barrio y nombre del conjunto."""
    async with base.nuevo_cliente(p) as cli:
        ctx = await catastro.contexto_punto(cli, p, lat, lng)
    pr = ctx["predio"] or {}
    elegido = servicio.elegir_conjunto(ctx["candidatos_conjunto"], p)
    direccion = f"{pr['via_cercana']} # {pr['placa_cercana']}" if pr.get("via_cercana") else None
    return {"en_cobertura": ctx["en_cobertura"], "direccion_catastral": direccion,
            "dist_placa_m": pr.get("dist_placa_m"), "barrio": pr.get("barrio_nombre"), "comuna": pr.get("comuna_nombre"),
            "cbml": pr.get("cbml"), "es_ph": pr.get("es_ph"),
            "conjunto": None if not elegido else {"nombre": limpiar_nombre(elegido["nombre"], p["taxonomia"]),
                                                  "tipo": elegido.get("tipo"),
                                                  "origen": elegido["origen"]}}


def _ficha_publica(p: dict, cotizacion_id: str) -> dict | None:
    """Ficha vigente de la cotización sin el texto (el Markdown se pide aparte)."""
    ficha = fichas.leer(p, cotizacion_id)
    if ficha is None:
        return None
    return {k: v for k, v in ficha.items() if k != "markdown"}


class Manejador(BaseHTTPRequestHandler):
    server_version = "CapturaEntorno/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):            # silencio en consola salvo errores
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), formato % args))

    # ---- utilidades ----
    def _enviar(self, codigo: int, cuerpo: bytes, tipo: str, extra: dict | None = None) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, datos, codigo: int = 200, extra: dict | None = None) -> None:
        self._enviar(codigo, json.dumps(datos, ensure_ascii=False, default=str).encode("utf-8"),
                     "application/json; charset=utf-8", extra)

    def _token(self, p: dict) -> str | None:
        try:
            galleta = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return None
        m = galleta.get(p["params"]["acceso"]["cookie"])
        return m.value if m else None

    def _galleta(self, p: dict, token: str, segundos: int) -> dict:
        nombre = p["params"]["acceso"]["cookie"]
        return {"Set-Cookie": f"{nombre}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={segundos}"}

    def _error(self, codigo: int, mensaje: str) -> None:
        # Tras un error se cierra la conexión: puede quedar un cuerpo sin leer y no debe
        # interpretarse como la solicitud siguiente.
        self.close_connection = True
        self._enviar(codigo, json.dumps({"error": mensaje}, ensure_ascii=False).encode("utf-8"),
                     "application/json; charset=utf-8", {"Connection": "close"})

    def _anfitrion_valido(self) -> bool:
        anfitrion = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return anfitrion in ANFITRIONES

    def _leer_crudo(self) -> bytes | None:
        """Lee siempre el cuerpo completo de un POST: si queda sin leer, se mezclaría con la petición siguiente."""
        try:
            largo = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            largo = -1
        if largo < 0 or largo > 200_000:
            self.close_connection = True
            return None
        return self.rfile.read(largo) if largo else b""

    def _cuerpo(self) -> dict:
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise Invalido("se esperaba Content-Type: application/json")
        if self._crudo is None:
            raise Invalido("cuerpo demasiado grande")
        try:
            return json.loads(self._crudo.decode("utf-8") or "{}")
        except ValueError:
            raise Invalido("JSON inválido") from None

    # ---- permisos ----
    @property
    def es_supervisor(self) -> bool:
        return bool(self.usuario) and self.usuario.get("rol") == "supervisor"

    @property
    def es_admin(self) -> bool:
        return bool(self.usuario) and self.usuario.get("rol") == "admin"

    def _solo_supervisor(self) -> None:
        if not self.es_supervisor:
            raise Prohibido("Solo un supervisor puede ver o cambiar esto")

    def _solo_admin(self) -> str:
        """Devuelve el usuario del administrador que hace el cambio (va a la bitácora)."""
        if not self.es_admin:
            raise Prohibido("Solo el administrador puede ver o cambiar esto")
        return self.usuario["usuario"]

    def _solo_quien_cotiza(self, p: dict) -> None:
        if self.es_admin:
            raise Prohibido("El administrador no registra clientes ni cotiza: entra con un usuario comercial")
        if self.es_supervisor and not p["reglas"]["supervision"]["supervisor_puede_cotizar"]:
            raise Prohibido("El supervisor no registra clientes ni cotiza: entra con un usuario comercial")

    def _a_cargo(self, p: dict, comercial: str | None = None) -> list[str] | None:
        """Comerciales cuyos datos puede ver el usuario (None = todos). Con `comercial`, lo valida y lo devuelve solo."""
        permitidos = acceso.a_cargo(p, self.usuario)
        if comercial:
            comercial = acceso.normalizar_usuario(comercial)
            if permitidos is not None and comercial not in permitidos:
                raise Prohibido("Ese comercial no está a tu cargo")
            return [comercial]
        return permitidos

    def _cotizacion(self, p: dict, cotizacion_id: str, solo_duenio: bool = False) -> dict:
        """La cotización, si el usuario puede verla. Un comercial nunca llega a la de otro (responde 404)."""
        q = clientes.cotizacion(p, cotizacion_id)
        if q is None or not acceso.puede_ver(p, self.usuario, q["comercial"]):
            raise LookupError("cotización no encontrada")
        if solo_duenio and q["comercial"] != self.usuario["usuario"]:
            raise Prohibido("Solo el comercial dueño de la cotización puede cambiarla")
        return q

    def _capturas_permitidas(self, p: dict) -> set[str] | None:
        """Capturas que el usuario puede abrir: las de sus cotizaciones (o las de quienes tiene a cargo)."""
        permitidos = acceso.a_cargo(p, self.usuario)
        if permitidos is None:
            return None
        ids: set[str] = set()
        for u in permitidos:
            ids |= clientes.capturas_de(p, u)
        return ids

    def _captura_permitida(self, p: dict, captura_id: str) -> None:
        permitidas = self._capturas_permitidas(p)
        if permitidas is not None and captura_id not in permitidas:
            raise LookupError("captura no encontrada")

    # ---- rutas ----
    def do_GET(self) -> None:                         # noqa: N802 (nombre impuesto por http.server)
        self._atender("GET")

    def do_POST(self) -> None:                        # noqa: N802
        self._atender("POST")

    def _atender(self, metodo: str) -> None:
        if not self._anfitrion_valido():
            return self._error(403, "solo se atiende en este equipo (127.0.0.1)")
        p = config.cargar()
        url = urlparse(self.path)
        ruta, consulta = url.path.rstrip("/") or "/", parse_qs(url.query)
        self._crudo = self._leer_crudo() if metodo == "POST" else b""
        self.usuario = acceso.sesion(p, self._token(p))
        if self.usuario is None and (metodo, ruta) not in PUBLICAS:
            self.close_connection = True
            return self._error(401, "Inicia sesión para continuar")
        if self.es_admin and ruta not in RUTAS_DEL_ADMIN and not ruta.startswith("/api/admin/"):
            return self._error(403, "El administrador gestiona la plataforma: no ve cotizaciones ni clientes")
        try:
            if metodo == "GET":
                self._get(p, ruta, consulta)
            else:
                self._post(p, ruta)
        except (Invalido, clientes.ClienteInvalido, GestionInvalida, admin.AdminInvalido) as e:
            self._error(422, str(e))
        except Prohibido as e:
            self._error(403, str(e) or "No tienes permiso para esto")
        except LookupError as e:
            self._error(404, str(e) or "no encontrado")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {str(e)[:300]}")

    def _get(self, p: dict, ruta: str, consulta: dict) -> None:
        if ruta in ("/", "/index.html"):
            return self._enviar(200, p["rutas"]["web"].read_bytes(), "text/html; charset=utf-8")
        if ruta == "/health":
            return self._json({"ok": True, "version": __version__, "bd": str(p["rutas"]["bd"]),
                               "google": google.habilitado(p), "claude": claude_cliente.detectar(p)["disponible"]})
        if ruta == "/api/sesion":
            return self._json({"autenticado": bool(self.usuario), **(self.usuario or {})})
        if ruta == "/api/config":
            return self._json({**configuracion_publica(p), "usuario": self.usuario})
        if ruta == "/api/cotizaciones":
            if not self.es_supervisor:
                return self._json(clientes.listar_cotizaciones(p, self.usuario["usuario"]))
            return self._json(clientes.listar_todas(p, self._a_cargo(p, (consulta.get("comercial") or [None])[0]),
                                                    (consulta.get("desde") or [None])[0], (consulta.get("hasta") or [None])[0]))
        m = re.fullmatch(rf"/api/cotizaciones/{UUID}", ruta)
        if m:
            q = self._cotizacion(p, m[1])
            ficha = _ficha_publica(p, m[1])
            return self._json({"cotizacion": clientes._fila_lista(clientes.fila_lista(p, m[1]), p), "ficha": ficha,
                               "gestion": estado_cliente.de_cotizacion(p, m[1], ficha),
                               "es_propia": q["comercial"] == self.usuario["usuario"]})
        m = re.fullmatch(rf"/api/cotizaciones/{UUID}/ficha/cliente", ruta)
        if m:                                        # la hoja para el cliente, lista para imprimir o guardar como PDF
            self._cotizacion(p, m[1])
            ficha = fichas.leer(p, m[1])
            pagina = hoja_cliente.de_cotizacion(p, ficha, imprimir=(consulta.get("imprimir") or [""])[0] == "1") if ficha else None
            if pagina is None:
                return self._error(404, "esta cotización todavía no tiene ficha para imprimir")
            return self._enviar(200, pagina.encode("utf-8"), "text/html; charset=utf-8")
        m = re.fullmatch(rf"/api/cotizaciones/{UUID}/ficha", ruta)
        if m:
            self._cotizacion(p, m[1])
            ficha = fichas.leer(p, m[1])
            if ficha is None:
                return self._error(404, "esta cotización todavía no tiene ficha")
            formato = (consulta.get("descargar") or [None])[0]
            if formato in ("md", "json"):
                ruta_archivo = p["rutas"]["fichas"] / ficha["ruta_md" if formato == "md" else "ruta_json"]
                if not ruta_archivo.exists():
                    return self._error(404, "el archivo de la ficha no está en la carpeta del cliente")
                tipo = "text/markdown; charset=utf-8" if formato == "md" else "application/json; charset=utf-8"
                return self._enviar(200, ruta_archivo.read_bytes(), tipo,
                                    {"Content-Disposition": f'attachment; filename="{ruta_archivo.name}"'})
            return self._json(ficha)
        m = re.fullmatch(r"/api/clientes/id/(\d{1,12})/historia", ruta)
        if m:                                        # la historia de un cliente con quien pregunta: sus fichas y en qué va cada una
            de_quien = None if self.es_supervisor else self.usuario["usuario"]
            historia = estado_cliente.historia_del_cliente(p, int(m[1]), de_quien)
            a_cargo = self._a_cargo(p)
            return self._json({"historia": [h for h in historia if a_cargo is None or h["comercial"] in a_cargo]})
        m = re.fullmatch(r"/api/clientes/([0-9.\- ]{1,20})", ruta)
        if m:
            de_quien = None if self.es_supervisor else self.usuario["usuario"]
            encontrado = clientes.buscar(p, m[1], de_quien)
            if encontrado:                           # su historia: cada cotización con el estado de la ficha con el cliente
                encontrado["historia"] = estado_cliente.historia_del_cliente(p, encontrado["cliente_id"], de_quien)
            return self._json({"encontrado": bool(encontrado), "cliente": encontrado})
        if ruta == "/api/punto":
            lat = _numero((consulta.get("lat") or [None])[0], -90, 90, "lat")
            lng = _numero((consulta.get("lng") or [None])[0], -180, 180, "lng")
            if lat is None or lng is None:
                raise Invalido("faltan lat y lng")
            return self._json(asyncio.run(_punto(p, lat, lng)))
        if ruta == "/api/barrios":
            return self._json({"barrios": asyncio.run(_barrios(p, str((consulta.get("q") or [""])[0])))})
        if ruta == "/api/capturas":
            return self._json({"capturas": consultas.listar_capturas(p, permitidas=self._capturas_permitidas(p))})
        m = re.fullmatch(rf"/api/trabajos/{UUID}", ruta)
        if m:
            prog = TRABAJOS.get(m[1])
            if prog is None or prog.usuario not in (None, self.usuario["usuario"]):
                return self._error(404, "trabajo no encontrado")
            return self._json(prog.a_dict())
        m = re.fullmatch(rf"/api/capturas/{UUID}", ruta)
        if m:
            self._captura_permitida(p, m[1])
            datos = consultas.leer_captura(p, m[1])
            return self._json(datos) if datos else self._error(404, "captura no encontrada")
        m = re.fullmatch(rf"/api/capturas/{UUID}/exportar", ruta)
        if m:
            self._captura_permitida(p, m[1])
            tabla = (consulta.get("tabla") or ["entorno"])[0]
            texto = consultas.exportar_csv(p, m[1], tabla)
            if texto is None:
                raise Invalido("tabla no exportable")
            return self._enviar(200, texto.encode("utf-8"), "text/csv; charset=utf-8",
                                {"Content-Disposition": f'attachment; filename="captura_{m[1][:8]}_{tabla}.csv"'})
        # ---- gestión: lo propio del comercial ----
        if ruta == "/api/mi-gestion":
            return self._json(tablero.mi_gestion(p, self.usuario))
        if ruta == "/api/seguimientos":
            estado = (consulta.get("estado") or [None])[0]
            if estado and estado not in p["reglas"]["supervision"]["estados_seguimiento"]:
                raise Invalido("estado de seguimiento no reconocido")
            a_cargo = self._a_cargo(p, (consulta.get("comercial") or [None])[0])
            return self._json({"seguimientos": seguimiento.listar(p, a_cargo, estado)})
        # ---- supervisión ----
        if ruta == "/api/tablero":
            self._solo_supervisor()
            return self._json(tablero.tablero(p, self.usuario, (consulta.get("desde") or [None])[0],
                                              (consulta.get("hasta") or [None])[0], (consulta.get("comercial") or [None])[0] or None))
        if ruta == "/api/supervision/comerciales":
            self._solo_supervisor()
            return self._json({"comerciales": acceso.listar_comerciales(p, self.usuario)})
        if ruta == "/api/supervision/clientes":
            self._solo_supervisor()
            return self._json({"clientes": clientes.listar_clientes(p, self._a_cargo(p))})
        if ruta == "/api/metas":
            self._solo_supervisor()
            mes = (consulta.get("mes") or [None])[0] or tablero._hoy_local(p).isoformat()[:7]
            return self._json({"mes": metas.validar_mes(mes), "metas": metas.listar(p, mes, self._a_cargo(p))})
        if ruta == "/api/almacen":
            self._solo_supervisor()
            return self._json(tablero.estado_almacen(p))
        # ---- administración de la plataforma ----
        if ruta == "/api/admin/usuarios":
            self._solo_admin()
            return self._json({"usuarios": admin.listar_usuarios(p), "roles": acceso.ETIQUETA_ROL})
        if ruta == "/api/admin/inmobiliarias":
            self._solo_admin()
            return self._json(admin.leer_catalogo(p))
        if ruta == "/api/admin/respaldos":
            self._solo_admin()
            return self._json({"respaldos": admin.listar_respaldos(p), "carpeta": str(p["rutas"]["respaldos"])})
        if ruta == "/api/admin/bitacora":
            self._solo_admin()
            return self._json({"eventos": admin.bitacora(p)})
        if ruta == "/api/admin/sistema":
            self._solo_admin()
            return self._json(admin.estado_sistema(p))
        self._error(404, "ruta no encontrada")

    def _post(self, p: dict, ruta: str) -> None:
        if ruta == "/api/login":
            cuerpo = self._cuerpo()
            try:
                quien = acceso.autenticar(p, cuerpo.get("usuario"), cuerpo.get("clave"))
            except acceso.AccesoDenegado as e:
                self.close_connection = True
                return self._error(401, str(e))
            token = acceso.crear_sesion(p, quien["usuario"])
            segundos = int(p["params"]["acceso"]["sesion_horas"] * 3600)
            return self._json({"autenticado": True, **quien}, 200, self._galleta(p, token, segundos))
        if ruta == "/api/logout":
            acceso.cerrar_sesion(p, self._token(p))
            return self._json({"autenticado": False}, 200, self._galleta(p, "", 0))
        if ruta == "/api/clientes":
            cuerpo = self._cuerpo()
            self._solo_quien_cotiza(p)
            cliente = clientes.registrar(p, self.usuario["usuario"], cuerpo.get("documento"), cuerpo.get("nombre"),
                                         bool(cuerpo.get("autoriza")), cuerpo.get("tipo_documento"), cuerpo.get("celular"))
            return self._json({"cliente": cliente})
        if ruta == "/api/geocodificar":
            direccion = str(self._cuerpo().get("direccion") or "").strip()
            if not (3 <= len(direccion) <= 300):
                raise Invalido("escribe una dirección")
            return self._json(asyncio.run(_geocodificar(p, direccion)))
        if ruta == "/api/capturas":
            cuerpo = self._cuerpo()
            self._solo_quien_cotiza(p)
            entrada = validar_entrada(cuerpo, p)
            cliente = clientes.para_capturar(p, cuerpo.get("cliente_id"))
            inmueble = clientes.validar_inmueble(cuerpo.get("inmueble"), p, exigir=p["reglas"]["inmueble"]["exigir_al_cotizar"])
            busca = clientes.validar_busqueda(cuerpo.get("busca"), p, inmueble.get("inm_tipo"),
                                              exigir=p["reglas"]["busqueda"]["exigir_al_cotizar"])
            try:
                encendidas = portales.validar_seleccion(cuerpo.get("portales"), p)
            except ValueError as e:
                raise Invalido(str(e)) from None
            cotizacion_id = clientes.abrir_cotizacion(p, self.usuario["usuario"], cliente,
                                                      entrada["direccion"] or entrada.get("direccion_catastral"), inmueble, busca)
            clientes.guardar_rpa(p, cotizacion_id, portales=[x["id"] for x in portales.elegir(p, encendidas)])
            trabajo_id = str(uuid.uuid4())
            prog = TRABAJOS[trabajo_id] = servicio.Progreso()
            prog.usuario = self.usuario["usuario"]
            prog.grupos = grupos_del_avance(p)
            for paso, etiqueta in PASOS_FICHA:
                prog.agregar_paso(paso, etiqueta)
            threading.Thread(target=_correr, args=(trabajo_id, entrada, p, cotizacion_id), daemon=True).start()
            return self._json({"trabajo_id": trabajo_id, "cotizacion_id": cotizacion_id}, 202)
        m = re.fullmatch(rf"/api/cotizaciones/{UUID}/inmueble", ruta)
        if m:                                        # completar o corregir los datos del inmueble y rehacer la ficha
            cuerpo = self._cuerpo()
            self._cotizacion(p, m[1], solo_duenio=True)
            inmueble = clientes.validar_inmueble(cuerpo.get("inmueble") or cuerpo, p, exigir=True)
            clientes.actualizar_inmueble(p, m[1], inmueble,
                                         clientes.validar_busqueda(cuerpo.get("busca"), p, inmueble.get("inm_tipo"), exigir=False))
            mercado_de_la_cotizacion(p, m[1])        # ya se sabe qué buscar: anuncios vigentes de ese tipo, depurados
            fichas.generar_para_cotizacion(p, m[1])
            return self._json({"ficha": _ficha_publica(p, m[1])})
        m = re.fullmatch(rf"/api/cotizaciones/{UUID}/inmobiliarias", ruta)
        if m:                                        # volver a buscar anuncios vigentes y rehacer la ficha
            cuerpo = self._cuerpo()
            self._cotizacion(p, m[1], solo_duenio=True)
            try:
                encendidas = portales.validar_seleccion(cuerpo.get("portales"), p)
            except ValueError as e:
                raise Invalido(str(e)) from None
            if encendidas is not None:
                clientes.guardar_rpa(p, m[1], portales=[x["id"] for x in portales.elegir(p, encendidas)])
            res = mercado_de_la_cotizacion(p, m[1], encendidas)
            fichas.generar_para_cotizacion(p, m[1])
            return self._json({"rpa": {k: v for k, v in res.items() if k != "anuncios"}, "ficha": _ficha_publica(p, m[1])})
        m = re.fullmatch(rf"/api/cotizaciones/{UUID}/ficha", ruta)
        if m:                                        # regenerar: queda una versión nueva; la anterior se conserva
            self._cuerpo()
            self._cotizacion(p, m[1])
            reg = fichas.generar_para_cotizacion(p, m[1])
            if not reg.get("ficha_id"):
                raise Invalido(reg.get("mensaje") or "no se pudo generar la ficha")
            return self._json({"ficha": _ficha_publica(p, m[1])})
        m = re.fullmatch(rf"/api/cotizaciones/{UUID}/estado-cliente", ruta)
        if m:                                        # el comercial registra en qué va la ficha con el cliente
            cuerpo = self._cuerpo()
            q = self._cotizacion(p, m[1], solo_duenio=True)
            return self._json({"gestion": estado_cliente.registrar(p, q, self.usuario["usuario"], cuerpo)})
        # ---- administración de la plataforma ----
        if ruta == "/api/admin/usuarios":
            cuerpo = self._cuerpo()
            return self._json({"usuario": admin.crear_usuario(p, self._solo_admin(), cuerpo)}, 201)
        m = re.fullmatch(rf"/api/admin/usuarios/{USUARIO_RUTA}", ruta)
        if m:
            cuerpo = self._cuerpo()
            return self._json({"usuario": admin.editar_usuario(p, self._solo_admin(), m[1], cuerpo)})
        m = re.fullmatch(rf"/api/admin/usuarios/{USUARIO_RUTA}/clave", ruta)
        if m:
            cuerpo = self._cuerpo()
            return self._json({"usuario": admin.restablecer_clave(p, self._solo_admin(), m[1], cuerpo.get("clave"))})
        m = re.fullmatch(rf"/api/admin/usuarios/{USUARIO_RUTA}/eliminar", ruta)
        if m:
            self._cuerpo()
            return self._json({"usuario": admin.eliminar_usuario(p, self._solo_admin(), m[1])})
        if ruta == "/api/admin/inmobiliarias":
            cuerpo = self._cuerpo()
            return self._json({"sitio": admin.guardar_sitio(p, self._solo_admin(), cuerpo)}, 201)
        m = re.fullmatch(rf"/api/admin/inmobiliarias/{SITIO_RUTA}", ruta)
        if m:
            cuerpo = self._cuerpo()
            return self._json({"sitio": admin.guardar_sitio(p, self._solo_admin(), cuerpo, m[1])})
        m = re.fullmatch(rf"/api/admin/inmobiliarias/{SITIO_RUTA}/eliminar", ruta)
        if m:
            cuerpo = self._cuerpo()
            return self._json({"descartada": admin.eliminar_sitio(p, self._solo_admin(), m[1], cuerpo.get("motivo"))})
        if ruta == "/api/admin/respaldos":
            self._cuerpo()
            return self._json({"respaldo": admin.crear_respaldo(p, self._solo_admin())}, 201)
        m = re.fullmatch(r"/api/admin/respaldos/(respaldo_\d{8}_\d{6}\.zip)/eliminar", ruta)
        if m:
            self._cuerpo()
            admin.eliminar_respaldo(p, self._solo_admin(), m[1])
            return self._json({"eliminado": m[1]})
        if ruta == "/api/metas":
            cuerpo = self._cuerpo()
            self._solo_supervisor()
            comercial = self._a_cargo(p, cuerpo.get("comercial") or "?")[0]
            meta = metas.fijar(p, self.usuario["usuario"], comercial, cuerpo.get("mes"), cuerpo.get("meta_cotizaciones"),
                               cuerpo.get("meta_clientes"))
            return self._json({"meta": meta})
        if ruta == "/api/seguimientos":
            cuerpo = self._cuerpo()
            self._solo_supervisor()
            comercial = self._a_cargo(p, cuerpo.get("comercial") or "?")[0]
            nota = seguimiento.crear(p, self.usuario["usuario"], comercial, cuerpo.get("tipo"), cuerpo.get("nota"),
                                     cuerpo.get("cotizacion_id") or None, cuerpo.get("fecha_compromiso") or None)
            return self._json({"seguimiento": nota})
        m = re.fullmatch(r"/api/seguimientos/(\d{1,12})/cerrar", ruta)
        if m:
            self._cuerpo()
            self._solo_supervisor()
            hecho = seguimiento.cerrar(p, self.usuario["usuario"], m[1], self._a_cargo(p))
            return self._json({"ok": True}) if hecho else self._error(404, "seguimiento no encontrado")
        if ruta == "/api/almacen/etl":
            self._cuerpo()
            self._solo_supervisor()
            res = etl.ejecutar(p, "completo")
            return self._json({"corrida_id": res["corrida_id"], "estado": res["estado"], "controles": res["controles"]})
        m = re.fullmatch(r"/api/capturas/([0-9a-f-]{36})/conjunto", ruta)
        if m:
            self._captura_permitida(p, m[1])
            nombre = str(self._cuerpo().get("nombre") or "").strip()[:120]
            with bd.conectar(p["rutas"]["bd"]) as con:
                hecho = con.execute(
                    "UPDATE capturas SET conjunto_nombre=?, conjunto_origen=?, conjunto_expira_en=NULL WHERE captura_id=?",
                    (nombre or None, "usuario" if nombre else None, m[1])).rowcount
                if hecho:
                    bd.insertar(con, "curaduria_log", {
                        "captura_id": m[1], "item_ref": "conjunto", "etapa": "edicion", "campo": "conjunto_nombre",
                        "valor_despues": nombre or None, "regla": "editado_por_usuario", "origen": "usuario",
                        "aceptado": 1, "creado_en": bd.ahora()})
            return self._json({"ok": bool(hecho)}) if hecho else self._error(404, "captura no encontrada")
        if ruta == "/api/mantenimiento/purgar":
            self._cuerpo()
            return self._json(bd.purgar_expirados(p))
        self._error(404, "ruta no encontrada")


class _Servidor(ThreadingHTTPServer):
    # En Windows, reutilizar la dirección dejaría que dos copias escucharan en el mismo puerto.
    allow_reuse_address = os.name != "nt"


def crear_servidor(p: dict, puerto: int | None = None) -> ThreadingHTTPServer:
    bd.iniciar(p)
    bd.purgar_expirados(p)
    try:                                             # el tablero y las fichas leen del almacén: que exista desde el arranque
        etl.refrescar_gestion(p)
    except Exception as e:                           # noqa: BLE001 - la captura funciona aunque el almacén no esté
        sys.stderr.write(f"Aviso: no se pudo preparar el almacén ({type(e).__name__}: {str(e)[:200]})\n")
    cfg = p["params"]["servidor"]
    servidor = _Servidor((cfg["host"], cfg["puerto"] if puerto is None else puerto), Manejador)
    servidor.daemon_threads = True
    return servidor


def ya_abierta(host: str, puerto: int) -> bool:
    """¿Ya hay una copia de esta aplicación atendiendo en ese puerto?"""
    try:
        with urllib.request.urlopen(f"http://{host}:{puerto}/health", timeout=3) as r:
            dato = json.loads(r.read().decode("utf-8"))
        return bool(dato.get("ok")) and "version" in dato
    except Exception:
        return False


def main() -> None:
    analizador = argparse.ArgumentParser(description="Venture One · Captura, almacén y ficha del inmueble")
    analizador.add_argument("--puerto", type=int, default=None)
    analizador.add_argument("--sin-navegador", action="store_true")
    args = analizador.parse_args()
    p = config.cargar()
    cfg = p["params"]["servidor"]
    puerto = cfg["puerto"] if args.puerto is None else args.puerto
    # Segundo doble clic en el lanzador: si ya está abierta, basta con mostrarla en el navegador.
    if puerto and ya_abierta(cfg["host"], puerto):
        print(f"La aplicación ya está abierta en http://{cfg['host']}:{puerto}")
        if cfg["abrir_navegador"] and not args.sin_navegador:
            webbrowser.open(f"http://{cfg['host']}:{puerto}")
        return
    try:
        servidor = crear_servidor(p, args.puerto)
    except OSError as e:
        raise SystemExit(f"No se pudo abrir el puerto {puerto} ({e}). Otro programa lo está usando: "
                         f"ciérralo o arranca con  --puerto {puerto + 1}") from e
    url = f"http://{servidor.server_address[0]}:{servidor.server_address[1]}"
    det = claude_cliente.detectar(p)
    print(f"Venture One · Captura, almacén y ficha del inmueble {__version__}")
    print(f"  Interfaz : {url}")
    print(f"  Base     : {p['rutas']['bd']}")
    print(f"  Almacén  : {p['rutas']['almacen']}")
    print(f"  Fichas   : {p['rutas']['fichas']}")
    print(f"  Google   : {'activo' if google.habilitado(p) else 'sin llave (opcional; ver .env.example)'}")
    print(f"  Claude   : {det['detalle'] if det['disponible'] else 'no disponible - ' + det['detalle']}")
    print("  Ctrl+C para detener.")
    if p["params"]["servidor"]["abrir_navegador"] and not args.sin_navegador:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
