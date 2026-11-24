"""Utilidades geográficas.

Las coordenadas de entrada están en EPSG:4326. La distancia de la ficha se mide con haversine
(decisión de Venture One del 14-nov-2026) y EPSG:9377 (MAGNA-SIRGAS Origen Nacional, el sistema
del catastro) queda como control y como coordenada métrica guardada para la fase siguiente.
"""
from __future__ import annotations

import math
from functools import lru_cache

RADIO_TIERRA_M = 6_371_008.8


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * RADIO_TIERRA_M * math.asin(math.sqrt(a))


def desplazar(lat: float, lng: float, dist_m: float, rumbo_grados: float) -> tuple[float, float]:
    """Punto a `dist_m` del origen con el rumbo dado (esfera; suficiente a escala de 1 km)."""
    d = dist_m / RADIO_TIERRA_M
    b = math.radians(rumbo_grados)
    p1, l1 = math.radians(lat), math.radians(lng)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def subcirculos(lat: float, lng: float, radio: float, traslape: float) -> list[tuple[float, float, float]]:
    """Cubre un círculo de radio R con 7 círculos de radio R/2: uno central y seis a R·√3/2."""
    r_sub = radio / 2 * traslape
    d = radio * math.sqrt(3) / 2
    centros = [(lat, lng)] + [desplazar(lat, lng, d, ang) for ang in range(0, 360, 60)]
    return [(la, ln, r_sub) for la, ln in centros]


def a_local_m(lat: float, lng: float, lat0: float, lng0: float) -> tuple[float, float]:
    """Coordenadas planas (x este, y norte) en metros respecto a un origen cercano."""
    x = math.radians(lng - lng0) * RADIO_TIERRA_M * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * RADIO_TIERRA_M
    return x, y


def centroide_anillo(anillo: list) -> tuple[float, float]:
    """Centroide (lng, lat) de un anillo [[lng, lat], ...] por la fórmula del área."""
    pts = [(float(p[0]), float(p[1])) for p in anillo]
    if len(pts) < 3:
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        f = x0 * y1 - x1 * y0
        a += f
        cx += (x0 + x1) * f
        cy += (y0 + y1) * f
    if abs(a) < 1e-15:
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    a *= 0.5
    return cx / (6 * a), cy / (6 * a)


def area_anillo(anillo: list) -> float:
    pts = [(float(p[0]), float(p[1])) for p in anillo]
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))) / 2


def _punto_en_anillo(x: float, y: float, anillo: list[tuple[float, float]]) -> bool:
    dentro = False
    n = len(anillo)
    for i in range(n):
        x1, y1 = anillo[i]
        x2, y2 = anillo[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            dentro = not dentro
    return dentro


def _dist_segmento(x: float, y: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def distancia_punto_poligono_m(lat: float, lng: float, anillos: list) -> float:
    """Distancia del punto al polígono (0 si está adentro). `anillos` en [[lng, lat], ...]."""
    mejor = math.inf
    dentro = False
    for anillo in anillos:
        plano = [a_local_m(p[1], p[0], lat, lng) for p in anillo]
        if len(plano) >= 3 and _punto_en_anillo(0.0, 0.0, plano):
            dentro = not dentro           # un hueco invierte la pertenencia
        for (x1, y1), (x2, y2) in zip(plano, plano[1:] + plano[:1]):
            mejor = min(mejor, _dist_segmento(0.0, 0.0, x1, y1, x2, y2))
    return 0.0 if dentro else mejor


def anillo_de(distancia_m: float | None, anillos: list[int]) -> int | None:
    """Menor anillo de caminabilidad que contiene la distancia."""
    if distancia_m is None:
        return None
    for limite in sorted(anillos):
        if distancia_m <= limite:
            return int(limite)
    return None


@lru_cache(maxsize=1)
def _transformador():
    try:
        from pyproj import Transformer
        return Transformer.from_crs(4326, 9377, always_xy=True)
    except Exception:                      # pyproj ausente: la app sigue; no hay control 9377
        return None


def a_9377(lng: float, lat: float) -> tuple[float | None, float | None]:
    """(x, y) en EPSG:9377. Devuelve (None, None) si pyproj no está disponible."""
    t = _transformador()
    if t is None or lng is None or lat is None:
        return None, None
    x, y = t.transform(lng, lat)
    return round(x, 3), round(y, 3)


def distancia_9377_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float | None:
    x1, y1 = a_9377(lng1, lat1)
    x2, y2 = a_9377(lng2, lat2)
    if None in (x1, y1, x2, y2):
        return None
    return math.hypot(x2 - x1, y2 - y1)
