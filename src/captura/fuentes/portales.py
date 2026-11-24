"""RPA de inmobiliarias: Claude Code busca en los portales encendidos los anuncios vigentes de lo que pidió el cliente.

Qué hace y qué no:
- Solo visita los dominios de fuentes.yaml › portales_inmobiliarios que el comercial dejó encendidos y que están
  permitidos. La lista viaja como permiso de la herramienta de lectura web, no como sugerencia.
- No evade bloqueos: un sitio que responde 403/429, pide CAPTCHA o no deja leer se anota y se sigue con el otro.
- No recibe datos del cliente: solo municipio, barrio, tipos de inmueble y rango de área.
- Todo lo que devuelve se valida aquí (dominio, dirección web, números, tipo). Lo que no pasa queda con su motivo.
- El precio es pedido. Si el sitio no muestra la fecha de publicación queda vacía y se guarda la fecha en que se vio.
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .. import bd
from ..curaduria import claude_cliente

FUENTE = "portales"

SISTEMA = """Eres un asistente de captura de datos de mercado inmobiliario en Colombia. Tu única tarea es leer
anuncios publicados en los sitios web indicados y devolverlos en la estructura pedida.

Reglas:
1. Usa solo los sitios de la lista. No abras otros dominios.
2. Devuelve únicamente anuncios que hayas visto en esos sitios durante esta búsqueda, con su dirección web exacta.
   No inventes ni completes datos: lo que el sitio no muestra va en null.
3. Si un sitio responde 403 o 429, pide CAPTCHA o no deja leer los anuncios, no insistas ni busques otra vía:
   repórtalo en "portales" con estado "bloqueado" o "no_legible" y sigue con el siguiente.
4. Solo anuncios de la operación, los tipos de inmueble y la zona pedidos. Si se indica un rango de área, respétalo.
5. fecha_publicacion solo si el sitio la muestra (AAAA-MM-DD); si muestra "hace N días", calcúlala con la fecha de hoy.
6. No opines sobre los inmuebles ni sobre su conveniencia como inversión. Solo datos."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "anuncios": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "portal": {"type": "string"}, "url": {"type": "string"}, "titulo": {"type": ["string", "null"]},
                "tipo_inmueble": {"type": "string"}, "precio_cop": {"type": ["number", "null"]},
                "area_m2": {"type": ["number", "null"]}, "habitaciones": {"type": ["integer", "null"]},
                "banos": {"type": ["integer", "null"]}, "parqueaderos": {"type": ["integer", "null"]},
                "barrio": {"type": ["string", "null"]}, "fecha_publicacion": {"type": ["string", "null"]}},
            "required": ["portal", "url", "tipo_inmueble", "precio_cop", "area_m2"]}},
        "portales": {"type": "array", "items": {
            "type": "object",
            "properties": {"portal": {"type": "string"},
                           "estado": {"type": "string", "enum": ["ok", "sin_resultados", "bloqueado", "no_legible"]},
                           "anuncios_vistos": {"type": ["integer", "null"]}, "detalle": {"type": ["string", "null"]}},
            "required": ["portal", "estado"]}}},
    "required": ["anuncios", "portales"],
}


def catalogo(p: dict) -> list[dict]:
    return [dict(x) for x in p["inmobiliarias"]["sitios"]]


def elegir(p: dict, pedidos) -> list[dict]:
    """Sitios que se consultan: los permitidos que el comercial dejó encendidos.

    `pedidos` None = no dijo nada: los que el catálogo trae encendidos (`por_defecto`).
    """
    permitidos = [x for x in catalogo(p) if x.get("permitida", True)]
    if pedidos is None:
        return [x for x in permitidos if x.get("por_defecto", True)]
    pedidos = {str(x) for x in pedidos}
    return [x for x in permitidos if x["id"] in pedidos]


def validar_seleccion(pedidos, p: dict):
    """Lista de ids que mandó la pantalla. None = no dijo nada (todas encendidas); [] = todas apagadas."""
    if pedidos is None:
        return None
    ids = {x["id"] for x in catalogo(p)}
    if not isinstance(pedidos, list) or any(str(x) not in ids for x in pedidos):
        raise ValueError("lista de inmobiliarias inválida")
    return [str(x) for x in pedidos]


def _dominio(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _portal_de(url: str, portales: list[dict]) -> dict | None:
    host = _dominio(url)
    return next((x for x in portales if host == x["dominio"] or host.endswith("." + x["dominio"])), None)


def _barrio_url(texto: str | None) -> str:
    """Barrio como suele ir en una URL: minúsculas, sin tildes y con guiones."""
    plano = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plano).strip("-")


def mensaje(contexto: dict, portales: list[dict], p: dict) -> str:
    cfg = p["params"]["rpa"]
    rango = contexto.get("rango_area")
    lineas = [
        f"Fecha de hoy: {contexto['fecha']}.",
        f"Operación: {contexto['operacion']}.",
        "Zona: " + ", ".join(x for x in (contexto.get("barrio") and f"barrio {contexto['barrio']}",
                                          contexto.get("comuna") and f"comuna {contexto['comuna']}",
                                          contexto["municipio"], "Colombia") if x) + ".",
        "Tipos de inmueble pedidos: " + ", ".join(t.replace("_", " ") for t in contexto["tipos"]) + ".",
    ]
    if rango and contexto.get("tipo_cotizado"):
        lineas.append(f"Para {contexto['tipo_cotizado'].replace('_', ' ')}: área entre {rango[0]:g} y {rango[1]:g} m². "
                      "Para los demás tipos no hay filtro de área.")
    lineas.append("Sitios permitidos (abre solo estos):")
    for x in portales:
        pista = ""
        if x.get("busqueda"):
            pista = (f" · búsqueda que funciona para apartamento en venta en Laureles: {x['busqueda']} "
                     "(cambia el barrio y el tipo por los pedidos; si la URL no filtra por barrio, filtra tú al leer)")
        lineas.append(f"- {x['nombre']} ({x['dominio']}){pista}")
    if contexto.get("barrio"):
        lineas.append(f"Barrio para las URL: {_barrio_url(contexto['barrio'])}.")
    lineas += [
        "Empieza cada sitio por su URL de búsqueda. Una o dos páginas por sitio bastan: no recorras todo el inventario.",
        f"Trae hasta {cfg['max_anuncios']} anuncios en total, repartidos entre los sitios y los tipos pedidos, empezando por "
        "los del barrio indicado y luego los de barrios vecinos de la misma comuna.",
        "En tipo_inmueble usa exactamente uno de: " + ", ".join(contexto["tipos"]) + ".",
        "En portal escribe el dominio del sitio. Reporta en 'portales' el estado de cada sitio de la lista.",
    ]
    return "\n".join(lineas)


def _entero(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fecha(v) -> str | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(v or "").strip())
    return m.group(0) if m else None


def validar(crudos, contexto: dict, portales: list[dict], p: dict) -> list[dict]:
    """Normaliza lo que devolvió la búsqueda. Cada fila sale con `motivo_exclusion` (None = sirve como oferta)."""
    amin, amax = p["reglas"]["ofertas"]["area_valida_m2"]
    vistos, filas = set(), []
    for a in crudos if isinstance(crudos, list) else []:
        if not isinstance(a, dict):
            continue
        url = str(a.get("url") or "").strip()
        portal = _portal_de(url, portales) if url.lower().startswith("https://") else None
        tipo = re.sub(r"\s+", "_", str(a.get("tipo_inmueble") or "").strip().lower())
        try:
            precio = float(a["precio_cop"]) if a.get("precio_cop") is not None else None
            area = float(a["area_m2"]) if a.get("area_m2") is not None else None
        except (TypeError, ValueError):
            precio = area = None
        motivo = None
        if portal is None:
            motivo = "fuera_de_los_sitios_permitidos"
        elif url in vistos:
            motivo = "repetido_en_la_busqueda"
        elif tipo not in contexto["tipos"]:
            motivo = "tipo_no_solicitado"
        elif not precio or precio <= 0:
            motivo = "sin_precio"
        elif not area or area <= 0:
            motivo = "sin_area"
        elif not (amin <= area <= amax):
            motivo = "area_fuera_de_rango"
        vistos.add(url)
        fecha = _fecha(a.get("fecha_publicacion"))
        if fecha and fecha > contexto["fecha"]:
            fecha = None                                   # una fecha futura no es una fecha de publicación
        filas.append({
            "portal_id": portal["id"] if portal else None, "portal_nombre": portal["nombre"] if portal else None,
            "url": url[:500], "titulo": (str(a.get("titulo") or "").strip()[:200] or None), "tipo_inmueble": tipo or None,
            "precio": precio, "area_m2": area, "habitaciones": _entero(a.get("habitaciones")), "banos": _entero(a.get("banos")),
            "parqueaderos": _entero(a.get("parqueaderos")), "barrio": (str(a.get("barrio") or "").strip()[:120] or None),
            "fecha_publicacion": fecha, "motivo_exclusion": motivo})
    return filas


def _lotes(portales: list[dict], tam: int) -> list[list[dict]]:
    return [portales[i:i + tam] for i in range(0, len(portales), max(1, tam))]


def _leer_lote(p: dict, contexto: dict, lote: list[dict]) -> dict:
    """Una llamada a Claude Code para un grupo de sitios. Nunca lanza."""
    try:
        r = claude_cliente.invocar_con_web(p, SISTEMA, mensaje(contexto, lote, p), ESQUEMA,
                                           [x["dominio"] for x in lote], p["params"]["rpa"])
    except claude_cliente.ClaudeNoDisponible as e:
        return {"error": str(e)[:300], "anuncios": [], "portales": [], "turnos": None, "modelo": None, "denegados": []}
    datos = r.get("datos") if isinstance(r.get("datos"), dict) else {}
    return {"error": None, "anuncios": datos.get("anuncios") or [], "portales": datos.get("portales") or [],
            "turnos": r.get("turnos"), "modelo": r.get("modelo"), "denegados": r.get("denegados") or []}


def buscar(p: dict, contexto: dict, portales: list[dict], avance=None) -> dict:
    """Corre la búsqueda por lotes de sitios, varios a la vez. Nunca lanza.

    Devuelve {estado, mensaje, anuncios, por_portal, duracion_s, ...}. `avance(hechos, total)` se llama al
    terminar cada lote.
    """
    cfg = p["params"]["rpa"]
    base = {"estado": "omitido", "mensaje": "", "anuncios": [], "por_portal": [], "duracion_s": 0.0,
            "consulta": {k: contexto.get(k) for k in ("fecha", "operacion", "municipio", "barrio", "comuna", "tipos",
                                                      "tipo_cotizado", "rango_area")},
            "portales": [x["id"] for x in portales]}
    if not cfg["activo"]:
        return dict(base, mensaje="desactivado en params.yaml › rpa")
    if not portales:
        return dict(base, mensaje="todas las inmobiliarias están apagadas")
    if not contexto.get("tipos"):
        return dict(base, mensaje="la cotización no registra qué tipo de inmueble busca el cliente")
    tope = int(cfg.get("max_sitios_por_busqueda") or 0)
    leer, sin_leer = (portales[:tope], portales[tope:]) if tope else (portales, [])
    lotes = _lotes(leer, int(cfg["sitios_por_lote"]))
    inicio = time.monotonic()
    resultados: list[dict | None] = [None] * len(lotes)
    with ThreadPoolExecutor(max_workers=max(1, int(cfg["lotes_en_paralelo"]))) as hilos:
        futuros = {hilos.submit(_leer_lote, p, contexto, lote): i for i, lote in enumerate(lotes)}
        for hecho, futuro in enumerate(as_completed(futuros), start=1):
            resultados[futuros[futuro]] = futuro.result()
            if avance:
                avance(hecho, len(lotes))
    crudos = [a for r in resultados for a in r["anuncios"]]
    anuncios = validar(crudos, contexto, leer, p)
    informe = {}
    for r in resultados:
        for x in r["portales"]:
            if isinstance(x, dict):
                informe[str(x.get("portal") or "").lower().replace("www.", "")] = x
    por_portal = []
    for i, lote in enumerate(lotes):
        fallo = resultados[i]["error"]
        for x in lote:
            suyos = [a for a in anuncios if a["portal_id"] == x["id"]]
            dicho = informe.get(x["dominio"], {})
            utiles = sum(a["motivo_exclusion"] is None for a in suyos)
            estado = "error" if fallo else (dicho.get("estado") or ("ok" if suyos else "sin_resultados"))
            if suyos and estado == "sin_resultados":
                estado = "ok"
            por_portal.append({"portal": x["id"], "nombre": x["nombre"], "dominio": x["dominio"], "estado": estado,
                               "anuncios": len(suyos), "utiles": utiles, "descartados": len(suyos) - utiles,
                               "detalle": (str(fallo or dicho.get("detalle") or "")[:300] or None)})
    por_portal += [{"portal": x["id"], "nombre": x["nombre"], "dominio": x["dominio"], "estado": "sin_consultar", "anuncios": 0,
                    "utiles": 0, "descartados": 0, "detalle": f"tope de {tope} sitios por búsqueda (params.yaml › rpa)"}
                   for x in sin_leer]
    utiles = sum(a["motivo_exclusion"] is None for a in anuncios)
    fuera = sum(a["portal_id"] is None for a in anuncios)
    motivos: dict[str, int] = {}
    for a in anuncios:
        if a["motivo_exclusion"]:
            motivos[a["motivo_exclusion"]] = motivos.get(a["motivo_exclusion"], 0) + 1
    caidos = sum(1 for r in resultados if r["error"])
    por_estado: dict[str, int] = {}
    for x in por_portal:
        por_estado[x["estado"]] = por_estado.get(x["estado"], 0) + 1
    if caidos == len(lotes):
        return dict(base, estado="no_disponible", mensaje=resultados[0]["error"], por_portal=por_portal,
                    duracion_s=round(time.monotonic() - inicio, 1), sitios_por_estado=por_estado)
    con_anuncios = sum(1 for x in por_portal if x["utiles"])
    return dict(base, estado="ok" if utiles else "sin_resultados", anuncios=anuncios, por_portal=por_portal,
                utiles=utiles, leidos=len(anuncios), fuera_de_lista=fuera, descartados_por_motivo=motivos,
                sitios_por_estado=por_estado, lotes=len(lotes), lotes_con_error=caidos,
                mensaje=f"{utiles} anuncios útiles de {len(anuncios)} leídos en {len(leer)} sitios ({con_anuncios} con anuncios)",
                duracion_s=round(time.monotonic() - inicio, 1),
                modelo=next((r["modelo"] for r in resultados if r["modelo"]), None),
                turnos=sum(r["turnos"] or 0 for r in resultados), denegados=[d for r in resultados for d in r["denegados"]])


def guardar(p: dict, captura_id: str, cotizacion_id: str, resultado: dict, operacion: str) -> int:
    """Deja los anuncios en `ofertas` (precio pedido) y una fila por sitio en `ejecuciones`. Nada se descarta en silencio."""
    t = bd.ahora()
    nuevos = 0
    with bd.conectar(p["rutas"]["bd"]) as con:
        for a in resultado["anuncios"]:
            if not a["portal_id"]:
                continue                                    # fuera de los sitios permitidos: queda contado en ejecuciones
            precio, area = a["precio"], a["area_m2"]
            banderas = ["rpa_claude_code"] + ([] if a["fecha_publicacion"] else ["sin_fecha_de_publicacion"])
            fila = {
                "captura_id": captura_id, "fuente": FUENTE, "capa": a["portal_id"],
                "id_externo": hashlib.sha256(a["url"].encode("utf-8")).hexdigest()[:20],
                "operacion": operacion, "tipo_inmueble": a["tipo_inmueble"], "area_privada": area, "area_usada": area,
                "area_tipo_usada": "publicada", "precio": precio, "tipo_precio": "pedido",
                "habitaciones": a["habitaciones"], "banos": a["banos"], "parqueaderos": a["parqueaderos"],
                "precio_m2": round(precio / area) if precio and area else None, "barrio_publicado": a["barrio"],
                "anio": int(t[:4]), "fecha_publicacion": a["fecha_publicacion"], "url": a["url"],
                "origen_publicacion": a["portal_nombre"], "banderas_json": bd.a_json(banderas),
                "motivo_exclusion": a["motivo_exclusion"],
                "atributos_json": bd.a_json({"titulo": a["titulo"], "cotizacion_id": cotizacion_id}),
                "licencia": "Anuncio público del sitio de origen. Precio pedido. Se guarda el dato y su enlace, no el contenido.",
                "obtenido_en": t}
            previa = con.execute("SELECT oferta_id FROM ofertas WHERE captura_id=? AND fuente=? AND capa=? AND id_externo=?",
                                 (captura_id, FUENTE, fila["capa"], fila["id_externo"])).fetchone()
            if previa:                                      # el mismo anuncio visto otra vez: se actualiza lo que se vio hoy
                cambios = {k: fila[k] for k in ("precio", "precio_m2", "area_privada", "area_usada", "fecha_publicacion",
                                                "motivo_exclusion", "banderas_json", "obtenido_en")}
                con.execute(f"UPDATE ofertas SET {', '.join(f'{k}=?' for k in cambios)} WHERE oferta_id=?",
                            (*cambios.values(), previa[0]))
            else:
                bd.insertar(con, "ofertas", fila)
                nuevos += 1
        for x in resultado["por_portal"]:
            bd.insertar(con, "ejecuciones", {
                "captura_id": captura_id, "fuente": FUENTE, "capa": x["portal"], "inicio": t, "fin": t,
                "estado": {"ok": "ok", "sin_resultados": "ok", "bloqueado": "bloqueado", "error": "error",
                           "sin_consultar": "omitida"}.get(x["estado"], "parcial"),
                "registros": x["utiles"], "descartados": x["descartados"], "llamadas": 1,
                "mensaje": f"RPA con Claude Code · {x['estado']}" + (f" · {x['detalle']}" if x["detalle"] else "")})
    return nuevos
