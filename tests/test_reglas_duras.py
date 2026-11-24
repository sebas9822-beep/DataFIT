"""Reglas duras del proyecto, revisadas sobre el código y sobre una captura real de prueba.

1. Ningún número de negocio en el código (viven en config/reglas.yaml).
2. Nada se borra en silencio.
3. Precio pedido y de cierre nunca se mezclan.
4. La aplicación describe: no recomienda ni dictamina.
5. Los secretos no llegan al navegador ni a la base; los datos personales no se guardan.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from test_flujo import capturar

from captura import bd, consultas
from captura.curaduria import claude_tareas

RAIZ = Path(__file__).resolve().parents[1]
# Los cuatro paquetes de la aplicación única: captura (M1), ficha (M2), almacén y gestión.
FUENTE = sorted(f for paquete in ("captura", "ficha", "almacen", "gestion") for f in (RAIZ / "src" / paquete).rglob("*.py"))
WEB = (RAIZ / "web" / "index.html").read_text(encoding="utf-8")

# Lenguaje que convertiría la captura en una recomendación o en un dictamen.
PROHIBIDO = re.compile(
    r"\b(recomendamos|se recomienda (comprar|invertir|adquirir)|conviene (comprar|invertir)|buena inversi[oó]n|"
    r"mala inversi[oó]n|oportunidad de (compra|inversi[oó]n)|comprar ya|rentabilidad (esperada|estimada|proyectada)|"
    r"viable jur[ií]dicamente|uso permitido|es apto para|puntaje de inversi[oó]n|atractivo de inversi[oó]n)\b", re.I)


def test_la_interfaz_y_el_codigo_no_recomiendan():
    textos = {str(f.relative_to(RAIZ)): f.read_text(encoding="utf-8") for f in FUENTE}
    textos["web/index.html"] = WEB
    hallazgos = {n: PROHIBIDO.findall(t) for n, t in textos.items() if PROHIBIDO.search(t)}
    assert not hallazgos, hallazgos
    assert "no vale como certificado catastral ni como concepto urbanístico" in WEB
    assert "precio pedido, no de cierre" in WEB and "no es precio de mercado" in WEB
    # Claude recibe la misma instrucción de alcance
    assert "no opines sobre el inmueble ni sobre su conveniencia como inversión" in claude_tareas.SISTEMA


def test_ningun_numero_de_negocio_en_el_codigo(p):
    """Umbrales, pesos, anillos y topes de reglas.yaml no aparecen escritos en los módulos.

    (El 1000 no se revisa: también es la conversión de metros a kilómetros.)
    """
    r = p["reglas"]
    d, ia = r["deduplicacion"], r["curaduria_ia"]
    sensibles = {str(x) for x in (d["umbral_auto"], d["umbral_revision"], d["peso_nombre"], d["peso_distancia"],
                                  d["sin_nombre"]["puntaje"], d["similitud_minima_nombre"], ia["max_items"],
                                  r["indicadores"]["cobertura_basica_m"], *r["captura"]["anillos_m"][:-1],
                                  r["google"]["traslape_subcirculo"], *r["ofertas"]["area_valida_m2"])}
    for f in FUENTE:
        if f.name in ("api.py", "base.py"):        # ahí los 4xx/5xx son códigos HTTP, no reglas
            continue
        codigo = "\n".join(linea.split("#")[0] for linea in f.read_text(encoding="utf-8").splitlines()
                           if not linea.lstrip().startswith(('"""', "#")))
        codigo = re.sub(r'""".*?"""', "", codigo, flags=re.S)
        codigo = re.sub(r"\[:\d+\]", "", codigo)   # recortes de texto para mensajes: no son reglas
        for n in sensibles:
            if len(n) < 3:
                continue
            assert not re.search(rf"(?<![\w.]){re.escape(n)}(?![\w.])", codigo), f"{f.name}: aparece {n} en el código"


def test_nada_se_borra_en_silencio_y_los_precios_no_se_mezclan(p, mundo):
    cid, _ = capturar(p)
    c = sqlite3.connect(p["rutas"]["bd"]); c.row_factory = sqlite3.Row
    # lo que cada capa entregó dentro del radio está en la base (aceptado o con motivo)
    for e in c.execute("SELECT fuente, capa, registros FROM ejecuciones WHERE captura_id=? AND estado='ok' "
                       "AND capa NOT LIKE 'punto_%'", (cid,)):
        tabla = "ofertas" if e["capa"] == "oferta_oime" else "entorno_items"
        n = c.execute(f"SELECT COUNT(*) FROM {tabla} WHERE captura_id=? AND fuente=? AND capa=?",
                      (cid, e["fuente"], e["capa"])).fetchone()[0]
        assert n == e["registros"], (e["fuente"], e["capa"], n, e["registros"])
    # todo no-maestro dice con quién coincide, con qué regla y con qué puntaje
    for it in c.execute("SELECT * FROM entorno_items WHERE captura_id=? AND es_maestro=0", (cid,)):
        assert it["coincide_con"] and it["regla_dup"] and it["entidad_id"]
    # la vista curada no pierde entidades: una por lugar
    total = c.execute("SELECT COUNT(DISTINCT entidad_id) FROM entorno_items WHERE captura_id=? AND motivo_exclusion IS NULL", (cid,)).fetchone()[0]
    vista = c.execute("SELECT COUNT(*) FROM v_entorno_curado WHERE captura_id=?", (cid,)).fetchone()[0]
    assert vista == total
    # precios: una sola clase por fila y por indicador
    assert {r[0] for r in c.execute("SELECT DISTINCT tipo_precio FROM ofertas WHERE captura_id=?", (cid,))} == {"pedido"}
    for r in c.execute("SELECT indicador, detalle_json FROM indicadores WHERE captura_id=? AND indicador LIKE 'oferta_%mediana%'", (cid,)):
        assert json.loads(r["detalle_json"])["tipo_precio"] == "pedido"
    # excluidos: siempre con motivo, y siguen en la base
    excl = c.execute("SELECT COUNT(*) FROM ofertas WHERE captura_id=? AND motivo_exclusion IS NOT NULL", (cid,)).fetchone()[0]
    assert excl == consultas.leer_captura(p, cid)["ofertas"]["excluidas"]


def test_sin_datos_personales_ni_secretos_en_la_base(p, mundo):
    p["secretos"]["google_servidor"] = "LLAVE-QUE-NO-DEBE-GUARDARSE"
    cid, _ = capturar(p, fuentes=["geomedellin", "catastro", "registro_ic", "oime", "osm"])
    c = sqlite3.connect(p["rutas"]["bd"])
    volcado = "\n".join(c.iterdump())
    assert "LLAVE-QUE-NO-DEBE-GUARDARSE" not in volcado
    for campo in p["reglas"]["datos_personales"]["campos_prohibidos"]:
        assert f'"{campo}"' not in volcado, campo
    assert not re.search(r"\b3\d{9}\b", volcado)                       # celulares colombianos
    assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", volcado.replace(p["user_agent"], ""))


def test_la_pagina_no_trae_llaves_y_escapa_lo_que_pinta():
    assert not re.search(r"AIza[0-9A-Za-z_-]{20,}|sk-ant-[A-Za-z0-9_-]{10,}", WEB)
    assert "GOOGLE_SERVER_KEY" not in WEB
    # Toda interpolación de datos en HTML pasa por esc() o num()/metros(): no hay ${x.nombre} crudo.
    crudos = re.findall(r"\$\{(?:it|x|c|e|m|pr|p|r)\.(nombre|mensaje|direccion\w*|valor_\w+|detalle|etiqueta|barrio_nombre)\}", WEB)
    assert not crudos, crudos


def test_el_esquema_declara_lo_que_exige_la_trazabilidad(p):
    bd.iniciar(p)
    c = sqlite3.connect(p["rutas"]["bd"])
    cols = lambda t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
    assert {"motivo_exclusion", "regla_dup", "puntaje_dup", "coincide_con", "entidad_id", "es_maestro",
            "nombre_original", "licencia", "expira_en", "obtenido_en", "curado_por"} <= cols("entorno_items")
    assert {"tipo_precio", "motivo_exclusion", "licencia"} <= cols("ofertas")
    assert {"valor_antes", "valor_despues", "regla", "origen", "aceptado", "modelo"} <= cols("curaduria_log")
    assert {"huella_config", "version_app", "origen_coordenada", "precision_geo", "coord_expira_en"} <= cols("capturas")
    assert not ({"phone", "telefono", "agency", "email"} & cols("ofertas"))
