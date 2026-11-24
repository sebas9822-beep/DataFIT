"""Descargador de la capa POT, con un servicio ArcGIS simulado (sin red)."""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
import urllib.parse

import pytest

from ficha.config import RAIZ, cargar_params

_spec = importlib.util.spec_from_file_location("descargar_pot", RAIZ / "scripts" / "descargar_pot.py")
descargar_pot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(descargar_pot)

INFO = {"name": "Tratamiento POT vigente", "geometryType": "esriGeometryPolygon",
        "extent": {"spatialReference": {"wkid": 9377, "latestWkid": 9377}},
        "fields": [{"name": n} for n in ("objectid", "codigo_tratamiento", "tratamiento", "categoria_tratamiento",
                                          "clase_suelo", "tipo", "zona")]}


def _feature(oid):
    return {"type": "Feature", "id": oid, "properties": {"objectid": oid, "tratamiento": "Consolidación Nivel 2"},
            "geometry": {"type": "Polygon", "coordinates": [[[-75.6, 6.24], [-75.58, 6.24], [-75.58, 6.26], [-75.6, 6.24]]]}}


class Servicio:
    """Responde como el MapServer: info, ids y páginas en GeoJSON. Puede fallar las primeras N veces."""

    def __init__(self, n_objetos=5, fallos_503=0, error_400=False, perder=0):
        self.n, self.fallos, self.error_400, self.perder = n_objetos, fallos_503, error_400, perder
        self.peticiones: list[dict] = []

    def __call__(self, req, timeout=None):
        datos = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else {}
        self.peticiones.append({"url": req.full_url, **datos})
        if self.fallos:
            self.fallos -= 1
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)
        if self.error_400:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
        if req.full_url.endswith("?f=json"):
            cuerpo = INFO
        elif datos.get("returnIdsOnly") == "true":
            cuerpo = {"objectIdFieldName": "objectid", "objectIds": list(range(self.n, 0, -1))}
        else:
            ids = [int(x) for x in datos["objectIds"].split(",")]
            cuerpo = {"type": "FeatureCollection", "features": [_feature(i) for i in ids[self.perder:]]}
        return io.BytesIO(json.dumps(cuerpo).encode())


@pytest.fixture()
def cfg():
    c = dict(cargar_params()["pot_descarga"])
    c.update(lote=2, espera_s=0, intentos=3)
    return c


def test_descarga_por_lotes_en_wgs84_con_metadatos(cfg):
    servicio = Servicio(n_objetos=5)
    capa = descargar_pot.descargar(cfg, abrir=servicio)
    assert [f["id"] for f in capa["features"]] == [1, 2, 3, 4, 5]
    paginas = [p for p in servicio.peticiones if "objectIds" in p]
    assert [p["objectIds"] for p in paginas] == ["1,2", "3,4", "5"]
    assert all(p["outSR"] == "4326" and p["f"] == "geojson" for p in paginas)
    m = capa["metadata"]
    assert m["registros"] == 5 and m["servicio"].endswith("MapServer/7") and m["crs"].startswith("EPSG:4326")


def test_reintenta_ante_503(cfg):
    capa = descargar_pot.descargar(cfg, abrir=Servicio(n_objetos=3, fallos_503=2))
    assert capa["metadata"]["registros"] == 3


def test_falla_tras_agotar_los_intentos(cfg):
    with pytest.raises(RuntimeError, match="no respondió tras 3 intentos"):
        descargar_pot.descargar(cfg, abrir=Servicio(fallos_503=10))


def test_error_4xx_no_se_reintenta(cfg):
    servicio = Servicio(error_400=True)
    with pytest.raises(descargar_pot.ErrorServicio):
        descargar_pot.descargar(cfg, abrir=servicio)
    assert len(servicio.peticiones) == 1


def test_descarga_incompleta_se_rechaza(cfg):
    with pytest.raises(RuntimeError, match="incompleta"):
        descargar_pot.descargar(cfg, abrir=Servicio(n_objetos=4, perder=1))
