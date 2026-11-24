"""Clientes y cotizaciones: para quién se captura y con qué evidencia.

Una cotización une a un comercial, un cliente y una captura (nueva o reabierta) y guarda la
evidencia de lo que el comercial vio: dirección, lote, lugares por categoría y la huella de todo.
Los datos del cliente (nombre y cédula) se quedan en la base local: no van a Claude ni a las
exportaciones del entorno, y en las listas la cédula se muestra enmascarada.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from . import __version__, bd, consultas


class ClienteInvalido(ValueError):
    """Dato del cliente que no pasa la validación (mensaje apto para mostrar)."""


def huella(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------------------------
def tipo_documento(texto, p: dict) -> str:
    """CC, NIT o CE (reglas.yaml › clientes.tipos_documento). Sin dato: el tipo por defecto."""
    r = p["reglas"]["clientes"]
    tipo = str(texto or r.get("tipo_por_defecto", "CC")).strip().upper()
    if tipo not in (r.get("tipos_documento") or {"CC": {}}):
        raise ClienteInvalido("Tipo de documento no reconocido")
    return tipo


def normalizar_documento(texto, p: dict, tipo: str | None = None) -> str:
    r = p["reglas"]["clientes"]
    doc = re.sub(r"[\s.\-]", "", str(texto or ""))
    regla = (r.get("tipos_documento") or {}).get(tipo or "") or {}
    nombre = "El NIT" if tipo == "NIT" else "El documento" if tipo == "CE" else "La cédula"
    if not doc.isdigit():
        raise ClienteInvalido(f"{nombre} lleva solo números")
    minimo = regla.get("min_digitos", r["documento_min_digitos"])
    maximo = regla.get("max_digitos", r["documento_max_digitos"])
    if not (minimo <= len(doc) <= maximo):
        raise ClienteInvalido(f"{nombre} tiene entre {minimo} y {maximo} dígitos")
    return doc


def normalizar_nombre(texto, p: dict) -> str:
    r = p["reglas"]["clientes"]
    nombre = re.sub(r"\s+", " ", str(texto or "")).strip()
    if not (r["nombre_min"] <= len(nombre) <= r["nombre_max"]):
        raise ClienteInvalido(f"El nombre tiene entre {r['nombre_min']} y {r['nombre_max']} caracteres")
    if sum(ch.isalpha() for ch in nombre) < 2 or re.search(r"[<>{}\[\]\\|@#$%^*=_~`]", nombre):
        raise ClienteInvalido("Escribe el nombre con letras")
    if nombre.isupper() or nombre.islower():             # "JUAN PÉREZ" o "juan pérez" → "Juan Pérez"
        nombre = " ".join(w[:1].upper() + w[1:].lower() for w in nombre.split(" "))
    return nombre


def enmascarar(doc: str | None, p: dict) -> str:
    if not doc:
        return ""
    visibles = p["reglas"]["clientes"]["digitos_visibles"]
    return "*" * max(0, len(doc) - visibles) + doc[-visibles:]


# ---------------------------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------------------------
def _contar(con, p: dict, campo: str, valor) -> int:
    estados = p["reglas"]["cotizaciones"]["estados_que_cuentan"]
    marcas = ",".join("?" for _ in estados)
    return con.execute(f"SELECT COUNT(*) FROM cotizaciones WHERE {campo}=? AND estado IN ({marcas})",
                       (valor, *estados)).fetchone()[0]


def normalizar_celular(texto, p: dict) -> str | None:
    """Celular del cliente: solo dígitos, con el largo y el prefijo de reglas.yaml. Vacío devuelve None."""
    r = p["reglas"]["clientes"]["celular"]
    cel = re.sub(r"[\s().+-]", "", str(texto or ""))
    if cel.startswith(r["indicativo"]) and len(cel) == r["digitos"] + len(r["indicativo"]):
        cel = cel[len(r["indicativo"]):]
    if not cel:
        return None
    if not cel.isdigit() or len(cel) != r["digitos"] or not cel.startswith(tuple(r["prefijos"])):
        raise ClienteInvalido(f"El celular debe tener {r['digitos']} dígitos y empezar por {' o '.join(r['prefijos'])}")
    return cel


def _publico(fila, p: dict, cotizaciones: int) -> dict:
    return {"cliente_id": fila["cliente_id"], "nombre": fila["nombre"], "documento": fila["documento"],
            "tiene_celular": bool(fila["celular"]), "celular_mascara": enmascarar(fila["celular"], p) or None,
            "tipo_documento": fila["tipo_documento"],
            "documento_mascara": enmascarar(fila["documento"], p), "autoriza_datos": bool(fila["autoriza_datos"]),
            "autorizado_en": fila["autorizado_en"], "cotizaciones": cotizaciones}


def _contar_de(con, p: dict, cliente_id: int, comercial: str | None) -> int:
    """Cotizaciones que cuentan de un cliente. Con `comercial`, solo las suyas: no se cruzan entre comerciales."""
    if comercial is None:
        return _contar(con, p, "cliente_id", cliente_id)
    estados = p["reglas"]["cotizaciones"]["estados_que_cuentan"]
    return con.execute(f"SELECT COUNT(*) FROM cotizaciones WHERE cliente_id=? AND comercial=? AND estado IN ({','.join('?' for _ in estados)})",
                       (cliente_id, comercial, *estados)).fetchone()[0]


def buscar(p: dict, documento, comercial: str | None = None) -> dict | None:
    doc = normalizar_documento(documento, p)
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute("SELECT * FROM clientes WHERE documento=?", (doc,)).fetchone()
        return _publico(fila, p, _contar_de(con, p, fila["cliente_id"], comercial)) if fila else None


def listar_clientes(p: dict, comerciales: list[str] | None = None) -> list[dict]:
    """Clientes registrados o cotizados por los comerciales indicados (None = todos), con la cédula enmascarada."""
    estados = p["reglas"]["cotizaciones"]["estados_que_cuentan"]
    limite = p["reglas"]["supervision"]["max_en_lista"]
    marcas = ",".join("?" for _ in estados)
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute(
            "SELECT cl.cliente_id, cl.nombre, cl.tipo_documento, cl.documento, cl.creado_por, cl.creado_en, cl.autoriza_datos, "
            f"(SELECT COUNT(*) FROM cotizaciones q WHERE q.cliente_id=cl.cliente_id AND q.estado IN ({marcas})) AS cotizaciones, "
            "(SELECT MAX(q.creada_en) FROM cotizaciones q WHERE q.cliente_id=cl.cliente_id) AS ultima, "
            "(SELECT GROUP_CONCAT(DISTINCT q.comercial) FROM cotizaciones q WHERE q.cliente_id=cl.cliente_id) AS comerciales "
            "FROM clientes cl ORDER BY cl.creado_en DESC LIMIT ?", (*estados, limite)).fetchall()
    lista = []
    for f in filas:
        atendido_por = sorted(set((f["comerciales"] or "").split(",")) - {""})
        if comerciales is not None and f["creado_por"] not in comerciales and not set(atendido_por) & set(comerciales):
            continue
        lista.append({"cliente_id": f["cliente_id"], "nombre": f["nombre"], "tipo_documento": f["tipo_documento"],
                      "documento_mascara": enmascarar(f["documento"], p), "registrado_por": f["creado_por"],
                      "registrado_en": f["creado_en"], "autoriza_datos": bool(f["autoriza_datos"]),
                      "cotizaciones": f["cotizaciones"], "ultima_cotizacion": f["ultima"], "comerciales": atendido_por})
    return lista


def registrar(p: dict, comercial: str, documento, nombre, autoriza: bool, tipo: str | None = None, celular=None) -> dict:
    """Crea el cliente o actualiza su nombre. La autorización, una vez dada, queda con su fecha y su texto.

    El número identifica al cliente (y a su carpeta de fichas) sin importar el tipo: si ya existe, conserva
    el tipo con el que se registró."""
    r = p["reglas"]["clientes"]
    tipo = tipo_documento(tipo, p)
    doc, nom = normalizar_documento(documento, p, tipo), normalizar_nombre(nombre, p)
    cel = normalizar_celular(celular, p)
    t = bd.ahora()
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute("SELECT * FROM clientes WHERE documento=?", (doc,)).fetchone()
        ya_autorizado = bool(fila and fila["autoriza_datos"])
        if r["exigir_autorizacion"] and not ya_autorizado and not autoriza:
            raise ClienteInvalido("Falta la autorización del cliente para tratar sus datos")
        autorizacion = {}
        if autoriza and not ya_autorizado:
            autorizacion = {"autoriza_datos": 1, "autorizado_en": t, "autorizado_por": comercial,
                            "texto_autorizacion_sha256": huella(r["texto_autorizacion"])}
        if r["celular"]["obligatorio"] and not cel and not (fila and fila["celular"]):
            raise ClienteInvalido("Falta el número de celular del cliente")
        if fila is None:
            bd.insertar(con, "clientes", {"documento": doc, "tipo_documento": tipo, "nombre": nom, "celular": cel,
                                          "creado_por": comercial, "creado_en": t, "actualizado_en": t, **autorizacion})
        else:
            cambios = {"nombre": nom, "actualizado_en": t, **autorizacion, **({"celular": cel} if cel else {})}
            con.execute(f"UPDATE clientes SET {', '.join(f'{k}=?' for k in cambios)} WHERE cliente_id=?",
                        (*cambios.values(), fila["cliente_id"]))
        fila = con.execute("SELECT * FROM clientes WHERE documento=?", (doc,)).fetchone()
        return _publico(fila, p, _contar_de(con, p, fila["cliente_id"], comercial))


def para_capturar(p: dict, cliente_id) -> dict:
    """El cliente existe y, si se exige, autorizó. Lanza ClienteInvalido si no."""
    try:
        cid = int(cliente_id)
    except (TypeError, ValueError):
        raise ClienteInvalido("Registra primero al cliente") from None
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute("SELECT * FROM clientes WHERE cliente_id=?", (cid,)).fetchone()
    if fila is None:
        raise ClienteInvalido("Registra primero al cliente")
    if p["reglas"]["clientes"]["exigir_autorizacion"] and not fila["autoriza_datos"]:
        raise ClienteInvalido("Falta la autorización del cliente para tratar sus datos")
    if p["reglas"]["clientes"]["celular"]["obligatorio"] and not fila["celular"]:
        raise ClienteInvalido("Falta el número de celular del cliente: complétalo en el registro")
    return dict(fila)


# ---------------------------------------------------------------------------------------------
# Cotizaciones
# ---------------------------------------------------------------------------------------------
def validar_inmueble(datos, p: dict, exigir: bool = True) -> dict:
    """Datos del inmueble que declara el comercial: tipo, área y estado (más habitaciones, baños y parqueaderos).

    Devuelve las columnas inm_* de la cotización. Con `exigir`, faltar un obligatorio es un error; sin él
    (cotizaciones anteriores a la aplicación única) se guarda lo que haya.
    """
    r = p["reglas"]["inmueble"]
    datos = datos if isinstance(datos, dict) else {}
    salida = {"inm_tipo": None, "inm_area_m2": None, "inm_estado": None, "inm_habitaciones": None, "inm_banos": None,
              "inm_parqueaderos": None, "inm_extras_json": None}
    tipo = re.sub(r"\s+", "_", str(datos.get("tipo") or "").strip().lower())
    if tipo:
        if tipo not in r["tipos"]:
            raise ClienteInvalido("Tipo de inmueble no reconocido")
        salida["inm_tipo"] = tipo
    aplican = set(r["campos_por_tipo"].get(tipo) or r["campos_por_tipo"][r["tipos"][0]]) if tipo else None

    def _aplica(campo: str, etiqueta: str) -> None:
        if aplican is not None and campo not in aplican:
            raise ClienteInvalido(f"{etiqueta} no aplica para {tipo.replace('_', ' ')}")

    estado = str(datos.get("estado") or "").strip().lower()
    if estado:
        _aplica("estado", "El estado")
        if estado not in r["etiquetas_estado"]:
            raise ClienteInvalido("Estado del inmueble no reconocido")
        salida["inm_estado"] = estado
    if datos.get("area_m2") not in (None, ""):
        try:
            area = float(str(datos["area_m2"]).replace(",", "."))
        except ValueError:
            raise ClienteInvalido("El área del inmueble debe ser un número") from None
        minimo, maximo = r["area_m2"]
        if not (minimo <= area <= maximo):
            raise ClienteInvalido(f"El área del inmueble va entre {minimo} y {maximo} m²")
        salida["inm_area_m2"] = round(area, 2)
    for campo, tope in (("habitaciones", r["max_habitaciones"]), ("banos", r["max_banos"]), ("parqueaderos", r["max_parqueaderos"])):
        if datos.get(campo) not in (None, ""):
            _aplica(campo, campo.capitalize())
            try:
                n = int(datos[campo])
            except (TypeError, ValueError):
                raise ClienteInvalido(f"{campo.capitalize()} debe ser un número entero") from None
            if not (0 <= n <= tope):
                raise ClienteInvalido(f"{campo.capitalize()} fuera de rango")
            salida[f"inm_{campo}"] = n
    extras = []
    for x in datos.get("extras") or []:
        nombre = re.sub(r"\s+", " ", str((x or {}).get("nombre") or "")).strip() if isinstance(x, dict) else ""
        valor = re.sub(r"\s+", " ", str((x or {}).get("valor") or "")).strip() if isinstance(x, dict) else ""
        if not nombre and not valor:
            continue
        if not nombre or not valor:
            raise ClienteInvalido("Cada característica adicional lleva nombre y valor")
        if len(nombre) > r["extra_nombre_max"] or len(valor) > r["extra_valor_max"]:
            raise ClienteInvalido("Una característica adicional es demasiado larga")
        extras.append({"nombre": nombre, "valor": valor})
    if len(extras) > r["max_extras"]:
        raise ClienteInvalido(f"Máximo {r['max_extras']} características adicionales")
    salida["inm_extras_json"] = bd.a_json(extras) if extras else None
    if exigir:
        nombres = {"tipo": "el tipo", "area_m2": "el área", "estado": "el estado"}
        faltan = [nombres.get(k, k) for k in r["obligatorios"] if salida.get(f"inm_{k}") is None]
        if faltan:
            raise ClienteInvalido("Del inmueble falta " + ", ".join(faltan))
    return salida


def validar_busqueda(datos, p: dict, tipo_cotizado: str | None = None, exigir: bool = True) -> list[dict]:
    """Lo que busca el cliente: uno o varios tipos de inmueble, sin repetir y en el orden en que se eligieron.

    El tipo del inmueble que se cotiza siempre hace parte de la búsqueda: si no venía, se agrega con origen
    'tipo_cotizado'. Devuelve [{"tipo", "origen"}].
    """
    validos = p["reglas"]["inmueble"]["tipos"]
    crudo = list(datos) if isinstance(datos, (list, tuple)) else ([] if datos in (None, "") else [datos])
    tipos: list[dict] = []
    for x in crudo:
        tipo = re.sub(r"\s+", "_", str(x or "").strip().lower())
        if not tipo:
            continue
        if tipo not in validos:
            raise ClienteInvalido("Tipo de inmueble buscado no reconocido")
        if all(t["tipo"] != tipo for t in tipos):
            tipos.append({"tipo": tipo, "origen": "declarado"})
    if tipo_cotizado and all(t["tipo"] != tipo_cotizado for t in tipos):
        tipos.insert(0, {"tipo": tipo_cotizado, "origen": "tipo_cotizado"})
    if exigir and len(tipos) < p["reglas"]["busqueda"]["min_tipos"]:
        raise ClienteInvalido("Indica qué tipo de inmueble busca el cliente")
    return tipos


def _guardar_busqueda(con, cotizacion_id: str, busca: list[dict] | None) -> None:
    """Agrega los tipos que falten. Nunca borra: lo que el cliente pidió al cotizar queda como se registró."""
    base = con.execute("SELECT COALESCE(MAX(orden), -1) + 1 FROM cotizacion_intereses WHERE cotizacion_id=?",
                       (cotizacion_id,)).fetchone()[0]
    for i, t in enumerate(busca or []):
        con.execute("INSERT OR IGNORE INTO cotizacion_intereses (cotizacion_id, tipo_inmueble, orden, origen, registrado_en) "
                    "VALUES (?,?,?,?,?)", (cotizacion_id, t["tipo"], base + i, t["origen"], bd.ahora()))


def busqueda_de(p: dict, cotizacion_id: str) -> list[str]:
    with bd.conectar(p["rutas"]["bd"]) as con:
        return [r[0] for r in con.execute("SELECT tipo_inmueble FROM cotizacion_intereses WHERE cotizacion_id=? "
                                          "ORDER BY orden, tipo_inmueble", (cotizacion_id,))]


def abrir_cotizacion(p: dict, comercial: str, cliente: dict, direccion: str | None, inmueble: dict | None = None,
                     busca: list[dict] | None = None) -> str:
    cid = str(uuid.uuid4())
    with bd.conectar(p["rutas"]["bd"]) as con:
        bd.insertar(con, "cotizaciones", {
            "cotizacion_id": cid, "comercial": comercial, "cliente_id": cliente["cliente_id"],
            "cliente_nombre": cliente["nombre"], "cliente_documento": cliente["documento"],
            "cliente_tipo_documento": cliente.get("tipo_documento"),
            "direccion_ingresada": (direccion or None), "estado": "en_curso",
            "huella_config": p["huella"], "version_app": __version__, "creada_en": bd.ahora(), **(inmueble or {})})
        _guardar_busqueda(con, cid, busca)
    return cid


def guardar_rpa(p: dict, cotizacion_id: str, portales: list[str] | None = None, resumen: dict | None = None) -> None:
    """Inmobiliarias encendidas en la cotización y resultado de la búsqueda (sin los anuncios: esos van a `ofertas`)."""
    cambios = {}
    if portales is not None:
        cambios["portales_json"] = bd.a_json(portales)
    if resumen is not None:
        cambios["rpa_json"] = bd.a_json({k: v for k, v in resumen.items() if k != "anuncios"})
    if cambios:
        with bd.conectar(p["rutas"]["bd"]) as con:
            con.execute(f"UPDATE cotizaciones SET {', '.join(f'{k}=?' for k in cambios)} WHERE cotizacion_id=?",
                        (*cambios.values(), cotizacion_id))


def guardar_depuracion(p: dict, cotizacion_id: str, resumen: dict) -> None:
    """Resultado del paso de depuración de anuncios, junto al de la búsqueda (los enlaces van en `ofertas`)."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute("SELECT rpa_json FROM cotizaciones WHERE cotizacion_id=?", (cotizacion_id,)).fetchone()
        rpa = (bd.de_json(fila["rpa_json"], {}) if fila else {}) or {}
        rpa["depuracion"] = resumen
        con.execute("UPDATE cotizaciones SET rpa_json=? WHERE cotizacion_id=?", (bd.a_json(rpa), cotizacion_id))


def actualizar_inmueble(p: dict, cotizacion_id: str, inmueble: dict, busca: list[dict] | None = None) -> None:
    """Completa o corrige los datos del inmueble de una cotización y agrega lo que busca el cliente (la ficha se regenera aparte)."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        con.execute(f"UPDATE cotizaciones SET {', '.join(f'{k}=?' for k in inmueble)} WHERE cotizacion_id=?",
                    (*inmueble.values(), cotizacion_id))
        _guardar_busqueda(con, cotizacion_id, busca)


def cotizacion(p: dict, cotizacion_id: str) -> dict | None:
    """Una cotización completa (uso interno: quien llama decide si el usuario puede verla)."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute(
            "SELECT q.*, co.nombre AS comercial_nombre, ca.direccion_catastral, ca.municipio AS municipio_captura, "
            "pr.barrio_nombre, pr.comuna_nombre "
            "FROM cotizaciones q JOIN comerciales co ON co.usuario = q.comercial "
            "LEFT JOIN capturas ca ON ca.captura_id = q.captura_id LEFT JOIN predios pr ON pr.captura_id = q.captura_id "
            "WHERE q.cotizacion_id=?", (cotizacion_id,)).fetchone()
    return dict(fila) if fila else None


def capturas_de(p: dict, comercial: str) -> set[str]:
    """Capturas ligadas a cotizaciones del comercial: las únicas que puede abrir."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        return {r[0] for r in con.execute("SELECT DISTINCT captura_id FROM cotizaciones WHERE comercial=? AND captura_id IS NOT NULL",
                                          (comercial,))}


def evidencia(p: dict, captura_id: str) -> dict | None:
    """Resumen congelado de lo que el comercial vio en pantalla para esa captura."""
    r = consultas.leer_captura(p, captura_id)
    if r is None:
        return None
    pr = r.get("predio") or {}
    return {
        "captura_id": captura_id, "captura_creada_en": r["creada_en"], "estado_captura": r["estado"],
        "direccion_catastral": r["punto"].get("direccion_catastral"), "precision": r["punto"].get("precision"),
        "cbml": pr.get("cbml"), "barrio": pr.get("barrio_nombre"), "comuna": pr.get("comuna_nombre"),
        "pot_tratamiento": pr.get("pot_tratamiento"), "conjunto": r["conjunto"].get("nombre"),
        "lugares": {c["id"]: c["lugares"] for c in r["categorias"]},
        "registros": r["curaduria"].get("registros"), "ofertas": r["ofertas"]["total"],
        "fuentes_con_novedad": sorted(f"{e['fuente']}:{e['capa']}" for e in r["ejecuciones"]
                                      if e["estado"] not in ("ok", "omitida")),
        "huella_config": r.get("huella_config"),
    }


def cerrar_cotizacion(p: dict, cotizacion_id: str, captura_id: str | None, reusada: bool, error: str | None) -> None:
    """Liga la captura, fija el estado y congela la evidencia. Nunca lanza: la captura ya está guardada."""
    ev, estado = None, "error"
    if captura_id:
        try:
            ev = evidencia(p, captura_id)
        except Exception as e:  # noqa: BLE001
            error = error or f"no se pudo armar la evidencia: {type(e).__name__}"
        if ev:
            estado = ev["estado_captura"] if ev["estado_captura"] in ("ok", "parcial") else "error"
    texto = json.dumps(ev, ensure_ascii=False, sort_keys=True, default=str) if ev else None
    with bd.conectar(p["rutas"]["bd"]) as con:
        con.execute("UPDATE cotizaciones SET captura_id=?, reusada=?, estado=?, mensaje=?, evidencia_json=?, "
                    "evidencia_sha256=?, terminada_en=? WHERE cotizacion_id=?",
                    (captura_id, int(bool(reusada)), estado, (error or None) and str(error)[:300], texto,
                     huella(texto) if texto else None, bd.ahora(), cotizacion_id))


def _estado_cliente(f, p: dict) -> dict | None:
    """Estado vigente de la ficha con el cliente (None si la cotización todavía no tiene ficha)."""
    if not f["ficha_id"]:
        return None
    cfg = p["reglas"]["gestion_cliente"]
    estado = f["gc_estado"] or cfg["estado_inicial"]
    return {"estado": estado, "etiqueta": (cfg["estados"].get(estado) or {}).get("etiqueta", estado),
            "desde": f["gc_desde"] or f["ficha_generada_en"], "sin_registrar": f["gc_estado"] is None,
            "motivo_etiqueta": cfg["motivos_rechazo"].get(f["gc_motivo"]) if f["gc_motivo"] else None}


def _fila_lista(f, p: dict) -> dict:
    ev = bd.de_json(f["evidencia_json"], {}) or {}
    res = bd.de_json(f["ficha_resumen"], {}) or {}
    return {
        "cotizacion_id": f["cotizacion_id"], "creada_en": f["creada_en"], "estado": f["estado"],
        "reusada": bool(f["reusada"]), "mensaje": f["mensaje"], "captura_id": f["captura_id"],
        "comercial": f["comercial"], "comercial_nombre": f["comercial_nombre"],
        "cliente_id": f["cliente_id"], "cliente_nombre": f["cliente_nombre"],
        "cliente_tipo_documento": f["cliente_tipo_documento"] or p["reglas"]["clientes"].get("tipo_por_defecto", "CC"),
        "cliente_documento_mascara": enmascarar(f["cliente_documento"], p),
        "direccion": f["direccion_catastral"] or f["direccion_ingresada"],
        "barrio": f["barrio_nombre"], "lugares": sum((ev.get("lugares") or {}).values()) if ev else None,
        "inmueble": {"tipo": f["inm_tipo"], "area_m2": f["inm_area_m2"], "estado": f["inm_estado"],
                     "habitaciones": f["inm_habitaciones"], "banos": f["inm_banos"], "parqueaderos": f["inm_parqueaderos"],
                     "extras": bd.de_json(f["inm_extras_json"], []) or []},
        "busca": f["busca"].split(",") if f["busca"] else [],
        "portales": bd.de_json(f["portales_json"], None),
        "rpa": bd.de_json(f["rpa_json"], None),
        "estado_cliente": _estado_cliente(f, p),
        "ficha": None if not f["ficha_id"] else {
            "ficha_id": f["ficha_id"], "version": f["ficha_version"], "estado": f["ficha_estado"],
            "generada_en": f["ficha_generada_en"], "confianza": res.get("confianza"), "n": res.get("n"),
            "mediana": res.get("mediana"), "base_mercado": res.get("base_mercado")}}


_SQL_LISTA = (
    "SELECT q.*, co.nombre AS comercial_nombre, ca.direccion_catastral, pr.barrio_nombre, "
    "fi.ficha_id, fi.version AS ficha_version, fi.estado AS ficha_estado, fi.generada_en AS ficha_generada_en, "
    "fi.resumen_json AS ficha_resumen, "
    "(SELECT GROUP_CONCAT(tipo_inmueble) FROM (SELECT i.tipo_inmueble FROM cotizacion_intereses i "
    "WHERE i.cotizacion_id = q.cotizacion_id ORDER BY i.orden, i.tipo_inmueble)) AS busca, "
    # estado de la ficha con el cliente: vale el último que registró el comercial
    "(SELECT g.estado FROM gestion_cliente g WHERE g.cotizacion_id = q.cotizacion_id ORDER BY g.gestion_id DESC LIMIT 1) AS gc_estado, "
    "(SELECT g.registrado_en FROM gestion_cliente g WHERE g.cotizacion_id = q.cotizacion_id ORDER BY g.gestion_id DESC LIMIT 1) AS gc_desde, "
    "(SELECT g.motivo_tipo FROM gestion_cliente g WHERE g.cotizacion_id = q.cotizacion_id ORDER BY g.gestion_id DESC LIMIT 1) AS gc_motivo "
    "FROM cotizaciones q JOIN comerciales co ON co.usuario = q.comercial "
    "LEFT JOIN capturas ca ON ca.captura_id = q.captura_id LEFT JOIN predios pr ON pr.captura_id = q.captura_id "
    "LEFT JOIN fichas fi ON fi.cotizacion_id = q.cotizacion_id AND fi.vigente = 1 ")


def fila_lista(p: dict, cotizacion_id: str):
    with bd.conectar(p["rutas"]["bd"]) as con:
        return con.execute(_SQL_LISTA + "WHERE q.cotizacion_id=?", (cotizacion_id,)).fetchone()


def listar_cotizaciones(p: dict, comercial: str) -> dict:
    """Las cotizaciones de UN comercial. Nunca devuelve las de otro."""
    tope = p["reglas"]["cotizaciones"]["max_en_lista"]
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute(_SQL_LISTA + "WHERE q.comercial=? ORDER BY q.creada_en DESC LIMIT ?", (comercial, tope)).fetchall()
        total = _contar(con, p, "comercial", comercial)
    return {"cotizaciones": [_fila_lista(f, p) for f in filas], "total": total}


def listar_todas(p: dict, comerciales: list[str] | None = None, desde: str | None = None, hasta: str | None = None) -> dict:
    """Consolidado para supervisión. `comerciales` limita a los que el supervisor tiene a cargo (None = todos)."""
    donde, args = ["1=1"], []
    limite = p["reglas"]["supervision"]["max_en_lista"]
    if comerciales is not None:
        if not comerciales:
            return {"cotizaciones": [], "total": 0}
        donde.append(f"q.comercial IN ({', '.join('?' for _ in comerciales)})")
        args += list(comerciales)
    if desde:
        donde.append("q.creada_en >= ?")
        args.append(desde)
    if hasta:
        donde.append("q.creada_en < ?")
        args.append(hasta)
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute(_SQL_LISTA + f"WHERE {' AND '.join(donde)} ORDER BY q.creada_en DESC LIMIT ?", (*args, limite)).fetchall()
    lista = [_fila_lista(f, p) for f in filas]
    return {"cotizaciones": lista, "total": len(lista)}
