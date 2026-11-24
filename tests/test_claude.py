"""Curaduría con Claude: qué se le envía, qué se acepta, qué se rechaza y qué queda en la bitácora."""
from __future__ import annotations

import json
import os
import stat
import sqlite3

import pytest
from test_flujo import capturar

from captura import consultas
from captura.curaduria import claude_cliente, claude_tareas

PAR = "catastro:toponimia:0:502|osm:overpass:node/8"


def respuesta_tipica(lote: dict) -> dict:
    ids = {x["id"] for x in lote["items"]}
    props = [
        {"id": "geomedellin:equipamientos:1:13", "nombre": "Centro Raro", "categoria": "cultura",
         "subtipo": "Centro Cultural", "confianza": "media", "motivo": "por el nombre"},
        {"id": "geomedellin:bibliotecas:0:61", "nombre": "Biblioteca Pública Piloto", "categoria": "cultura",
         "subtipo": "biblioteca", "confianza": "alta", "motivo": "tilde"},
        {"id": "geomedellin:equipamientos:1:12", "nombre": "Institución Educativa INEM José Félix de Restrepo",
         "categoria": "salud", "subtipo": "colegio", "confianza": "alta", "motivo": "prueba de rechazo"},
        {"id": "catastro:toponimia:0:503", "nombre": "Hospital General de Medellín", "categoria": "salud",
         "subtipo": "hospital", "confianza": "alta", "motivo": "nombre inventado"},
        {"id": "no:existe:1", "nombre": "X", "categoria": "salud", "subtipo": "x", "confianza": "alta", "motivo": ""},
    ]
    return {"items": [x for x in props if x["id"] in ids or x["id"] == "no:existe:1"],
            "duplicados": [{"par": d["par"], "mismo_lugar": True, "motivo": "mismo colegio"} for d in lote["duplicados"]],
            "conjunto": {"candidato": "osm:area:3600001", "nombre": "Urbanización Bolivariana", "motivo": "contiene el punto"},
            "observaciones": ["Hay un equipamiento con tipo desconocido.", "Un parque de OSM no trae nombre."]}


def test_curaduria_con_claude_valida_y_registra(p, mundo, claude_falso):
    claude_falso["respuesta"]["fn"] = respuesta_tipica
    cid, prog = capturar(p, curar_con_claude=True)
    assert prog.pasos["claude"]["estado"] == "ok" and "cambios aceptados" in prog.pasos["claude"]["detalle"]

    # --- qué se envió ---
    lote = claude_falso["recibidos"][0]["lote"]
    enviados = {x["id"] for x in lote["items"]}
    assert enviados and not any(x.startswith(("google:", "registro_ic:")) for x in enviados)
    assert not any(":camaras_cctv:" in x or ":paradas_bus:" in x for x in enviados)
    dudosos = {"geomedellin:equipamientos:1:13", "catastro:toponimia:0:503", "osm:overpass:node/3"}
    assert {x["id"] for x in lote["items"][:3]} == dudosos                      # lo dudoso va primero
    assert [d["par"] for d in lote["duplicados"]] == [PAR]
    assert {c["id"] for c in lote["conjunto"]["candidatos"]} >= {"catastro:toponimia:501", "osm:area:3600001"}
    assert "DATOS" in claude_falso["recibidos"][0]["sistema"] and "inversión" in claude_falso["recibidos"][0]["sistema"]
    esquema = claude_falso["recibidos"][0]["esquema"]
    assert esquema["additionalProperties"] is False
    assert set(esquema["properties"]["items"]["items"]["properties"]["categoria"]["enum"]) == set(p["taxonomia"]["categorias"])
    assert "phone" not in json.dumps(lote) and "telefono" not in json.dumps(lote)

    # --- qué se aplicó ---
    c = sqlite3.connect(p["rutas"]["bd"]); c.row_factory = sqlite3.Row
    it = {x["id_externo"]: dict(x) for x in c.execute("SELECT * FROM entorno_items")}
    raro = it["1:13"]
    assert raro["categoria"] == "cultura" and raro["subtipo"] == "centro_cultural" and raro["curado_por"] == "claude"
    assert "ajustado_por_ia" in raro["banderas_json"] and "clasificacion_dudosa" not in raro["banderas_json"]
    assert it["0:61"]["nombre"] == "Biblioteca Pública Piloto" and it["0:61"]["nombre_original"] == "biblioteca publica piloto"
    inem = it["1:12"]
    assert inem["categoria"] == "educacion"                                   # regla con confianza alta: no se cambia
    assert inem["nombre"] == "Institución Educativa INEM José Félix de Restrepo"   # la tilde sí
    assert it["0:503"]["nombre"] == "Sitio Raro"                              # nombre inventado: rechazado
    assert it["0:503"]["categoria"] == "salud"                                # la regla era dudosa: categoría sí
    assert it["0:502"]["entidad_id"] == it["node/8"]["entidad_id"]            # par resuelto como mismo lugar
    assert sorted([it["0:502"]["es_maestro"], it["node/8"]["es_maestro"]]) == [0, 1]
    rev = dict(c.execute("SELECT * FROM cola_revision").fetchone())
    assert rev["resuelto_por"] == "claude" and rev["decision"] == "mismo_lugar"

    log = [dict(x) for x in c.execute("SELECT * FROM curaduria_log WHERE origen='claude'")]
    reglas = {(x["item_ref"], x["campo"]): (x["regla"], x["aceptado"]) for x in log}
    assert reglas[("geomedellin:equipamientos:1:13", "categoria")] == ("aceptado:regla_dudosa", 1)
    assert reglas[("geomedellin:equipamientos:1:12", "categoria")] == ("rechazado:regla_con_confianza_alta", 0)
    assert reglas[("catastro:toponimia:0:503", "nombre")][0].startswith("rechazado:similitud_") and not reglas[("catastro:toponimia:0:503", "nombre")][1]
    assert reglas[("no:existe:1", "id")] == ("rechazado:id_desconocido", 0)
    assert reglas[(PAR, "duplicado")] == ("aceptado", 1)
    assert all(x["modelo"] == "falso" for x in log)

    # --- el nombre oficial del mismo lote no lo cambia Claude ---
    r = consultas.leer_captura(p, cid)
    assert r["conjunto"]["nombre"] == "Edificio Los Almendros" and r["conjunto"]["origen"] == "catastro"
    cur = r["curaduria"]
    assert cur["proveedor"] == "prueba" and cur["modelo"] == "falso" and len(cur["nota"]) == 2
    assert cur["cambios_claude"] >= 5 and cur["rechazados_claude"] == 3 and cur["en_revision"] == 0
    assert cur["enlazados"] == 4 and cur["por_claude"] >= 3
    assert r["indicadores"]["conteo_educacion_1000m"]["valor"] == 3           # el par fusionado ya cuenta una vez
    assert r["indicadores"]["conteo_cultura_1000m"]["valor"] == 2


def test_claude_elige_conjunto_solo_si_no_hay_nombre_oficial_del_lote(p, mundo, claude_falso, monkeypatch):
    claude_falso["respuesta"]["fn"] = respuesta_tipica
    original = mundo.arcgis

    def sin_toponimia(clave, q):
        return {"features": []} if clave.endswith("ConsultaOperadorCatastral/0") else original(clave, q)
    monkeypatch.setattr(mundo, "arcgis", sin_toponimia)
    cid, _ = capturar(p, curar_con_claude=True)
    r = consultas.leer_captura(p, cid)
    assert r["conjunto"] == {"nombre": "Urbanización Bolivariana", "tipo": "urbanizacion", "origen": "osm"}


def test_si_claude_falla_la_captura_queda_por_reglas_y_lo_dice(p, mundo, claude_falso, monkeypatch):
    def falla(*a, **k):
        raise claude_cliente.ClaudeNoDisponible("sesión vencida")
    monkeypatch.setattr(claude_cliente, "invocar", falla)
    cid, prog = capturar(p, curar_con_claude=True)
    r = consultas.leer_captura(p, cid)
    assert r["estado"] == "ok" and prog.pasos["claude"]["estado"] == "error"
    assert any("solo por reglas" in a for a in r["avisos"]) and r["curaduria"]["por_claude"] == 0
    assert r["curaduria"]["en_revision"] == 1 and r["curaduria"]["proveedor"] is None


def test_respuesta_malformada_no_rompe(p, mundo, claude_falso):
    claude_falso["respuesta"]["fn"] = lambda lote: {"items": "no es lista", "duplicados": [{"par": "x"}], "conjunto": None}
    cid, prog = capturar(p, curar_con_claude=True)
    assert consultas.leer_captura(p, cid)["estado"] == "ok" and prog.pasos["claude"]["estado"] in ("ok", "parcial", "error")


def test_sin_proveedor_se_omite_y_se_declara(p, mundo):
    cid, prog = capturar(p, curar_con_claude=True)        # proveedor "ninguno" en la configuración de prueba
    assert prog.pasos["claude"]["estado"] == "omitido" and "desactivado" in prog.pasos["claude"]["detalle"]


def test_extraer_json_y_deteccion(p):
    assert claude_cliente.extraer_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(claude_cliente.ClaudeNoDisponible):
        claude_cliente.extraer_json("sin llaves")
    p["params"]["claude"].update(proveedor="claude_code", ejecutable="no-existe-este-programa")
    det = claude_cliente.detectar(p)
    assert det["disponible"] is False and "Claude Code" in det["detalle"]
    assert claude_tareas.armar_lotes([], [], [], {}, p) == []


@pytest.mark.skipif(os.name == "nt", reason="el ejecutable simulado es un script de shell")
def test_invocacion_por_claude_code_arma_bien_el_comando(p, tmp_path):
    (tmp_path / "bin").mkdir()
    falso = tmp_path / "bin" / "claude"
    registro = tmp_path / "args.json"
    falso.write_text(f"""#!/usr/bin/env python3
import json, sys
json.dump({{"args": sys.argv[1:], "stdin": sys.stdin.read()}}, open({str(registro)!r}, "w"))
print(json.dumps({{"type": "result", "is_error": False, "result": "", "total_cost_usd": 0.002,
                  "structured_output": {{"items": [], "duplicados": [], "conjunto": {{"candidato": "", "nombre": "", "motivo": ""}}, "observaciones": ["ok"]}}}}))
""", encoding="utf-8")
    falso.chmod(falso.stat().st_mode | stat.S_IEXEC)
    p["params"]["claude"].update(proveedor="claude_code", ejecutable=str(falso))
    esq = claude_tareas.esquema(list(p["taxonomia"]["categorias"]))
    r = claude_cliente.invocar(p, claude_tareas.SISTEMA, '{"items": []}', esq)
    assert r["datos"]["observaciones"] == ["ok"] and r["proveedor"] == "claude_code" and r["costo_usd"] == 0.002
    visto = json.loads(registro.read_text())
    a = visto["args"]
    assert a[0] == "-p" and visto["stdin"] == '{"items": []}'
    for bandera in ("--output-format", "--json-schema", "--system-prompt-file", "--no-session-persistence", "--safe-mode"):
        assert bandera in a
    assert a[a.index("--tools") + 1] == "" and a[a.index("--model") + 1] == "haiku"
    assert json.loads(a[a.index("--json-schema") + 1])["required"] == ["items", "duplicados", "conjunto", "observaciones"]
    assert "--bare" not in a                                # --bare exige llave de API; aquí se usa la sesión del plan
