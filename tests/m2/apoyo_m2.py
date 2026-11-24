"""Apoyo de las pruebas del lote oficial (Momento 2, sobre archivos y capas GeoJSON).

Proyecto mínimo en una carpeta temporal, con el esquema oficial y resultados calculables a mano.

Zona z1 (Medellín). Inmueble T-01: 100 m², estado "OK" (habitable), su anuncio es fr:100.
Comparables de T-01 (habitable, área 70–130 m², válidos, sin su propio grupo):
  fila 2 · 8,0 M/m² pedido · fila 5 · 8,5 M/m² pedido · fila 6 · 9,0 M/m² pedido · fila 7 · 7,5 M/m² cierre
=> pedido n=3, mediana 8.500.000, desviación 500.000, CV 0,0588 (media); cierre n=1 (no alcanza 3).
Las demás filas cubren cada regla: duplicados R1 y R2, grupo ambiguo, válido absorbido por un
representante inválido y los cinco motivos oficiales de invalidez.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from ficha.config import RAIZ

LAT, LON = 6.2537, -75.5868

COLUMNAS = "id_anuncio,fuente,fecha,zona,municipio,barrio,area_m2,precio,habitaciones,banos,parqueadero,estado,tipo_precio,origen"
ANUNCIOS = [
    # fila CSV 2..: comentario
    "fr:1,fincaraiz,,z1,medellin,b,100,800000000,3,2,1,OK,pedido,h:1",                # válido 8,0 M
    ",facebook,,z1,medellin,b,100,800000000,3,2,,Ok,pedido,h:2",                      # R2 de h:1
    "fr:1,fincaraiz,,z1,medellin,b,101,810000000,3,2,1,OK,pedido,h:3",                # R1 de h:1
    "fr:2,fincaraiz,,z1,medellin,b,90,765000000,3,2,1,vivir,pedido,h:4",              # válido 8,5 M
    "fr:3,fincaraiz,,z1,medellin,b,110,990000000,3,2,1,HABITABLE POR REMODELAR,pedido,h:5",  # 9,0 M
    "fr:4,fincaraiz,,z1,medellin,b,95,712500000,3,2,1,OK,cierre,h:6",                 # válido 7,5 M cierre
    ",aviso_calle,,z1,medellin,b,105,,3,2,,OK,pedido,h:7",                            # sin_precio
    "fr:5,fincaraiz,,z1,medellin,b,,500000000,3,2,1,OK,pedido,h:8",                   # sin_area
    "fr:6,fincaraiz,,z1,medellin,b,100,820000000,3,2,1,,pedido,h:9",                  # estado_sin_clasificar
    "fr:7,fincaraiz,,z1,medellin,b,100,50000000,3,2,1,OK,pedido,h:10",                # precio_m2_fuera_de_rango
    "fr:8,fincaraiz,,z1,medellin,b,100,830000000,3,2,1,OK,arriendo,h:11",             # tipo_precio_no_aceptado
    "fr:100,fincaraiz,,z1,medellin,b,100,900000000,3,2,1,OK,pedido,h:12",             # anuncio del propio T-01
    "fr:9,fincaraiz,,z2,medellin,b,100,700000000,3,2,1,OK,pedido,h:13",               # otra zona
    "fr:10,fincaraiz,,z1,medellin,b,100,950000000,3,2,1,Remodelado,pedido,h:14",      # otro estado
    "fr:11,fincaraiz,,z1,medellin,b,140,1120000000,3,2,1,OK,pedido,h:15",             # 8,0 M; fuera de 70–130
    "fr:12,metrocuadrado,,z1,medellin,b,100,800000000,3,2,1,OK,pedido,h:16",          # R2 de h:1, otro id: ambiguo
    ",facebook,,z1,medellin,b,100,820000000,3,2,,OK,pedido,h:17",                     # R2 de h:9 (inválido)
]

INMUEBLES = f"""id_inmueble,id_anuncio,origen,direccion,zona,municipio,barrio,area_m2,estado,lat,lon,precision_coordenadas,fuente_coordenadas
T-01,fr:100,h:12,Calle 1 # 1-1,z1,medellin,b,100,OK,{LAT},{LON},cruce,prueba
T-02,,h:x2,,z1,medellin,b,150,OK,{LAT},{LON},barrio,prueba
T-03,,h:x3,,z1,medellin,b,100,OK,6.3000,{LON},barrio,prueba
T-04,,h:x4,,z1,itagui,b,100,OK,{LAT},{LON},barrio,prueba
T-05,,h:x5,,z1,medellin,b,100,OK,60.0,{LON},barrio,prueba
"""


def _punto(lon, lat, props):
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _coleccion(features, **extra):
    return json.dumps({"type": "FeatureCollection", **extra, "features": features}, ensure_ascii=False)


@pytest.fixture()
def proyecto(tmp_path: Path) -> Path:
    """Proyecto temporal con los params.yaml y parametros.yaml reales del repositorio."""
    (tmp_path / "config").mkdir()
    shutil.copy(RAIZ / "config" / "ficha.yaml", tmp_path / "config" / "params.yaml")      # en la aplicación única se llama ficha.yaml
    shutil.copy(RAIZ / "config" / "parametros.yaml", tmp_path / "config" / "parametros.yaml")
    editar_params(tmp_path, lambda p: p["ejecucion"].update(fecha_generacion="2026-11-14"))
    editar_params(tmp_path, lambda p: p["rutas"].update(salida_dir="salidas", auditoria_dir="auditoria"))
    datos = tmp_path / "reto_oficial" / "data"
    (datos / "geo").mkdir(parents=True)
    (datos / "anuncios_muestra.csv").write_text("\n".join([COLUMNAS, *ANUNCIOS]) + "\n", encoding="utf-8")
    (datos / "inmuebles_prueba.csv").write_text(INMUEBLES, encoding="utf-8")
    meta = {"metadata": {"fecha_osm": "2026-11-13T23:03:06Z"}}
    (datos / "geo" / "transporte.geojson").write_text(_coleccion([
        _punto(LON - 0.001, LAT, {"nombre": "Estadio", "sistema": "metro"}),        # ≈ 111 m
        _punto(LON - 0.006, LAT, {"nombre": "Tranvía", "sistema": "tranvia"}),      # ≈ 663 m
        _punto(LON, LAT, {"nombre": "Excluida", "sistema": "metroplus"}),           # sistema no incluido
    ], **meta), encoding="utf-8")
    (datos / "geo" / "equipamientos.geojson").write_text(_coleccion([
        _punto(LON + 0.002, LAT, {"nombre": "Parque", "categoria": "parques"}),     # ≈ 221 m
        _punto(LON + 0.004, LAT, {"nombre": "Clínica", "categoria": "salud"}),      # ≈ 442 m
        _punto(LON + 0.008, LAT, {"nombre": "Colegio", "categoria": "educacion"}),  # ≈ 884 m, fuera
    ], **meta), encoding="utf-8")
    return tmp_path


def ruta_params(raiz: Path) -> Path:
    return raiz / "config" / "params.yaml"


def editar_params(raiz: Path, cambios) -> None:
    """Aplica `cambios(params)` sobre el params.yaml del proyecto temporal."""
    ruta = ruta_params(raiz)
    params = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    cambios(params)
    ruta.write_text(yaml.safe_dump(params, allow_unicode=True, sort_keys=False), encoding="utf-8")


def escribir_pot(raiz: Path, propiedades: dict | None = None) -> Path:
    """Capa POT (rutas.pot_geojson) con un polígono que cubre el punto de T-01 y no el de T-03."""
    ruta = raiz / "data" / "geo" / "pot_tratamientos.geojson"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(_coleccion([{
        "type": "Feature", "properties": propiedades or {"TRATAMIENTO": "CN2"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-75.60, 6.24], [-75.58, 6.24], [-75.58, 6.26], [-75.60, 6.26], [-75.60, 6.24]]]},
    }]), encoding="utf-8")
    return ruta


def crear_proyecto_real(tmp_path: Path) -> Path:
    """params.yaml que lee los datos oficiales del repositorio y escribe las salidas en una carpeta temporal."""
    params = yaml.safe_load((RAIZ / "config" / "ficha.yaml").read_text(encoding="utf-8"))
    params["rutas"].update(salida_dir="salidas", auditoria_dir="auditoria")
    params["reglas_negocio"] = str(RAIZ / params["reglas_negocio"])
    for k, v in params["rutas"].items():
        params["rutas"][k] = str(tmp_path / v) if k in ("salida_dir", "auditoria_dir") else str(RAIZ / v)
    params["rutas"]["pot_geojson"] = str(tmp_path / "sin_capa_pot.geojson")   # condiciones del cálculo manual: sin POT
    params["ejecucion"]["fecha_generacion"] = "2026-11-14"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "params.yaml").write_text(yaml.safe_dump(params, allow_unicode=True, sort_keys=False),
                                                      encoding="utf-8")
    return tmp_path
