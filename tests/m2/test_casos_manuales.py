"""Los datos oficiales del reto: cálculo manual, referencia oficial, esquema y notebook de Colab.

- Validador oficial (validacion/validar.py) contra el cálculo manual de INM-04, INM-06 e INM-09.
- Las 11 fichas idénticas a la implementación de referencia en todos los campos oficiales.
- La ficha contiene todos los campos de ejemplos/ficha_ejemplo.json.
- La solución del notebook de Colab produce las mismas 11 fichas.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from m2.apoyo_m2 import crear_proyecto_real, ruta_params

from ficha.config import RAIZ
from ficha.pipeline import ejecutar

REFERENCIA = RAIZ / "reto_oficial"


def _modulo(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def oficial(tmp_path_factory):
    raiz = crear_proyecto_real(tmp_path_factory.mktemp("real"))
    return ejecutar(ruta_params(raiz), raiz)


def test_validador_oficial_3_de_3(oficial):
    val = oficial["validacion"]
    assert val is not None and list(val["id_inmueble"]) == ["INM-04", "INM-06", "INM-09"]
    assert val["ok"].all(), val.to_string()


def test_el_validador_detecta_una_mediana_equivocada(oficial):
    validar = _modulo("validar_oficial", REFERENCIA / "validacion" / "validar.py")
    esperado = json.loads((REFERENCIA / "validacion" / "esperado.json").read_text(encoding="utf-8"))
    ficha = copy.deepcopy(next(f for f in oficial["fichas"] if f["id_inmueble"] == "INM-04"))
    ficha["mercado"]["precio_m2"]["mediana"] += 10
    errores = validar.validar(ficha, esperado["inmuebles"]["INM-04"], esperado["_tolerancias"])
    assert len(errores) == 1 and "mediana" in errores[0]


def test_11_fichas_identicas_a_la_referencia(oficial):
    comparador = _modulo("comparar", RAIZ / "scripts" / "comparar_con_referencia.py")
    referencia = comparador.fichas_referencia()
    nuestras = {f["id_inmueble"]: json.loads(json.dumps(f, default=str)) for f in oficial["fichas"]}
    assert set(referencia) == set(nuestras) and len(nuestras) == 11
    diferencias = {k: comparador.diferencias(ref, nuestras[k]) for k, ref in referencia.items()}
    assert not any(diferencias.values()), diferencias


def _claves(obj, ruta=""):
    if isinstance(obj, dict):
        return {f"{ruta}.{k}".lstrip(".") for k in obj} | {c for k, v in obj.items() for c in _claves(v, f"{ruta}.{k}")}
    if isinstance(obj, list) and obj:
        return _claves(obj[0], f"{ruta}[]")
    return set()


def test_ficha_contiene_todos_los_campos_del_ejemplo_oficial(oficial):
    ejemplo = json.loads((REFERENCIA / "ejemplos" / "ficha_ejemplo.json").read_text(encoding="utf-8"))
    ficha = json.loads(json.dumps(next(f for f in oficial["fichas"] if f["id_inmueble"] == "INM-04"), default=str))
    assert _claves(ejemplo) <= _claves(ficha), sorted(_claves(ejemplo) - _claves(ficha))


def test_resumen_de_los_datos_oficiales(oficial):
    assert oficial["resumen"] == {"anuncios_entrada": 117, "duplicados": 33, "invalidos": 21, "validos": 63}
    assert all(f["norma"]["estado"] in {"pendiente_capa", "no_disponible"} for f in oficial["fichas"])


def test_notebook_de_colab_produce_las_mismas_fichas(oficial, tmp_path):
    shutil.copytree(REFERENCIA, tmp_path / "reto_oficial", ignore=shutil.ignore_patterns("salidas"))
    entorno = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}     # consola de Windows en cp1252
    proceso = subprocess.run([sys.executable, str(RAIZ / "notebooks" / "reto_ficha_colab_solucion.py")], cwd=tmp_path,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", env=entorno)
    assert proceso.returncode == 0, proceso.stderr[-2000:]
    assert "Coincide con el cálculo manual" in proceso.stdout
    comparador = _modulo("comparar_nb", RAIZ / "scripts" / "comparar_con_referencia.py")
    for f in oficial["fichas"]:
        nb = json.loads((tmp_path / "reto_oficial" / "salidas" / "participante" / f"{f['id_inmueble']}.json").read_text(encoding="utf-8"))
        nuestra = json.loads(json.dumps(f, default=str))
        assert comparador.diferencias(nb, nuestra) == [], f["id_inmueble"]
