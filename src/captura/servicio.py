"""Orquestación de una captura: ubicación → predio → barrido → reglas → guardado provisional → Claude → cierre.

Cada paso informa su avance. Una fuente caída no tumba la captura: queda con su estado y un aviso.

El guardado es en dos tiempos. Después de las reglas la captura queda en la base como "en curso": si la
curaduría con Claude falla, el barrido no se pierde. Claude corrige esas mismas filas y el último paso
recalcula los indicadores y cierra la captura. Solo una captura cerrada alimenta el almacén.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import __version__, bd, geo
from .curaduria import claude_tareas, indicadores, modelo
from .curaduria.normalizacion import limpiar_nombre
from .fuentes import base, catastro, geomedellin, google, osm

PASOS = [("ubicacion", "Ubicación"), ("predio", "Predio y norma"), ("fuentes", "Barrido de 1 km"),
         ("reglas", "Depuración por reglas"), ("guardado", "Guardado provisional"),
         ("claude", "Curaduría con Claude"), ("indicadores", "Indicadores y cierre")]
GRUPO_ENTORNO, GRUPO_MERCADO = "entorno", "mercado"       # los dos bloques del avance: predio (se reusa) y cotización (del día)
FUENTES_MEDELLIN = {"geomedellin", "catastro", "registro_ic", "oime"}
FALLAS = ("error", "bloqueado", "fallo_estructura")
NOMBRES_SESION: dict[str, dict[str, str]] = {}      # nombres de Google: solo en memoria, nunca en la base


class Progreso:
    """Estado visible de una captura en curso (lo consulta el navegador)."""

    def __init__(self) -> None:
        self.pasos = {k: {"id": k, "etiqueta": e, "estado": "pendiente", "detalle": "", "grupo": GRUPO_ENTORNO}
                      for k, e in PASOS}
        self.grupos: list[dict] = []              # bloques del avance, con su nota (los arma quien lanza el trabajo)
        self.al_ubicar_predio = None              # aviso opcional: ya se sabe el barrio (adelanta la búsqueda de anuncios)
        self.capas: list[dict] = []
        self.captura_id: str | None = None
        self.terminado = False
        self.reusada = False
        self.error: str | None = None
        self.usuario: str | None = None           # quién lanzó la captura: solo él puede seguirla
        self.ficha: dict | None = None            # registro de la ficha generada al terminar

    def agregar_paso(self, paso: str, etiqueta: str, grupo: str = GRUPO_MERCADO) -> None:
        """Pasos del mercado de la cotización: anuncios, su depuración, carga al almacén y ficha."""
        self.pasos[paso] = {"id": paso, "etiqueta": etiqueta, "estado": "pendiente", "detalle": "", "grupo": grupo}

    def marcar(self, paso: str, estado: str, detalle: str = "") -> None:
        self.pasos[paso].update(estado=estado, detalle=detalle)

    def a_dict(self) -> dict:
        return {"captura_id": self.captura_id, "terminado": self.terminado, "reusada": self.reusada,
                "error": self.error, "pasos": list(self.pasos.values()), "grupos": list(self.grupos),
                "capas": list(self.capas), "ficha": self.ficha}


# --------------------------------------------------------------------------------------------
# Ubicación
# --------------------------------------------------------------------------------------------
async def geocodificar(cli, p: dict, direccion: str) -> dict:
    """Cadena de resolución: nomenclatura oficial (P1/P2) → Google (si hay llave) → Nominatim (P3)."""
    intentos = []
    try:
        r = await catastro.geocodificar(cli, p, direccion)
    except Exception as e:
        r = {"ok": False, "motivo": f"catastro: {type(e).__name__}"}
    if r.get("ok"):
        return r
    intentos.append(r.get("motivo"))
    if google.habilitado(p):
        r = await google.geocodificar(cli, p, direccion)
        if r.get("ok"):
            return r
        intentos.append(r.get("motivo"))
    r = await osm.geocodificar_nominatim(cli, p, direccion)
    if r.get("ok"):
        r.setdefault("avisos", []).append("Ubicación aproximada (Nominatim): confirma el punto en el mapa.")
        return r
    intentos.append(r.get("motivo"))
    return {"ok": False, "motivo": "; ".join(str(i) for i in intentos if i)}


async def resolver_ubicacion(cli, p: dict, entrada: dict) -> dict:
    prec = p["reglas"]["ubicacion"]["precision"]
    direccion = (entrada.get("direccion") or "").strip()
    avisos: list[str] = []
    if entrada.get("lat") is None or entrada.get("lng") is None:
        r = await geocodificar(cli, p, direccion)
        if not r.get("ok"):
            raise ValueError(f"No se pudo ubicar la dirección ({r.get('motivo')}). Marca el punto en el mapa.")
        punto = {"lat": r["lat"], "lng": r["lng"], "origen": r["origen"], "precision": r["precision"],
                 "direccion_normalizada": r.get("direccion_normalizada") or r.get("direccion_canonica"),
                 "direccion_catastral": r.get("direccion_catastral"), "place_id": r.get("place_id")}
        avisos += r.get("avisos", [])
    else:
        origen = entrada.get("origen_coordenada") or "pin_usuario"
        punto = {"lat": float(entrada["lat"]), "lng": float(entrada["lng"]), "origen": origen,
                 "precision": entrada.get("precision") or prec["pin_usuario"],
                 "direccion_normalizada": entrada.get("direccion_normalizada"),
                 "direccion_catastral": entrada.get("direccion_catastral"), "place_id": entrada.get("place_id")}
        if origen.startswith("google"):
            # Si la nomenclatura oficial resuelve la misma dirección, se usa ese punto: es permanente,
            # trae CBML y no queda sujeto a la regla de 30 días de Google.
            oficial = {"ok": False}
            if direccion:
                try:
                    oficial = await catastro.geocodificar(cli, p, direccion)
                except Exception:
                    oficial = {"ok": False}
            if oficial.get("ok"):
                punto.update(lat=oficial["lat"], lng=oficial["lng"], origen="catastro",
                             precision=oficial["precision"], direccion_catastral=oficial["direccion_catastral"])
                avisos.append(f"Punto tomado de la nomenclatura oficial ({oficial['metodo']}).")
            else:
                punto["precision"] = prec["externo"]
        elif origen == "pin_usuario":
            punto["precision"] = prec["pin_usuario"]
    punto["coord_expira_en"] = google.expira(p) if punto["origen"].startswith("google") else None
    punto["avisos"] = avisos
    return punto


def elegir_conjunto(candidatos: list[dict], p: dict) -> dict | None:
    """Regla determinista: mismo lote > polígono que contiene el punto > el más cercano."""
    orden_origen = {f: k for k, f in enumerate(p["reglas"]["deduplicacion"]["prioridad_fuente"])}
    validos = [c for c in candidatos if c.get("nombre")]
    if not validos:
        return None
    return min(validos, key=lambda c: (not c.get("mismo_lote"), not c.get("contiene_punto"),
                                       c["distancia_m"] if c.get("distancia_m") is not None else 1e9,
                                       orden_origen.get(c["origen"], 9)))


def buscar_reciente(p: dict, lat: float, lng: float, radio: int) -> str | None:
    """Captura terminada del mismo punto dentro del plazo de reuso."""
    reglas = p["reglas"]["captura"]
    desde = (datetime.now(timezone.utc) - timedelta(days=reglas["reusar_captura_dias"])).isoformat(timespec="seconds")
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute(
            "SELECT captura_id, lat, lng FROM capturas WHERE lat IS NOT NULL AND radio_m=? AND creada_en>=? "
            "AND estado IN ('ok','parcial') ORDER BY creada_en DESC", (radio, desde)).fetchall()
    for f in filas:
        if geo.haversine_m(lat, lng, f["lat"], f["lng"]) <= reglas["tolerancia_mismo_punto_m"]:
            return f["captura_id"]
    return None


# --------------------------------------------------------------------------------------------
# Escritura
# --------------------------------------------------------------------------------------------
def _fila_item(captura_id: str, it: dict, t: str) -> dict:
    es_google = it["fuente"] == "google"
    return {
        "captura_id": captura_id, "fuente": it["fuente"], "capa": it["capa"], "id_externo": str(it["id_externo"]),
        "categoria": it["categoria"], "subtipo": it.get("subtipo"),
        "categoria_original": None if es_google else it.get("categoria_original"),
        "confianza_clasificacion": it.get("confianza_clasificacion"),
        "nombre_original": None if es_google else it.get("nombre_original"),
        "nombre": None if es_google else it.get("nombre"),
        "nombre_clave": None if es_google else (it.get("nombre_clave") or None),
        "lat": it.get("lat"), "lng": it.get("lng"), "x_9377": it.get("x_9377"), "y_9377": it.get("y_9377"),
        "distancia_m": it.get("distancia_m"), "anillo_m": it.get("anillo_m"), "geometria": it.get("geometria"),
        "atributos_json": bd.a_json(it.get("atributos") or {}), "banderas_json": bd.a_json(it.get("banderas") or []),
        "completitud": it.get("completitud"), "entidad_id": it.get("entidad_id"), "es_maestro": it.get("es_maestro", 1),
        "regla_dup": it.get("regla_dup"), "puntaje_dup": it.get("puntaje_dup"), "coincide_con": it.get("coincide_con"),
        "motivo_exclusion": it.get("motivo_exclusion"), "curado_por": it.get("curado_por", "regla"),
        "en_mapa": int(bool(it.get("en_mapa", True))), "licencia": it["licencia"], "expira_en": it.get("expira_en"),
        "obtenido_en": t,
    }


def _guardar_bitacora(con, captura_id: str, entradas: list[dict], t: str) -> None:
    for e in entradas:
        bd.insertar(con, "curaduria_log", {
            "captura_id": captura_id, "item_ref": e.get("item_ref"), "etapa": e.get("etapa"), "campo": e.get("campo"),
            "valor_antes": e.get("valor_antes"), "valor_despues": e.get("valor_despues"), "regla": e.get("regla"),
            "origen": e.get("origen", "regla"), "aceptado": e.get("aceptado", 1), "modelo": e.get("modelo"),
            "creado_en": t})


def _guardar_derivados(con, captura_id: str, registros: list[dict], ofertas: list[dict], predio: dict | None,
                       p: dict, radio: int, saturados: dict, lat: float, lng: float) -> None:
    """Resumen por categoría e indicadores (se recalculan después de la curaduría con Claude)."""
    expira = google.expira(p) if any(it["fuente"] == "google" for it in registros) else None
    tabla = modelo.a_tabla(registros)
    con.execute("DELETE FROM resumen_categoria WHERE captura_id=?", (captura_id,))
    con.execute("DELETE FROM indicadores WHERE captura_id=?", (captura_id,))
    for f in indicadores.resumen_categoria(tabla, saturados, expira):
        bd.insertar(con, "resumen_categoria", dict(f, captura_id=captura_id))
    control = modelo.control_distancia(registros, lat, lng, p["params"]["control"]["muestra_distancia_9377"])
    for f in indicadores.calcular(tabla, ofertas, predio, p, radio, control, expira):
        bd.insertar(con, "indicadores", {
            "captura_id": captura_id, "indicador": f["indicador"], "base": f["base"], "valor": f["valor"],
            "unidad": f["unidad"], "detalle_json": bd.a_json(f["detalle"]), "expira_en": f["expira_en"]},
            modo="INSERT OR REPLACE")


def municipio_de(cubierto) -> str | None:
    """Municipio de la captura: solo se afirma cuando el punto cayó dentro de la cobertura oficial."""
    return "Medellín" if cubierto else None


def _actualizar_captura(con, captura_id: str, conjunto: dict | None, p: dict, cubierto, avisos: list[str],
                        llamadas_google: int, ia: dict | None, duracion: float, estado: str,
                        direccion_catastral: str | None = None) -> None:
    conjunto = conjunto or {}
    expira = google.expira(p) if conjunto.get("origen") == "google" else None
    con_ia = bool(ia and ia.get("lotes") and ia.get("estado") in ("ok", "parcial"))
    con.execute(
        "UPDATE capturas SET conjunto_nombre=?, conjunto_tipo=?, conjunto_origen=?, conjunto_expira_en=?, "
        "en_cobertura=?, municipio=?, avisos_json=?, llamadas_google=?, nota_curaduria=?, curaduria_proveedor=?, "
        "curaduria_modelo=?, duracion_s=?, estado=?, direccion_catastral=COALESCE(?, direccion_catastral) "
        "WHERE captura_id=?",
        (conjunto.get("nombre"), conjunto.get("tipo"), conjunto.get("origen"), expira,
         None if cubierto is None else int(cubierto), municipio_de(cubierto),
         bd.a_json(avisos), llamadas_google,
         ("\n".join(ia.get("observaciones") or []) or None) if con_ia else None,
         ia.get("proveedor") if con_ia else None, ia.get("modelo") if con_ia else None,
         round(duracion, 1), estado, direccion_catastral, captura_id))


def _aplicar_ia(con, captura_id: str, registros: list[dict], ia: dict, t: str) -> None:
    """Lleva a la base lo que Claude cambió y los enlaces de duplicados que resolvió."""
    hubo_fusion = any(r["decision"] == "mismo_lugar" for r in ia["resueltos"])
    for it in registros:
        tocado = it.get("curado_por") == "claude"
        if not tocado and not (hubo_fusion and it.get("en_mapa", True)):
            continue
        es_google = it["fuente"] == "google"
        con.execute(
            "UPDATE entorno_items SET categoria=?, subtipo=?, confianza_clasificacion=?, nombre=?, nombre_clave=?, "
            "entidad_id=?, es_maestro=?, regla_dup=?, puntaje_dup=?, coincide_con=?, curado_por=?, banderas_json=? "
            "WHERE captura_id=? AND fuente=? AND capa=? AND id_externo=?",
            (it["categoria"], it.get("subtipo"), it.get("confianza_clasificacion"),
             None if es_google else it.get("nombre"), None if es_google else (it.get("nombre_clave") or None),
             it.get("entidad_id"), it.get("es_maestro", 1), it.get("regla_dup"), it.get("puntaje_dup"),
             it.get("coincide_con"), it.get("curado_por", "regla"), bd.a_json(it.get("banderas") or []),
             captura_id, it["fuente"], it["capa"], str(it["id_externo"])))
    for res in ia["resueltos"]:
        con.execute("UPDATE cola_revision SET resuelto_por='claude', decision=? WHERE captura_id=? AND par=?",
                    (res["decision"], captura_id, res["par"]))
    _guardar_bitacora(con, captura_id, ia["bitacora"], t)


# --------------------------------------------------------------------------------------------
# Captura
# --------------------------------------------------------------------------------------------
async def capturar(entrada: dict, p: dict, prog: Progreso) -> str:
    t0 = time.time()
    reglas = p["reglas"]
    radio = int(reglas["captura"]["radio_m"])
    catalogo = p["fuentes"]["fuentes"]
    pedidas = set(entrada.get("fuentes") or [f for f, c in catalogo.items() if c.get("activa")])
    avisos: list[str] = []
    ejecuciones: list[base.Ejecucion] = []

    async with base.nuevo_cliente(p) as cli:
        # 1. Ubicación
        prog.marcar("ubicacion", "en_curso")
        punto = await resolver_ubicacion(cli, p, entrada)
        lat, lng = punto["lat"], punto["lng"]
        avisos += punto["avisos"]
        prog.marcar("ubicacion", "ok", f"{punto['precision']} · {punto['origen']} · {lat:.6f}, {lng:.6f}")

        if entrada.get("reusar", True):
            previa = buscar_reciente(p, lat, lng, radio)
            if previa:
                prog.captura_id, prog.reusada = previa, True
                for k, _ in PASOS[1:]:
                    prog.marcar(k, "ok", "reusado de la base")
                return previa

        captura_id = str(uuid.uuid4())
        prog.captura_id = captura_id
        x9, y9 = geo.a_9377(lng, lat)
        with bd.conectar(p["rutas"]["bd"]) as con:
            bd.insertar(con, "capturas", {
                "captura_id": captura_id, "creada_en": bd.ahora(),
                "direccion_ingresada": (entrada.get("direccion") or "").strip() or "Punto marcado en el mapa",
                "direccion_normalizada": punto.get("direccion_normalizada"),
                "direccion_catastral": punto.get("direccion_catastral"),
                "lat": lat, "lng": lng, "x_9377": x9, "y_9377": y9,
                "origen_coordenada": punto["origen"], "precision_geo": punto["precision"],
                "google_place_id": punto.get("place_id"), "coord_expira_en": punto["coord_expira_en"],
                "radio_m": radio, "estado": "en_curso", "huella_config": p["huella"], "version_app": __version__})

        # 2. Predio y norma (capa oficial del punto)
        prog.marcar("predio", "en_curso")
        try:
            ctx = await catastro.contexto_punto(cli, p, lat, lng)
        except Exception as e:
            ctx = {"predio": None, "candidatos_conjunto": [], "ejecuciones": [], "en_cobertura": None}
            ejecuciones.append(base.Ejecucion("catastro", "contexto").cerrar("error", f"{type(e).__name__}: {str(e)[:160]}"))
        ejecuciones += ctx["ejecuciones"]
        predio, cubierto = ctx["predio"], ctx["en_cobertura"]
        if predio and all(e.estado in FALLAS for e in ctx["ejecuciones"]):
            predio, cubierto = None, None            # el catastro no respondió: no se afirma nada del predio
        if cubierto is False:
            avisos.append("El punto está fuera de Medellín: no hay catastro, norma ni capas oficiales de la "
                          "Alcaldía (pendiente la fuente municipal). OpenStreetMap y Google sí aplican.")
        if predio and predio.get("via_cercana") and not punto.get("direccion_catastral"):
            punto["direccion_catastral"] = f"{predio['via_cercana']} # {predio['placa_cercana']}"
        if predio and cubierto:
            prog.marcar("predio", "ok", f"{predio.get('barrio_nombre') or 'sin barrio'} · CBML {predio.get('cbml') or '—'}")
        elif cubierto is False:
            prog.marcar("predio", "omitido", "fuera de Medellín: pendiente la fuente municipal")
        else:
            prog.marcar("predio", "error", "el catastro no respondió")
        if prog.al_ubicar_predio:                    # ya hay barrio y comuna: el mercado de la cotización puede arrancar
            prog.al_ubicar_predio(captura_id, predio, cubierto)

        # 3. Barrido de fuentes en paralelo
        prog.marcar("fuentes", "en_curso")

        def avance_capa(capa: dict, ej: base.Ejecucion) -> None:
            prog.capas.append({"capa": capa["etiqueta"], "estado": ej.estado, "registros": ej.registros})

        activas_med = (pedidas & FUENTES_MEDELLIN) if cubierto is not False else set()
        tareas = {"oficial": geomedellin.barrer(cli, p, lat, lng, radio, activas_med, avance_capa)}
        if "osm" in pedidas and catalogo["osm"].get("activa"):
            tareas["osm"] = osm.barrer(cli, p, lat, lng, radio)
        if "google" in pedidas:
            tareas["google"] = google.barrer(cli, p, lat, lng, radio)
        hechos = dict(zip(tareas, await asyncio.gather(*tareas.values(), return_exceptions=True)))

    items: list[dict] = []
    ofertas: list[dict] = []
    candidatos = list(ctx["candidatos_conjunto"])
    saturados: dict[str, bool] = {}
    llamadas_google = 0
    for nombre, res in hechos.items():
        if isinstance(res, BaseException):
            ejecuciones.append(base.Ejecucion(nombre, "barrido").cerrar("error", f"{type(res).__name__}: {str(res)[:160]}"))
            continue
        if nombre == "oficial":
            items += res[0]
            ofertas += res[1]
            ejecuciones += res[2]
        elif nombre == "osm":
            items += res[0]
            candidatos += res[1]
            ejecuciones.append(res[2])
            prog.capas.append({"capa": "OpenStreetMap", "estado": res[2].estado, "registros": res[2].registros})
        else:
            items += res[0]
            candidatos += res[1]
            saturados = res[2]
            ejecuciones.append(res[3])
            llamadas_google = res[3].llamadas
            prog.capas.append({"capa": "Google Places", "estado": res[3].estado, "registros": res[3].registros})
    if cubierto is False and (pedidas & FUENTES_MEDELLIN):
        ejecuciones.append(base.Ejecucion("geomedellin", "capas_oficiales").cerrar("omitida", "punto fuera de Medellín"))
    for ej in ejecuciones:
        if ej.estado in FALLAS:
            avisos.append(f"Fuente {ej.fuente}/{ej.capa}: {ej.estado} ({ej.mensaje or 'sin detalle'}). "
                          "Sus datos no entraron; eso no significa que no existan.")
        elif ej.estado == "parcial":
            avisos.append(f"Fuente {ej.fuente}/{ej.capa}: parcial ({ej.mensaje}).")
    for cat, sat in saturados.items():
        if sat:
            avisos.append(f"Google llenó su tope en '{cat}' incluso subdividido: ese conteo puede estar incompleto.")
    if ofertas:
        avisos.append("Las ofertas del OIME son históricas (2023) y de precio pedido: sirven de referencia, no de oferta vigente.")
    fallidas = sum(1 for e in ejecuciones if e.estado in FALLAS)
    prog.marcar("fuentes", "ok" if not fallidas else "parcial",
                f"{len(items)} registros · {len(ofertas)} ofertas · {fallidas} capas con falla")

    # 4. Curaduría por reglas
    prog.marcar("reglas", "en_curso")
    NOMBRES_SESION[captura_id] = {str(it["id_externo"]): it["nombre_original"] for it in items
                                  if it["fuente"] == "google" and it.get("nombre_original")}
    curado = modelo.curar(items, p, lat, lng)
    registros, revision = curado["registros"], curado["revision"]
    enlazados = sum(1 for it in registros if not it["es_maestro"])
    prog.marcar("reglas", "ok", f"{enlazados} duplicados enlazados · {len(revision)} pares en revisión")

    # Nombre del conjunto: manda el usuario; después el nombre oficial del mismo lote. Solo si no hay
    # ninguno de los dos queda "abierto" para que Claude elija entre los candidatos cercanos.
    if (entrada.get("conjunto_nombre") or "").strip():
        conjunto, conjunto_firme = {"nombre": entrada["conjunto_nombre"].strip(), "tipo": None, "origen": "usuario"}, True
    else:
        elegido = elegir_conjunto(candidatos, p)
        conjunto = None if not elegido else {"nombre": limpiar_nombre(elegido["nombre"], p["taxonomia"]),
                                             "tipo": elegido.get("tipo"), "origen": elegido["origen"]}
        conjunto_firme = bool(elegido and elegido.get("mismo_lote"))

    # 5. Guardado
    prog.marcar("guardado", "en_curso")
    t = bd.ahora()
    with bd.conectar(p["rutas"]["bd"]) as con:
        if predio:
            fila = {k: v for k, v in predio.items() if k != "atributos"}
            fila.update(captura_id=captura_id, atributos_json=bd.a_json(predio.get("atributos")),
                        licencia=catalogo["catastro"]["licencia"], obtenido_en=t)
            bd.insertar(con, "predios", fila)
        for ej in ejecuciones:
            bd.insertar(con, "ejecuciones", dict(ej.a_dict(), captura_id=captura_id))
        for it in registros:
            bd.insertar(con, "entorno_items", _fila_item(captura_id, it, t), modo="INSERT OR IGNORE")
        for o in ofertas:
            fila = {k: v for k, v in o.items() if k not in ("banderas", "atributos")}
            fila.update(captura_id=captura_id, banderas_json=bd.a_json(o["banderas"]),
                        atributos_json=bd.a_json(o["atributos"]), obtenido_en=t)
            bd.insertar(con, "ofertas", fila, modo="INSERT OR IGNORE")
        _guardar_bitacora(con, captura_id, curado["bitacora"], t)
        for par in revision:
            bd.insertar(con, "cola_revision", {"captura_id": captura_id, "tipo": "duplicado", "par": par["par"],
                                               "detalle_json": bd.a_json(par), "creado_en": t})
        _guardar_derivados(con, captura_id, registros, ofertas, predio, p, radio, saturados, lat, lng)
        _actualizar_captura(con, captura_id, conjunto, p, cubierto, avisos, llamadas_google, None,
                            time.time() - t0, "en_curso", punto.get("direccion_catastral"))
    prog.marcar("guardado", "ok", f"{len(registros)} registros · {len(ofertas)} ofertas · {len(ejecuciones)} ejecuciones")

    # 6. Curaduría con Claude (opcional)
    ia = None
    if entrada.get("curar_con_claude", True):
        prog.marcar("claude", "en_curso", "enviando lo dudoso a Claude…")
        contexto = {"direccion": punto.get("direccion_catastral") or entrada.get("direccion"),
                    "barrio": (predio or {}).get("barrio_nombre"), "comuna": (predio or {}).get("comuna_nombre"),
                    "categorias": {k: v["etiqueta"] for k, v in p["taxonomia"]["categorias"].items()}}
        ia = await asyncio.to_thread(claude_tareas.curar, registros, revision, candidatos, contexto, p)
        if ia["estado"] in ("ok", "parcial") and ia["lotes"]:
            if ia["conjunto"] and not conjunto_firme:
                conjunto = {k: ia["conjunto"].get(k) for k in ("nombre", "tipo", "origen")}
            with bd.conectar(p["rutas"]["bd"]) as con:
                _aplicar_ia(con, captura_id, registros, ia, bd.ahora())
        aceptados = sum(1 for b in ia["bitacora"] if b.get("aceptado"))
        estado_paso = {"ok": "ok", "parcial": "parcial", "error": "error"}.get(ia["estado"], "omitido")
        prog.marcar("claude", estado_paso, ia["detalle"] + (f" · {aceptados} cambios aceptados" if ia["lotes"] else ""))
        if ia["estado"] == "error":
            avisos.append(f"Curaduría con Claude no disponible ({ia['detalle']}): la captura quedó depurada solo por reglas.")
    else:
        prog.marcar("claude", "omitido", "desactivada para esta captura")

    # 7. Indicadores y cierre
    prog.marcar("indicadores", "en_curso")
    estado = "parcial" if fallidas else "ok"
    with bd.conectar(p["rutas"]["bd"]) as con:
        _guardar_derivados(con, captura_id, registros, ofertas, predio, p, radio, saturados, lat, lng)
        _actualizar_captura(con, captura_id, conjunto, p, cubierto, avisos, llamadas_google, ia,
                            time.time() - t0, estado, punto.get("direccion_catastral"))
    prog.marcar("indicadores", "ok", f"captura {estado} en {time.time() - t0:.0f} s")
    return captura_id
