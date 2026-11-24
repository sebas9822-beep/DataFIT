"""Servicio de fichas de la aplicación única: de la cotización a la ficha guardada.

    cotización (comercial + cliente + captura + datos del inmueble)
        → ETL de la captura al almacén (esquema estrella)
        → motor de ficha sobre el almacén (reglas oficiales)
        → fichas_clientes/<NIT o cédula>/FICHA_<fecha>_<cotización>_v<n>.md  (+ .json)
        → registro en la base operativa (tabla fichas) y en el almacén (fact_ficha)

    python -m ficha.servicio reto        fichas de los 11 inmuebles de prueba, leídas del almacén, y validador oficial
    python -m ficha.servicio pendientes  genera las fichas de las cotizaciones que no la tienen
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

from almacen import esquema, etl
from almacen import normalizacion as nz
from captura import __version__ as VERSION_APP
from captura import bd, clientes

from . import __version__, desde_almacen, plantilla_cliente
from .ficha import renderizar_markdown
from .validacion import validar_contra_esperado


def _json_default(o):
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _texto_json(contenido) -> str:
    return json.dumps(contenido, ensure_ascii=False, indent=2, default=_json_default) + "\n"


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def carpeta_cliente(p: dict, documento: str) -> Path:
    """Carpeta de las fichas de un cliente: su NIT o cédula, solo dígitos."""
    doc = "".join(ch for ch in str(documento) if ch.isdigit())
    if not doc:
        raise ValueError("El cliente no tiene documento: no se puede ubicar su carpeta de fichas")
    return Path(p["rutas"]["fichas"]) / doc


def _fecha_legible(t: datetime) -> str:
    return f"{t.day} de {nz.MESES[t.month]} de {t.year}"


def generar_para_cotizacion(p: dict, cotizacion_id: str, etl_modo: str = "captura") -> dict:
    """Genera (o regenera) la ficha de una cotización y la deja en la carpeta del cliente.

    `etl_modo`: "captura" carga la captura al almacén antes de leer; "gestion" solo refresca cotizaciones
    (sirve cuando la captura ya está cargada y cambiaron los datos del inmueble).
    Devuelve el registro de la ficha. Lanza LookupError si la cotización no existe.
    """
    q = clientes.cotizacion(p, cotizacion_id)
    if q is None:
        raise LookupError("cotización no encontrada")
    cuentan = p["reglas"]["cotizaciones"]["estados_que_cuentan"]
    if q["estado"] not in cuentan or not q["captura_id"]:
        return {"cotizacion_id": cotizacion_id, "estado": "sin_captura", "ficha_id": None,
                "mensaje": "La captura de esta cotización no terminó bien: no hay datos para armar la ficha"}
    pf = etl.params_ficha(p)
    corrida = etl.ejecutar(p, "captura", [q["captura_id"]]) if etl_modo == "captura" else etl.ejecutar(p, "gestion")
    con = esquema.conectar(p["rutas"]["almacen"])
    try:
        ficha = desde_almacen.ficha_inmueble(con, p, pf, f"cot:{cotizacion_id}", date.today().isoformat())
    finally:
        con.close()

    desfase = p["almacen"]["tiempo"]["desfase_utc_horas"]
    ahora_utc = bd.ahora()
    local = nz.a_local(ahora_utc, desfase)
    creada = nz.a_local(q["creada_en"], desfase)
    with bd.conectar(p["rutas"]["bd"]) as con_op:
        version = (con_op.execute("SELECT MAX(version) FROM fichas WHERE cotizacion_id=?", (cotizacion_id,)).fetchone()[0] or 0) + 1
    ficha_id = f"{cotizacion_id}:v{version}"
    tipo_doc = q["cliente_tipo_documento"] or p["reglas"]["clientes"].get("tipo_por_defecto", "CC")
    ficha["cotizacion"] = {
        "ficha_id": ficha_id, "version": version, "cotizacion_id": cotizacion_id,
        "generada_en": f"{local.isoformat(timespec='seconds')}{desfase:+03d}:00",
        "fecha_legible": _fecha_legible(creada or local),
        "creada_en": q["creada_en"], "estado": q["estado"], "reusada": bool(q["reusada"]),
        "comercial": {"usuario": q["comercial"], "nombre": q["comercial_nombre"]},
        "cliente": {"cliente_id": q["cliente_id"], "tipo_documento": tipo_doc, "documento": q["cliente_documento"],
                    "nombre": q["cliente_nombre"]},
        "captura_id": q["captura_id"], "evidencia_sha256": q["evidencia_sha256"],
        "evidencia": {"fuentes_con_novedad": (bd.de_json(q["evidencia_json"], {}) or {}).get("fuentes_con_novedad", [])},
        "version_app": VERSION_APP,
    }
    res = desde_almacen.resumen(ficha)
    if "area_del_inmueble" in ficha["datos_faltantes"] or "tipo_de_inmueble" in ficha["datos_faltantes"]:
        estado, mensaje = "incompleta", "Faltan datos del inmueble: complétalos para buscar comparables"
    elif not res["n"]:
        estado, mensaje = "sin_mercado", ficha["mercado"].get("motivo_sin_mercado") or "Ninguna oferta cumple el criterio de comparables"
    else:
        estado, mensaje = "ok", None

    carpeta = carpeta_cliente(p, q["cliente_documento"])
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = plantilla_cliente.nombre_archivo(cotizacion_id, (creada or local).date().isoformat(), version)
    texto_md = plantilla_cliente.renderizar(ficha, p["reglas"]["fichas"].get("max_comparables_en_md", 40))
    texto_json = _texto_json(ficha)
    (carpeta / f"{nombre}.md").write_text(texto_md, encoding="utf-8", newline="\n")
    (carpeta / f"{nombre}.json").write_text(texto_json, encoding="utf-8", newline="\n")
    registro = {
        "ficha_id": ficha_id, "cotizacion_id": cotizacion_id, "version": version, "vigente": 1,
        "comercial": q["comercial"], "cliente_id": q["cliente_id"], "generada_en": ahora_utc, "estado": estado,
        "mensaje": mensaje, "ruta_md": f"{carpeta.name}/{nombre}.md", "ruta_json": f"{carpeta.name}/{nombre}.json",
        "sha256_md": _sha(texto_md), "sha256_json": _sha(texto_json),
        "resumen_json": json.dumps(res, ensure_ascii=False, sort_keys=True, default=_json_default),
        "corrida_etl": corrida["corrida_id"], "huella_parametros": pf["_meta"]["hash_params"][:16], "version_motor": __version__}
    with bd.conectar(p["rutas"]["bd"]) as con_op:
        con_op.execute("UPDATE fichas SET vigente=0 WHERE cotizacion_id=?", (cotizacion_id,))
        bd.insertar(con_op, "fichas", registro)
    etl.ejecutar(p, "gestion")                       # la ficha recién registrada entra a fact_ficha
    return {**registro, "resumen": res}


def leer(p: dict, cotizacion_id: str) -> dict | None:
    """La ficha vigente de una cotización: registro, Markdown y resumen. None si no tiene."""
    with bd.conectar(p["rutas"]["bd"]) as con:
        fila = con.execute("SELECT * FROM fichas WHERE cotizacion_id=? AND vigente=1", (cotizacion_id,)).fetchone()
        versiones = [dict(r) for r in con.execute(
            "SELECT version, generada_en, estado, ruta_md FROM fichas WHERE cotizacion_id=? ORDER BY version DESC", (cotizacion_id,))]
    if fila is None:
        return None
    ruta = Path(p["rutas"]["fichas"]) / fila["ruta_md"]
    texto = ruta.read_text(encoding="utf-8") if ruta.exists() else None
    integro = texto is not None and _sha(texto) == fila["sha256_md"]
    return {"ficha_id": fila["ficha_id"], "cotizacion_id": cotizacion_id, "version": fila["version"],
            "estado": fila["estado"], "mensaje": fila["mensaje"], "generada_en": fila["generada_en"],
            "ruta_md": fila["ruta_md"], "ruta_json": fila["ruta_json"], "sha256_md": fila["sha256_md"],
            "archivo_presente": texto is not None, "archivo_integro": integro, "markdown": texto,
            "resumen": bd.de_json(fila["resumen_json"], {}), "versiones": versiones, "corrida_etl": fila["corrida_etl"],
            "ofertas": _ofertas(p, fila["ruta_json"])}


def _ofertas(p: dict, ruta_json: str | None) -> dict | None:
    """Lo que la pantalla muestra en "Ofertas inmobiliarias": tomado de la ficha, sin volver a calcular nada."""
    ruta = Path(p["rutas"]["fichas"]) / str(ruta_json or "")
    if not ruta_json or not ruta.exists():
        return None
    f = json.loads(ruta.read_text(encoding="utf-8"))
    m, busca = f["mercado"], f.get("busqueda_cliente") or {}
    tope = p["reglas"]["fichas"]["max_comparables_en_md"]
    return {"busca": busca.get("tipos", []), "por_tipo": busca.get("oferta_por_tipo", []), "vigencia": m.get("vigencia"),
            "base": m.get("base_mercado"), "referencia_historica": f.get("referencia_historica") or {},
            "comparables": [{k: c.get(k) for k in ("fuente", "tipo_inmueble", "area_m2", "precio", "precio_m2", "barrio", "url",
                                                   "fecha", "vista_el", "distancia_m")} for c in m.get("comparables", [])[:tope]]}


def generar_pendientes(p: dict) -> list[dict]:
    """Fichas de las cotizaciones que cuentan y aún no la tienen (p. ej. las anteriores a la aplicación única)."""
    cuentan = p["reglas"]["cotizaciones"]["estados_que_cuentan"]
    with bd.conectar(p["rutas"]["bd"]) as con:
        pendientes = [r[0] for r in con.execute(
            f"SELECT q.cotizacion_id FROM cotizaciones q WHERE q.estado IN ({', '.join('?' for _ in cuentan)}) "
            "AND q.captura_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM fichas f WHERE f.cotizacion_id=q.cotizacion_id) "
            "ORDER BY q.creada_en", cuentan)]
    return [generar_para_cotizacion(p, c) for c in pendientes]


# ---------------------------------------------------------------------------------------------
# Lote oficial del reto, leído del almacén
# ---------------------------------------------------------------------------------------------
def generar_lote_reto(p: dict, fecha: str | None = None, carpeta: Path | None = None) -> dict:
    """Las 11 fichas de la muestra oficial desde el esquema estrella, con el validador oficial."""
    pf = etl.params_ficha(p)
    corrida = etl.ejecutar(p, "reto")
    con = esquema.conectar(p["rutas"]["almacen"])
    try:
        fichas = desde_almacen.fichas_reto(con, p, pf, fecha)
    finally:
        con.close()
    salida = Path(carpeta or pf["rutas"]["salida_dir"])
    (salida / "fichas").mkdir(parents=True, exist_ok=True)
    (salida / "fichas_md").mkdir(parents=True, exist_ok=True)
    fichas = [json.loads(_texto_json(f)) for f in fichas]
    for f in fichas:
        (salida / "fichas" / f"{f['id_inmueble']}.json").write_text(_texto_json(f), encoding="utf-8", newline="\n")
        (salida / "fichas_md" / f"{f['id_inmueble']}.md").write_text(renderizar_markdown(f), encoding="utf-8", newline="\n")
    validacion = None
    if pf["rutas"]["esperado_json"].exists() and pf["rutas"]["validador_py"].exists():
        validacion = validar_contra_esperado(fichas, pf["rutas"]["esperado_json"], pf["rutas"]["validador_py"])
        validacion.to_csv(salida / "validacion.csv", index=False, encoding="utf-8", lineterminator="\n")
    return {"fichas": fichas, "validacion": validacion, "carpeta": salida, "corrida": corrida}


def main(argv: list[str] | None = None) -> int:
    from captura import config
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    orden = argv[0] if argv else "reto"
    p = config.cargar()
    bd.iniciar(p)
    if orden == "reto":
        res = generar_lote_reto(p)
        print(f"Fichas de la muestra oficial leídas del almacén: {len(res['fichas'])} en {res['carpeta']}")
        for f in res["fichas"]:
            pm = f["mercado"]["precio_m2"]
            print(f"  {f['id_inmueble']}  {f['inmueble']['estado']:<14} n={pm['n']:>2}  mediana {pm['mediana']:>9,}  "
                  f"confianza {f['mercado']['confianza']['nivel']}")
        if res["validacion"] is not None:
            ok = int(res["validacion"]["ok"].sum())
            print(f"Validador oficial contra el cálculo manual: {ok}/{len(res['validacion'])}")
            return 0 if ok == len(res["validacion"]) else 1
        return 0
    if orden == "pendientes":
        hechas = generar_pendientes(p)
        for h in hechas:
            print(f"  {h['cotizacion_id'][:8]}  {h['estado']}  {h.get('ruta_md') or h.get('mensaje')}")
        print(f"Fichas generadas: {sum(1 for h in hechas if h.get('ficha_id'))} de {len(hechas)} cotizaciones pendientes")
        return 0
    print("Uso: python -m ficha.servicio [reto|pendientes]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
