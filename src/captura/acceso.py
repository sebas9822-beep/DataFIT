"""Usuarios y sesiones: quién entra a la aplicación y con qué rol (comercial, supervisor o administrador).

- Los usuarios iniciales salen de config/usuarios.yaml y se crean en la base al arrancar. Los demás los
  crea el administrador desde su pantalla (captura/admin.py); para un usuario que él tocó manda la base.
- Un comercial solo ve sus cotizaciones y sus fichas; un supervisor ve el consolidado de quienes tiene a cargo.
- El administrador gestiona la plataforma: no cotiza ni ve cotizaciones o clientes.
- Las claves se guardan con PBKDF2-SHA256 y sal propia; nunca en texto.
- La sesión viaja en una cookie HttpOnly; en la base solo queda la huella del token.
- Claves erradas seguidas bloquean al usuario unos minutos (params.yaml › acceso).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import bd


class AccesoDenegado(Exception):
    """Usuario o clave incorrectos, usuario inactivo o bloqueado."""


def normalizar_usuario(texto: str) -> str:
    """'Comercial 1', ' COMERCIAL1 ' y 'comercial1' son el mismo usuario."""
    return re.sub(r"\s+", "", str(texto or "")).lower()[:60]


def hash_clave(clave: str, sal: str, iteraciones: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), bytes.fromhex(sal), iteraciones).hex()


def _nueva_clave(clave: str, iteraciones: int) -> tuple[str, str]:
    sal = secrets.token_hex(16)
    return hash_clave(clave, sal, iteraciones), sal


def clave_inicial(p: dict, usuario: str) -> str:
    """La clave con la que nace un usuario (usuarios.yaml › clave_inicial)."""
    return _clave_inicial(p, usuario)


def nueva_clave(clave: str, iteraciones: int) -> tuple[str, str]:
    """(huella, sal) de una clave, para guardarla. Nunca se guarda la clave."""
    return _nueva_clave(clave, iteraciones)


def _clave_inicial(p: dict, usuario: str) -> str:
    modo = (p.get("usuarios") or {}).get("clave_inicial", "igual_al_usuario")
    if modo != "igual_al_usuario":
        raise ValueError(f"clave_inicial no reconocida en usuarios.yaml: {modo}")
    return usuario


ROLES = ("comercial", "supervisor", "admin")
ETIQUETA_ROL = {"comercial": "Comercial", "supervisor": "Supervisor", "admin": "Administrador"}


def _usuarios_configurados(p: dict) -> list[dict]:
    """Comerciales y supervisores de usuarios.yaml, cada uno con su rol."""
    cfg = p.get("usuarios") or {}
    lista = [dict(c, rol="comercial") for c in cfg.get("comerciales") or []]
    lista += [dict(s, rol="supervisor") for s in cfg.get("supervisores") or []]
    lista += [dict(a, rol="admin") for a in cfg.get("administradores") or []]
    return lista


def sembrar_comerciales(p: dict) -> int:
    """Crea los usuarios que falten y sincroniza nombre, activo y rol. Nunca toca una clave existente.

    Un usuario que el administrador creó, cambió o eliminó no se sincroniza: para él manda la base.
    """
    iteraciones = p["params"]["acceso"]["iteraciones_clave"]
    creados = 0
    with bd.conectar(p["rutas"]["bd"]) as con:
        existentes = {r[0] for r in con.execute("SELECT usuario FROM comerciales")}
        del_admin = {r[0] for r in con.execute("SELECT usuario FROM comerciales WHERE editado_en IS NOT NULL")}
        for c in _usuarios_configurados(p):
            usuario = normalizar_usuario(c["usuario"])
            nombre = str(c.get("nombre") or usuario).strip()
            activo = int(bool(c.get("activo", True)))
            supervisa = bd.a_json(c.get("supervisa", "todos")) if c["rol"] == "supervisor" else None
            if usuario in del_admin:
                continue
            if usuario in existentes:
                con.execute("UPDATE comerciales SET nombre=?, activo=?, rol=?, supervisa_json=? WHERE usuario=?",
                            (nombre, activo, c["rol"], supervisa, usuario))
                continue
            clave_hash, sal = _nueva_clave(_clave_inicial(p, usuario), iteraciones)
            con.execute("INSERT INTO comerciales (usuario, nombre, clave_hash, sal, iteraciones, activo, creado_en, rol, supervisa_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (usuario, nombre, clave_hash, sal, iteraciones, activo, bd.ahora(),
                                                      c["rol"], supervisa))
            creados += 1
    return creados


def a_cargo(p: dict, quien: dict) -> list[str] | None:
    """Usuarios cuyos datos puede ver `quien`: él mismo si es comercial; None (= todos) o una lista si es supervisor."""
    if quien.get("rol") != "supervisor":
        return [quien["usuario"]]
    supervisa = quien.get("supervisa", "todos")
    if supervisa in (None, "todos"):
        return None
    return [normalizar_usuario(u) for u in supervisa]


def puede_ver(p: dict, quien: dict, comercial: str) -> bool:
    """¿`quien` puede ver lo de `comercial`? Un comercial solo lo suyo; un supervisor, a quienes tiene a cargo."""
    permitidos = a_cargo(p, quien)
    return permitidos is None or comercial in permitidos


def listar_comerciales(p: dict, quien: dict) -> list[dict]:
    """Comerciales (rol comercial) que `quien` puede ver, en orden natural."""
    permitidos = a_cargo(p, quien)
    with bd.conectar(p["rutas"]["bd"]) as con:
        filas = con.execute("SELECT usuario, nombre, activo FROM comerciales WHERE rol='comercial' "
                            "ORDER BY length(usuario), usuario").fetchall()
    return [{"usuario": f["usuario"], "nombre": f["nombre"], "activo": bool(f["activo"])} for f in filas
            if permitidos is None or f["usuario"] in permitidos]


def _registrar(con, usuario: str, evento: str) -> None:
    con.execute("INSERT INTO accesos (usuario, evento, creado_en) VALUES (?,?,?)", (usuario, evento, bd.ahora()))


def _bloqueado(con, p: dict, usuario: str) -> bool:
    cfg = p["params"]["acceso"]
    desde = (datetime.now(timezone.utc) - timedelta(minutes=cfg["bloqueo_min"])).isoformat(timespec="seconds")
    eventos = [r[0] for r in con.execute(
        "SELECT evento FROM accesos WHERE usuario=? AND creado_en >= ? ORDER BY acceso_id DESC", (usuario, desde))]
    seguidos = 0
    for e in eventos:                      # cuenta las erradas desde la última entrada correcta
        if e == "entrada":
            break
        if e == "clave_errada":
            seguidos += 1
    return seguidos >= cfg["intentos_max"]


def autenticar(p: dict, usuario: str, clave: str) -> dict:
    """Devuelve {usuario, nombre}. Lanza AccesoDenegado con un mensaje apto para mostrar."""
    usuario = normalizar_usuario(usuario)
    clave = str(clave or "")
    if not usuario or not clave:
        raise AccesoDenegado("Escribe usuario y clave")
    # El evento se guarda y se confirma ANTES de responder: si se lanzara dentro del bloque, sqlite
    # desharía el registro y el bloqueo por intentos nunca llegaría.
    with bd.conectar(p["rutas"]["bd"]) as con:
        if _bloqueado(con, p, usuario):
            evento = "bloqueado"
        else:
            fila = con.execute("SELECT usuario, nombre, clave_hash, sal, iteraciones, activo, rol FROM comerciales "
                               "WHERE usuario=?", (usuario,)).fetchone()
            valida = bool(fila) and hmac.compare_digest(hash_clave(clave, fila["sal"], fila["iteraciones"]),
                                                        fila["clave_hash"])
            evento = "entrada" if valida and fila["activo"] else "clave_errada"
        _registrar(con, usuario, evento)
    if evento == "bloqueado":
        raise AccesoDenegado(f"Demasiados intentos. Espera {p['params']['acceso']['bloqueo_min']} minutos.")
    if evento == "clave_errada":
        raise AccesoDenegado("Usuario o clave incorrectos")
    return {"usuario": fila["usuario"], "nombre": fila["nombre"], "rol": fila["rol"]}


def _huella(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def crear_sesion(p: dict, usuario: str) -> str:
    token = secrets.token_urlsafe(32)
    vence = datetime.now(timezone.utc) + timedelta(hours=p["params"]["acceso"]["sesion_horas"])
    with bd.conectar(p["rutas"]["bd"]) as con:
        con.execute("INSERT INTO sesiones (token_sha256, usuario, creada_en, expira_en) VALUES (?,?,?,?)",
                    (_huella(token), usuario, bd.ahora(), vence.isoformat(timespec="seconds")))
    return token


def sesion(p: dict, token: str | None) -> dict | None:
    """{usuario, nombre, rol, supervisa} si el token es válido, vigente y el usuario sigue activo."""
    if not token or len(token) > 200:
        return None
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute(
            "SELECT s.usuario, c.nombre, c.rol, c.supervisa_json FROM sesiones s JOIN comerciales c ON c.usuario = s.usuario "
            "WHERE s.token_sha256=? AND s.cerrada_en IS NULL AND s.expira_en > ? AND c.activo = 1",
            (_huella(token), bd.ahora())).fetchone()
    if not fila:
        return None
    quien = {"usuario": fila["usuario"], "nombre": fila["nombre"], "rol": fila["rol"]}
    if fila["rol"] == "supervisor":
        quien["supervisa"] = bd.de_json(fila["supervisa_json"], "todos")
    return quien


def cerrar_sesion(p: dict, token: str | None) -> None:
    if not token:
        return
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute("SELECT usuario FROM sesiones WHERE token_sha256=? AND cerrada_en IS NULL",
                           (_huella(token),)).fetchone()
        if fila:
            con.execute("UPDATE sesiones SET cerrada_en=? WHERE token_sha256=?", (bd.ahora(), _huella(token)))
            _registrar(con, fila["usuario"], "salida")


def cambiar_clave(p: dict, usuario: str, nueva: str) -> None:
    usuario = normalizar_usuario(usuario)
    minimo = p["reglas"]["acceso"]["clave_minima"]
    if len(nueva) < minimo:
        raise ValueError(f"La clave debe tener al menos {minimo} caracteres")
    if normalizar_usuario(nueva) == usuario:
        raise ValueError("La clave no puede ser igual al usuario")
    clave_hash, sal = _nueva_clave(nueva, p["params"]["acceso"]["iteraciones_clave"])
    with bd.conectar(p["rutas"]["bd"]) as con:
        hecho = con.execute("UPDATE comerciales SET clave_hash=?, sal=?, iteraciones=?, clave_cambiada_en=? "
                            "WHERE usuario=?", (clave_hash, sal, p["params"]["acceso"]["iteraciones_clave"],
                                                bd.ahora(), usuario)).rowcount
    if not hecho:
        raise ValueError(f"No existe el usuario {usuario}")
    with bd.conectar(p["rutas"]["bd"]) as con:
        con.execute("UPDATE sesiones SET cerrada_en=? WHERE usuario=? AND cerrada_en IS NULL", (bd.ahora(), usuario))
        _registrar(con, usuario, "clave_cambiada")


def con_clave_inicial(p: dict) -> list[str]:
    """Comerciales activos que nunca cambiaron la clave inicial (para el verificador)."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        return [r[0] for r in con.execute(
            "SELECT usuario FROM comerciales WHERE activo = 1 AND clave_cambiada_en IS NULL ORDER BY length(usuario), usuario")]
