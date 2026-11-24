"""Administración de la plataforma (rol admin): usuarios, catálogo de inmobiliarias, respaldos y bitácora.

- El administrador gestiona lo que hace funcionar la aplicación. No cotiza ni ve cotizaciones o clientes.
- Nada se borra en silencio: un usuario eliminado queda inactivo y conserva su historial; una inmobiliaria
  eliminada pasa a `descartadas` con su motivo; cada cambio queda en `admin_bitacora`. Nunca se guarda una clave.
- El catálogo de inmobiliarias sigue viviendo en config/inmobiliarias.yaml. Se toca solo el sitio que cambia:
  el resto del archivo, comentarios incluidos, queda igual. Antes de escribir se comprueba que el archivo nuevo
  se puede leer y dice exactamente lo esperado, y se deja una copia del anterior en config/historial/.
- El respaldo es un .zip con la base operativa, la configuración y las fichas. El archivo .env no se copia:
  las llaves no salen de su sitio. El almacén no se copia: se rehace con el ETL.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import __version__, acceso, bd


class AdminInvalido(ValueError):
    """Dato que no pasa la validación de una acción de administración (HTTP 422)."""


CANDADO_CATALOGO = threading.Lock()
RE_USUARIO = re.compile(r"[a-z0-9][a-z0-9._-]*")
RE_DOMINIO = re.compile(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}")
RE_RESPALDO = re.compile(r"respaldo_\d{8}_\d{6}\.zip")
RE_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}")
ORDEN_ROL = {"admin": 0, "supervisor": 1, "comercial": 2}
# Orden de los campos de un sitio en el archivo, y cuáles van entre comillas.
CAMPOS_SITIO = ("id", "nombre", "dominio", "tipo", "nivel", "plataforma", "busqueda", "filtros", "prueba_externa",
                "permitida", "por_defecto", "lectura", "revision_terminos", "nota")
PALABRAS_YAML = {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"}
ENTRE_COMILLAS = {"nombre", "plataforma", "busqueda", "filtros", "prueba_externa", "nota"}     # texto libre: siempre con comillas


def _anotar(con, admin: str, accion: str, objeto: str | None, detalle: dict | None = None) -> None:
    con.execute("INSERT INTO admin_bitacora (administrador, accion, objeto, detalle_json, creado_en) VALUES (?,?,?,?,?)",
                (admin, accion, objeto, bd.a_json(detalle) if detalle else None, bd.ahora()))


def bitacora(p: dict) -> list[dict]:
    limite = p["reglas"]["administracion"]["bitacora_en_lista"]
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute("SELECT * FROM admin_bitacora ORDER BY evento_id DESC LIMIT ?", (limite,)).fetchall()
    return [{"evento_id": f["evento_id"], "administrador": f["administrador"], "accion": f["accion"], "objeto": f["objeto"],
             "detalle": bd.de_json(f["detalle_json"], None), "creado_en": f["creado_en"]} for f in filas]


# =============================================================================================
# Usuarios
# =============================================================================================
def _usuario_publico(f) -> dict:
    return {"usuario": f["usuario"], "nombre": f["nombre"], "rol": f["rol"], "activo": bool(f["activo"]),
            "eliminado": bool(f["eliminado_en"]), "eliminado_en": f["eliminado_en"],
            "supervisa": bd.de_json(f["supervisa_json"], "todos") if f["rol"] == "supervisor" else None,
            "clave_inicial": f["clave_cambiada_en"] is None, "creado_en": f["creado_en"], "creado_por": f["creado_por"],
            "cotizaciones": f["cotizaciones"], "ultima_entrada": f["ultima_entrada"]}


_SQL_USUARIOS = ("SELECT c.*, (SELECT COUNT(*) FROM cotizaciones q WHERE q.comercial = c.usuario) AS cotizaciones, "
                 "(SELECT MAX(a.creado_en) FROM accesos a WHERE a.usuario = c.usuario AND a.evento = 'entrada') AS ultima_entrada "
                 "FROM comerciales c ")


def listar_usuarios(p: dict) -> list[dict]:
    """Todos los usuarios, también los eliminados (su historial sigue en la base)."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = [_usuario_publico(f) for f in con.execute(_SQL_USUARIOS)]
    return sorted(filas, key=lambda u: (u["eliminado"], ORDEN_ROL.get(u["rol"], 9), len(u["usuario"]), u["usuario"]))


def _leer_usuario(con, usuario: str):
    return con.execute(_SQL_USUARIOS + "WHERE c.usuario=?", (usuario,)).fetchone()


def _nombre_valido(texto, p: dict) -> str:
    nombre = re.sub(r"\s+", " ", str(texto or "")).strip()
    if not nombre or len(nombre) > p["reglas"]["administracion"]["nombre_max"]:
        raise AdminInvalido(f"Escribe el nombre (hasta {p['reglas']['administracion']['nombre_max']} letras)")
    return nombre


def _supervisa_valido(con, valor) -> str:
    """`todos` o una lista de comerciales que existan. Devuelve el JSON que se guarda."""
    if valor in (None, "", "todos"):
        return bd.a_json("todos")
    if not isinstance(valor, list) or not valor:
        raise AdminInvalido("Indica a quién supervisa: todos o una lista de comerciales")
    pedidos = [acceso.normalizar_usuario(u) for u in valor]
    existentes = {r[0] for r in con.execute("SELECT usuario FROM comerciales WHERE rol='comercial'")}
    faltan = sorted(set(pedidos) - existentes)
    if faltan:
        raise AdminInvalido("No son comerciales: " + ", ".join(faltan))
    return bd.a_json(sorted(set(pedidos), key=lambda u: (len(u), u)))


def _clave_valida(p: dict, usuario: str, clave: str) -> None:
    minimo = p["reglas"]["acceso"]["clave_minima"]
    if len(clave) < minimo:
        raise AdminInvalido(f"La clave debe tener al menos {minimo} caracteres")
    if acceso.normalizar_usuario(clave) == usuario:
        raise AdminInvalido("La clave no puede ser igual al usuario")


def _otros_admin_activos(con, usuario: str) -> int:
    return con.execute("SELECT COUNT(*) FROM comerciales WHERE rol='admin' AND activo=1 AND usuario<>?", (usuario,)).fetchone()[0]


def crear_usuario(p: dict, admin: str, datos: dict) -> dict:
    cfg = p["reglas"]["administracion"]
    usuario = acceso.normalizar_usuario((datos or {}).get("usuario"))
    if not RE_USUARIO.fullmatch(usuario) or not (cfg["usuario_min"] <= len(usuario) <= cfg["usuario_max"]):
        raise AdminInvalido(f"El usuario lleva entre {cfg['usuario_min']} y {cfg['usuario_max']} letras minúsculas, números, punto o guion")
    nombre = _nombre_valido(datos.get("nombre"), p)
    rol = datos.get("rol")
    if rol not in acceso.ROLES:
        raise AdminInvalido("Elige el rol: comercial, supervisor o administrador")
    clave = str(datos.get("clave") or "")
    if clave:
        _clave_valida(p, usuario, clave)
    iteraciones = p["params"]["acceso"]["iteraciones_clave"]
    ahora = bd.ahora()
    with bd.conectar(p["rutas"]["bd"]) as con:
        previo = con.execute("SELECT eliminado_en FROM comerciales WHERE usuario=?", (usuario,)).fetchone()
        if previo:
            raise AdminInvalido(f"El usuario {usuario} ya existió y está eliminado: reactívalo desde la lista" if previo["eliminado_en"]
                                else f"El usuario {usuario} ya existe")
        supervisa = _supervisa_valido(con, datos.get("supervisa")) if rol == "supervisor" else None
        huella, sal = acceso.nueva_clave(clave or acceso.clave_inicial(p, usuario), iteraciones)
        con.execute("INSERT INTO comerciales (usuario, nombre, clave_hash, sal, iteraciones, activo, creado_en, rol, supervisa_json, "
                    "clave_cambiada_en, editado_en, creado_por) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (usuario, nombre, huella, sal, iteraciones, 1, ahora, rol, supervisa, ahora if clave else None, ahora, admin))
        _anotar(con, admin, "usuario_creado", usuario, {"nombre": nombre, "rol": rol, "clave": "definida" if clave else "inicial"})
        return _usuario_publico(_leer_usuario(con, usuario))


def editar_usuario(p: dict, admin: str, usuario: str, datos: dict) -> dict:
    """Cambia nombre, rol, a quién supervisa o si está activo. Reactivar también recupera a un eliminado."""
    usuario = acceso.normalizar_usuario(usuario)
    datos = datos or {}
    with bd.conectar(p["rutas"]["bd"]) as con:
        actual = _leer_usuario(con, usuario)
        if actual is None:
            raise LookupError("usuario no encontrado")
        nombre = _nombre_valido(datos["nombre"], p) if "nombre" in datos else actual["nombre"]
        rol = datos.get("rol", actual["rol"])
        if rol not in acceso.ROLES:
            raise AdminInvalido("Elige el rol: comercial, supervisor o administrador")
        activo = bool(datos["activo"]) if "activo" in datos else bool(actual["activo"])
        pierde_admin = actual["rol"] == "admin" and (rol != "admin" or not activo)
        if pierde_admin and usuario == admin:
            raise AdminInvalido("No puedes quitarte a ti mismo el acceso de administrador")
        if pierde_admin and actual["activo"] and not _otros_admin_activos(con, usuario):
            raise AdminInvalido("Debe quedar al menos un administrador activo")
        if rol == "supervisor":
            supervisa = _supervisa_valido(con, datos["supervisa"]) if "supervisa" in datos else (actual["supervisa_json"] or bd.a_json("todos"))
        else:
            supervisa = None
        antes = {"nombre": actual["nombre"], "rol": actual["rol"], "activo": bool(actual["activo"]),
                 "supervisa": bd.de_json(actual["supervisa_json"], None), "eliminado": bool(actual["eliminado_en"])}
        despues = {"nombre": nombre, "rol": rol, "activo": activo, "supervisa": bd.de_json(supervisa, None),
                   "eliminado": bool(actual["eliminado_en"]) and not activo}
        if antes == despues:
            return _usuario_publico(actual)
        ahora = bd.ahora()
        con.execute("UPDATE comerciales SET nombre=?, rol=?, activo=?, supervisa_json=?, editado_en=?, "
                    "eliminado_en=CASE WHEN ?=1 THEN NULL ELSE eliminado_en END WHERE usuario=?",
                    (nombre, rol, int(activo), supervisa, ahora, int(activo), usuario))
        if not activo or rol != actual["rol"]:           # deja de poder entrar, o cambia lo que puede ver: entra de nuevo
            con.execute("UPDATE sesiones SET cerrada_en=? WHERE usuario=? AND cerrada_en IS NULL", (ahora, usuario))
        _anotar(con, admin, "usuario_editado", usuario, {"antes": antes, "despues": despues})
        return _usuario_publico(_leer_usuario(con, usuario))


def eliminar_usuario(p: dict, admin: str, usuario: str) -> dict:
    """Deja al usuario sin acceso y marcado como eliminado. Sus cotizaciones, clientes y fichas se conservan."""
    usuario = acceso.normalizar_usuario(usuario)
    if usuario == admin:
        raise AdminInvalido("No puedes eliminar tu propio usuario")
    with bd.conectar(p["rutas"]["bd"]) as con:
        actual = _leer_usuario(con, usuario)
        if actual is None:
            raise LookupError("usuario no encontrado")
        if actual["eliminado_en"]:
            return _usuario_publico(actual)
        if actual["rol"] == "admin" and actual["activo"] and not _otros_admin_activos(con, usuario):
            raise AdminInvalido("Debe quedar al menos un administrador activo")
        ahora = bd.ahora()
        con.execute("UPDATE comerciales SET activo=0, eliminado_en=?, editado_en=? WHERE usuario=?", (ahora, ahora, usuario))
        con.execute("UPDATE sesiones SET cerrada_en=? WHERE usuario=? AND cerrada_en IS NULL", (ahora, usuario))
        _anotar(con, admin, "usuario_eliminado", usuario, {"nombre": actual["nombre"], "rol": actual["rol"],
                                                         "cotizaciones_que_conserva": actual["cotizaciones"]})
        return _usuario_publico(_leer_usuario(con, usuario))


def restablecer_clave(p: dict, admin: str, usuario: str, clave: str | None = None) -> dict:
    """Con `clave`, la deja definida; sin ella, vuelve a la clave inicial. Cierra las sesiones abiertas del usuario."""
    usuario = acceso.normalizar_usuario(usuario)
    clave = str(clave or "")
    if clave:
        _clave_valida(p, usuario, clave)
    iteraciones = p["params"]["acceso"]["iteraciones_clave"]
    huella, sal = acceso.nueva_clave(clave or acceso.clave_inicial(p, usuario), iteraciones)
    ahora = bd.ahora()
    with bd.conectar(p["rutas"]["bd"]) as con:
        if _leer_usuario(con, usuario) is None:
            raise LookupError("usuario no encontrado")
        con.execute("UPDATE comerciales SET clave_hash=?, sal=?, iteraciones=?, clave_cambiada_en=?, editado_en=? WHERE usuario=?",
                    (huella, sal, iteraciones, ahora if clave else None, ahora, usuario))
        con.execute("UPDATE sesiones SET cerrada_en=? WHERE usuario=? AND cerrada_en IS NULL", (ahora, usuario))
        con.execute("INSERT INTO accesos (usuario, evento, creado_en) VALUES (?,?,?)", (usuario, "clave_cambiada", ahora))
        _anotar(con, admin, "clave_restablecida", usuario, {"clave": "definida" if clave else "inicial"})
        return _usuario_publico(_leer_usuario(con, usuario))


# =============================================================================================
# Catálogo de inmobiliarias (config/inmobiliarias.yaml)
# =============================================================================================
def _ruta_catalogo(p: dict) -> Path:
    return Path(p["rutas"]["config"]) / "inmobiliarias.yaml"


def _valor_yaml(valor) -> str:
    if isinstance(valor, bool):
        return "true" if valor else "false"
    texto = str(valor)
    simple = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", texto) and texto.lower() not in PALABRAS_YAML
    return texto if simple else json.dumps(texto, ensure_ascii=False)


def _linea_campo(campo: str, valor) -> str:
    texto = json.dumps(str(valor), ensure_ascii=False) if campo in ENTRE_COMILLAS else _valor_yaml(valor)
    return f"    {campo}: {texto}"


def _bloque_sitio(sitio: dict) -> list[str]:
    return [f"  - id: {_valor_yaml(sitio['id'])}"] + [_linea_campo(c, sitio[c]) for c in CAMPOS_SITIO[1:] if sitio.get(c) is not None]


def _partir(texto: str) -> dict:
    """Separa el archivo en: cabeza (hasta `sitios:`), un bloque de líneas por sitio, lo que hay entre el último
    sitio y `descartadas:`, las líneas de descartadas y el final. Nada se interpreta todavía."""
    lineas = texto.split("\n")
    try:
        i_sitios = lineas.index("sitios:")
    except ValueError:
        raise AdminInvalido("config/inmobiliarias.yaml no tiene la sección 'sitios:'") from None
    i_desc = lineas.index("descartadas:") if "descartadas:" in lineas else len(lineas)
    bloques: list[tuple[str, list[str]]] = []
    i = i_sitios + 1
    while i < i_desc:
        m = re.fullmatch(r"  - id: (\S+)", lineas[i].rstrip())
        if m:
            bloques.append((m.group(1).strip('"'), [lineas[i]]))
        elif bloques and lineas[i].startswith("    ") and lineas[i].strip():
            bloques[-1][1].append(lineas[i])
        else:
            break                                      # línea en blanco o comentario: terminó la lista de sitios
        i += 1
    medio = lineas[i:i_desc + 1] if i_desc < len(lineas) else lineas[i:]
    resto = lineas[i_desc + 1:] if i_desc < len(lineas) else []
    n_desc = 0
    while n_desc < len(resto) and resto[n_desc].startswith("  - "):
        n_desc += 1
    return {"cabeza": lineas[:i_sitios + 1], "bloques": bloques, "medio": medio, "descartadas": resto[:n_desc],
            "final": resto[n_desc:], "tiene_descartadas": i_desc < len(lineas)}


def _armar(partes: dict) -> str:
    lineas = list(partes["cabeza"])
    for _, bloque in partes["bloques"]:
        lineas += bloque
    medio = list(partes["medio"])
    if partes["descartadas"] and not partes["tiene_descartadas"]:
        medio = [x for x in medio if x.strip()] + ["", "descartadas:"]
    return "\n".join(lineas + medio + partes["descartadas"] + partes["final"])


def leer_catalogo(p: dict) -> dict:
    """El catálogo tal como está en el archivo: sitios con todos sus campos y descartadas con su motivo."""
    ruta = _ruta_catalogo(p)
    datos = (yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}) if ruta.exists() else {}
    return {"sitios": list(datos.get("sitios") or []), "descartadas": list(datos.get("descartadas") or [])}


def _dominio_limpio(texto) -> str:
    d = str(texto or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0]
    d = d[4:] if d.startswith("www.") else d
    if not RE_DOMINIO.fullmatch(d):
        raise AdminInvalido("Escribe el dominio del sitio, sin https ni www (por ejemplo: inmobiliaria.com.co)")
    return d


def _id_para(dominio: str, usados: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]", "", dominio.split(".")[0]) or "sitio"
    candidato, n = base, 1
    while candidato in usados:
        n += 1
        candidato = f"{base}{n}"
    return candidato


def _validar_sitio(datos: dict, p: dict, actual: dict | None, otros: list[dict]) -> dict:
    """Devuelve el sitio completo como debe quedar. `actual` es el sitio que se edita (None = uno nuevo)."""
    cfg = p["reglas"]["administracion"]
    datos = datos or {}
    sitio = dict(actual or {})
    nombre = re.sub(r"\s+", " ", str(datos.get("nombre", sitio.get("nombre", "")) or "")).strip()
    if not nombre or len(nombre) > cfg["nombre_max"]:
        raise AdminInvalido(f"Escribe el nombre del sitio (hasta {cfg['nombre_max']} letras)")
    dominio = _dominio_limpio(datos.get("dominio", sitio.get("dominio")))
    if any(o["dominio"] == dominio for o in otros):
        raise AdminInvalido(f"El dominio {dominio} ya está en el catálogo")
    tipo = datos.get("tipo", sitio.get("tipo", "inmobiliaria"))
    if tipo not in cfg["tipos_sitio"]:
        raise AdminInvalido("Tipo de sitio no reconocido: " + ", ".join(cfg["tipos_sitio"]))
    nivel = datos.get("nivel", sitio.get("nivel", cfg["niveles_sitio"][0]))
    if nivel not in cfg["niveles_sitio"]:
        raise AdminInvalido("Nivel no reconocido: " + ", ".join(cfg["niveles_sitio"]))
    busqueda = str(datos.get("busqueda", sitio.get("busqueda", "")) or "").strip()
    try:
        anfitrion = (urlparse(busqueda).hostname or "").lower()
    except ValueError:
        anfitrion = ""
    anfitrion = anfitrion[4:] if anfitrion.startswith("www.") else anfitrion
    if not busqueda.startswith("https://") or not (anfitrion == dominio or anfitrion.endswith("." + dominio)) or len(busqueda) > cfg["url_max"]:
        raise AdminInvalido(f"La dirección de búsqueda debe empezar por https:// y ser del dominio {dominio}")
    permitida = bool(datos.get("permitida", sitio.get("permitida", True)))
    por_defecto = bool(datos.get("por_defecto", sitio.get("por_defecto", True))) and permitida
    revision = str(datos.get("revision_terminos", sitio.get("revision_terminos", "pendiente")) or "pendiente").strip()
    if revision != "pendiente" and not RE_FECHA.fullmatch(revision):
        raise AdminInvalido("Revisión de términos: 'pendiente' o la fecha en que se revisaron (AAAA-MM-DD)")
    nota = re.sub(r"\s+", " ", str(datos.get("nota", sitio.get("nota", "")) or "")).strip()
    if len(nota) > cfg["nota_max"]:
        raise AdminInvalido(f"La nota admite hasta {cfg['nota_max']} letras")
    if not permitida and not nota:
        raise AdminInvalido("Si el sitio no se permite, escribe en la nota por qué")
    cambia_lectura = actual is None or dominio != actual.get("dominio") or busqueda != actual.get("busqueda")
    sitio.update(nombre=nombre, dominio=dominio, tipo=tipo, nivel=nivel, busqueda=busqueda, permitida=permitida,
                 por_defecto=por_defecto, revision_terminos=revision, nota=nota or None,
                 lectura="sin_probar" if cambia_lectura else actual.get("lectura", "sin_probar"))
    if actual is None:
        sitio["id"] = _id_para(dominio, {o["id"] for o in otros})
    return {k: v for k, v in sitio.items() if v is not None}


def _reemplazar_campos(bloque: list[str], antes: dict, despues: dict) -> list[str]:
    """Cambia en el bloque solo las líneas de los campos que cambiaron; las demás quedan como estaban."""
    nuevo = list(bloque)
    for campo in CAMPOS_SITIO[1:]:
        if antes.get(campo) == despues.get(campo):
            continue
        pos = next((i for i, linea in enumerate(nuevo) if re.match(rf"    {campo}:(\s|$)", linea)), None)
        if despues.get(campo) is None:
            if pos is not None:
                del nuevo[pos]
        elif pos is not None:
            nuevo[pos] = _linea_campo(campo, despues[campo])
        else:                                          # campo nuevo: va en su sitio según el orden del archivo
            siguientes = CAMPOS_SITIO[CAMPOS_SITIO.index(campo) + 1:]
            destino = next((i for i, linea in enumerate(nuevo) if any(re.match(rf"    {s}:(\s|$)", linea) for s in siguientes)), len(nuevo))
            nuevo.insert(destino, _linea_campo(campo, despues[campo]))
    return nuevo


def _escribir_catalogo(p: dict, partes: dict, esperado: dict) -> None:
    """Escribe el archivo solo si al leerlo dice exactamente `esperado`. Deja copia del anterior."""
    ruta = _ruta_catalogo(p)
    texto = _armar(partes)
    try:
        leido = yaml.safe_load(texto) or {}
    except yaml.YAMLError as e:
        raise AdminInvalido(f"El catálogo no se cambió: el archivo resultante no se puede leer ({type(e).__name__})") from None
    if (leido.get("sitios") or []) != esperado["sitios"] or (leido.get("descartadas") or []) != esperado["descartadas"]:
        raise AdminInvalido("El catálogo no se cambió: el archivo resultante no coincide con lo esperado. "
                            "Revisa config/inmobiliarias.yaml a mano")
    if ruta.exists():
        historial = ruta.parent / "historial"
        historial.mkdir(parents=True, exist_ok=True)
        (historial / f"inmobiliarias_{datetime.now():%Y%m%d_%H%M%S_%f}.yaml").write_bytes(ruta.read_bytes())
    temporal = ruta.with_suffix(".yaml.tmp")
    temporal.write_text(texto, encoding="utf-8", newline="\n")
    os.replace(temporal, ruta)
    actual = p.setdefault("inmobiliarias", {})         # la aplicación ve el cambio sin reiniciar
    actual.clear()
    actual.update(sitios=esperado["sitios"], descartadas=esperado["descartadas"])


def _abrir_catalogo(p: dict) -> tuple[dict, dict]:
    ruta = _ruta_catalogo(p)
    if not ruta.exists():
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("# Inmobiliarias y portales donde el RPA busca anuncios vigentes.\n\nsitios:\n\ndescartadas:\n",
                        encoding="utf-8", newline="\n")
    texto = ruta.read_text(encoding="utf-8").replace("\r\n", "\n")
    return _partir(texto), leer_catalogo(p)


def guardar_sitio(p: dict, admin: str, datos: dict, sitio_id: str | None = None) -> dict:
    """Agrega un sitio (sin `sitio_id`) o cambia uno que existe. Devuelve el sitio como quedó."""
    with CANDADO_CATALOGO:
        partes, cat = _abrir_catalogo(p)
        actual = next((s for s in cat["sitios"] if s["id"] == sitio_id), None) if sitio_id else None
        if sitio_id and actual is None:
            raise LookupError("sitio no encontrado")
        otros = [s for s in cat["sitios"] if s["id"] != sitio_id]
        sitio = _validar_sitio(datos, p, actual, otros)
        descartadas = list(cat["descartadas"])
        if actual is None:
            partes["bloques"].append((sitio["id"], _bloque_sitio(sitio)))
            sitios = cat["sitios"] + [sitio]
            # un sitio que estaba descartado y vuelve: sale de descartadas (la bitácora conserva la historia)
            quedan = [(d, linea) for d, linea in zip(descartadas, partes["descartadas"])
                      if str(d.get("dominio", "")).removeprefix("www.").split("/")[0] != sitio["dominio"]]
            if len(quedan) != len(descartadas):
                if len(descartadas) != len(partes["descartadas"]):
                    raise AdminInvalido("El catálogo no se cambió: la lista de descartadas no tiene el formato esperado")
                descartadas, partes["descartadas"] = [d for d, _ in quedan], [linea for _, linea in quedan]
        else:
            if sitio == actual:
                return sitio
            pos = next(i for i, (ident, _) in enumerate(partes["bloques"]) if ident == sitio_id)
            partes["bloques"][pos] = (sitio_id, _reemplazar_campos(partes["bloques"][pos][1], actual, sitio))
            sitios = [sitio if s["id"] == sitio_id else s for s in cat["sitios"]]
        _escribir_catalogo(p, partes, {"sitios": sitios, "descartadas": descartadas})
    with bd.conectar(p["rutas"]["bd"]) as con:
        if actual is None:
            _anotar(con, admin, "sitio_agregado", sitio["id"], {"sitio": sitio})
        else:
            cambios = {k: {"antes": actual.get(k), "despues": sitio.get(k)} for k in CAMPOS_SITIO if actual.get(k) != sitio.get(k)}
            _anotar(con, admin, "sitio_editado", sitio["id"], cambios)
    return sitio


def eliminar_sitio(p: dict, admin: str, sitio_id: str, motivo) -> dict:
    """Saca el sitio de la lista que se consulta y lo deja en `descartadas` con su motivo."""
    motivo = re.sub(r"\s+", " ", str(motivo or "")).strip()
    cfg = p["reglas"]["administracion"]
    if len(motivo) < cfg["motivo_min_letras"] or len(motivo) > cfg["nota_max"]:
        raise AdminInvalido("Escribe por qué se elimina el sitio: queda registrado en el catálogo")
    with CANDADO_CATALOGO:
        partes, cat = _abrir_catalogo(p)
        actual = next((s for s in cat["sitios"] if s["id"] == sitio_id), None)
        if actual is None:
            raise LookupError("sitio no encontrado")
        descartada = {"nombre": actual["nombre"], "dominio": actual["dominio"],
                      "motivo": f"Eliminada por el administrador el {datetime.now():%Y-%m-%d}: {motivo}"}
        partes["bloques"] = [b for b in partes["bloques"] if b[0] != sitio_id]
        partes["descartadas"] = partes["descartadas"] + [
            "  - {" + ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in descartada.items()) + "}"]
        _escribir_catalogo(p, partes, {"sitios": [s for s in cat["sitios"] if s["id"] != sitio_id],
                                       "descartadas": cat["descartadas"] + [descartada]})
    with bd.conectar(p["rutas"]["bd"]) as con:
        _anotar(con, admin, "sitio_eliminado", sitio_id, {"sitio": actual, "motivo": motivo})
    return descartada


# =============================================================================================
# Respaldos
# =============================================================================================
TABLAS_RESPALDO = ("capturas", "entorno_items", "ofertas", "clientes", "cotizaciones", "fichas", "comerciales", "gestion_cliente")


def _carpeta_respaldos(p: dict) -> Path:
    return Path(p["rutas"]["respaldos"])


def listar_respaldos(p: dict) -> list[dict]:
    carpeta = _carpeta_respaldos(p)
    if not carpeta.exists():
        return []
    archivos = sorted((f for f in carpeta.iterdir() if RE_RESPALDO.fullmatch(f.name)), key=lambda f: f.name, reverse=True)
    return [{"nombre": f.name, "bytes": f.stat().st_size, "creado_en": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
             "carpeta": str(carpeta)} for f in archivos[:p["params"]["respaldos"]["en_lista"]]]


def crear_respaldo(p: dict, admin: str) -> dict:
    """Un .zip con la base operativa (copia consistente aunque la aplicación esté en uso), la configuración y las fichas."""
    cfg = p["params"]["respaldos"]
    carpeta = _carpeta_respaldos(p)
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now()
    destino = carpeta / f"respaldo_{marca:%Y%m%d_%H%M%S}.zip"
    copia_bd, temporal = carpeta / f".{destino.stem}.db", carpeta / f".{destino.stem}.tmp"
    try:
        origen, copia = sqlite3.connect(p["rutas"]["bd"]), sqlite3.connect(copia_bd)
        try:
            origen.backup(copia)
            filas = {t: copia.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLAS_RESPALDO}
        finally:
            copia.close()
            origen.close()
        incluye = ["data/captura.db"]
        with zipfile.ZipFile(temporal, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copia_bd, "data/captura.db")
            if cfg["incluir_config"]:
                for f in sorted(Path(p["rutas"]["config"]).glob("*.yaml")):
                    z.write(f, f"config/{f.name}")
                incluye.append("config/*.yaml")
            n_fichas = 0
            raiz_fichas = Path(p["rutas"]["fichas"])
            if cfg["incluir_fichas"] and raiz_fichas.exists():
                for f in sorted(raiz_fichas.rglob("*")):
                    if f.is_file():
                        z.write(f, "fichas_clientes/" + f.relative_to(raiz_fichas).as_posix())
                        n_fichas += 1
                incluye.append("fichas_clientes/")
            manifiesto = {"creado_en": marca.isoformat(timespec="seconds"), "creado_por": admin, "version_app": __version__,
                          "incluye": incluye, "filas": filas, "archivos_de_fichas": n_fichas,
                          "no_incluye": [".env (las llaves no se copian)", "data/almacen.db (se rehace con el ETL)"]}
            z.writestr("manifiesto.json", json.dumps(manifiesto, ensure_ascii=False, indent=2))
        os.replace(temporal, destino)
    finally:
        for f in (copia_bd, temporal):
            if f.exists():
                f.unlink()
    with bd.conectar(p["rutas"]["bd"]) as con:
        _anotar(con, admin, "respaldo_creado", destino.name, {"bytes": destino.stat().st_size, "filas": filas,
                                                             "archivos_de_fichas": n_fichas})
    return {"nombre": destino.name, "bytes": destino.stat().st_size, "carpeta": str(carpeta), "creado_en": manifiesto["creado_en"],
            "filas": filas, "archivos_de_fichas": n_fichas}


def eliminar_respaldo(p: dict, admin: str, nombre: str) -> None:
    if not RE_RESPALDO.fullmatch(str(nombre or "")):
        raise AdminInvalido("nombre de respaldo no válido")
    archivo = _carpeta_respaldos(p) / nombre
    if not archivo.exists():
        raise LookupError("respaldo no encontrado")
    tamano = archivo.stat().st_size
    archivo.unlink()
    with bd.conectar(p["rutas"]["bd"]) as con:
        _anotar(con, admin, "respaldo_eliminado", nombre, {"bytes": tamano})


# =============================================================================================
# Estado del sistema (sin datos de clientes)
# =============================================================================================
def estado_sistema(p: dict) -> dict:
    ruta_bd, ruta_alm = Path(p["rutas"]["bd"]), Path(p["rutas"]["almacen"])
    with bd.conectar(p["rutas"]["bd"]) as con:
        usuarios = {f"{r['rol']}{'' if r['activo'] else '_inactivos'}": r["n"] for r in con.execute(
            "SELECT rol, activo, COUNT(*) AS n FROM comerciales GROUP BY rol, activo")}
        capturas = {r["estado"]: r["n"] for r in con.execute("SELECT estado, COUNT(*) AS n FROM capturas GROUP BY estado")}
        conteos = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("clientes", "cotizaciones", "fichas")}
        clave_inicial = con.execute("SELECT COUNT(*) FROM comerciales WHERE activo=1 AND clave_cambiada_en IS NULL").fetchone()[0]
    respaldos = listar_respaldos(p)
    cat = p.get("inmobiliarias") or {}
    sitios = cat.get("sitios") or []
    return {"version": __version__, "base": {"ruta": str(ruta_bd), "bytes": ruta_bd.stat().st_size if ruta_bd.exists() else 0},
            "almacen": {"ruta": str(ruta_alm), "bytes": ruta_alm.stat().st_size if ruta_alm.exists() else 0},
            "usuarios": usuarios, "usuarios_con_clave_inicial": clave_inicial, "capturas": capturas, **conteos,
            "inmobiliarias": {"sitios": len(sitios), "permitidas": sum(1 for s in sitios if s.get("permitida")),
                              "encendidas": sum(1 for s in sitios if s.get("permitida") and s.get("por_defecto")),
                              "terminos_pendientes": sum(1 for s in sitios if s.get("revision_terminos", "pendiente") == "pendiente"),
                              "descartadas": len(cat.get("descartadas") or [])},
            "ultimo_respaldo": respaldos[0] if respaldos else None}
