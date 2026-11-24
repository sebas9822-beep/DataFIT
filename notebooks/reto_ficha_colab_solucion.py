# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.6
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Ficha automática de mercado y entorno — Venture One SAS · solución
#
# **Reto 13 · EstudIA · Análisis de datos · Capa 2 · Nivel básico**
#
# Solución del notebook de arranque `reto_ficha_colab.ipynb`. Completa las cuatro funciones de la
# plantilla (`limpiar_anuncios`, `bloque_mercado`, `bloque_entorno`, `generar_ficha`) con solo
# biblioteca estándar, PyYAML y pandas, para que corra igual en Google Colab.
#
# Genera, para cada inmueble de prueba, una ficha JSON con:
# 1. **Mercado:** precio/m² de comparables en el mismo estado y una regla de confianza.
# 2. **Entorno:** estación de transporte más cercana y equipamientos dentro del radio de búsqueda.
# 3. **Norma:** tratamiento del POT (cuando la capa esté disponible).
# 4. **Calidad:** duplicados, datos inválidos y datos faltantes.
#
# Reglas del reto: solo los datos entregados, sin scraping, sin datos personales. Ningún número va
# fijo en el código: todo sale de `config/parametros.yaml`. La ficha no recomienda comprar.

# %%
# 1. Preparar el entorno: clona el repositorio en Colab y se ubica en su raíz.
import os
import subprocess
import sys

REPO_URL = "https://github.com/solhecoo/ventureone-reto-ficha.git"
CARPETA = "ventureone-reto-ficha"

if "google.colab" in sys.modules and not os.path.exists(CARPETA):
    subprocess.run(["git", "clone", "-q", REPO_URL], check=True)
for candidata in (CARPETA, "reto_oficial", "../reto_oficial", ".."):      # Colab, o la copia local del proyecto
    if os.path.exists(os.path.join(candidata, "config", "parametros.yaml")) and \
            os.path.exists(os.path.join(candidata, "data", "anuncios_muestra.csv")):
        os.chdir(candidata)
        break
assert os.path.exists("data/anuncios_muestra.csv"), "No se encontró la raíz del repositorio del reto"
print("Raíz del repositorio:", os.getcwd())

# %%
# 2. Cargar parámetros y datos.
import csv
import json

import yaml

PARAMS = yaml.safe_load(open("config/parametros.yaml", encoding="utf-8"))
ANUNCIOS = list(csv.DictReader(open("data/anuncios_muestra.csv", encoding="utf-8")))
INMUEBLES = list(csv.DictReader(open("data/inmuebles_prueba.csv", encoding="utf-8")))
TRANSPORTE = json.load(open("data/geo/transporte.geojson", encoding="utf-8"))
EQUIPAMIENTOS = json.load(open("data/geo/equipamientos.geojson", encoding="utf-8"))
POT = json.load(open(PARAMS["norma"]["archivo_pot"], encoding="utf-8")) if os.path.exists(PARAMS["norma"]["archivo_pot"]) else None

print(f"Anuncios: {len(ANUNCIOS)} · Inmuebles de prueba: {len(INMUEBLES)}")
print(f"Estaciones: {len(TRANSPORTE['features'])} · Equipamientos: {len(EQUIPAMIENTOS['features'])} · OSM {TRANSPORTE['metadata']['fecha_osm']}")
print("Capa POT:", "cargada" if POT else "pendiente")

# %% [markdown]
# ## Exploración
#
# Calidad antes de calcular: ids repetidos, variantes de `estado`, columnas vacías.

# %%
import pandas as pd

try:
    display
except NameError:                      # fuera de Jupyter/Colab
    display = print

df = pd.read_csv("data/anuncios_muestra.csv")
display(df.head())
print("Filas por zona:\n", df["zona"].value_counts(), "\n")
print("Variantes de estado:\n", df["estado"].value_counts(dropna=False), "\n")
print("id_anuncio repetidos:", df["id_anuncio"].dropna().duplicated().sum())
print("Vacíos por columna:\n", df.isna().sum())

# %% [markdown]
# ## Solución
#
# Procedimiento de `validacion/calculo_manual.md`:
# 1. Normalizar el estado (minúsculas, sin tildes, sin "?", sin espacios extremos) y mapearlo.
# 2. Deduplicar en el orden del CSV: mismo `id_anuncio`, o misma (`zona`, `area_m2`, `precio`). Se conserva la primera fila.
# 3. Validar: sin área, sin precio, precio/m² fuera de rango, estado sin clasificar, tipo de precio no aceptado.
# 4. Comparables: misma zona y estado, área en ±30 %, sin el grupo de duplicados del propio inmueble.
# 5. Estadísticas: mediana, promedio, desviación muestral (n − 1) y CV; regla de confianza.
# 6. Entorno: haversine a estaciones y equipamientos dentro del radio.

# %%
import math
import statistics
import unicodedata
from collections import Counter
from datetime import date


def haversine_m(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos en grados decimales."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(a))


def _texto(valor):
    sin_tildes = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.replace("?", " ").lower().split())


def estado_canonico(texto, params):
    limpio = _texto(texto)
    for canonico, variantes in params["estados"].items():
        if limpio in (_texto(v) for v in variantes):
            return canonico
    return "sin_clasificar"


def _positivo(valor):
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _motivo_invalido(a, params):
    if a["area_m2"] is None:
        return "sin_area"
    if a["precio"] is None:
        return "sin_precio"
    minimo, maximo = params["comparables"]["rango_precio_m2_valido"]
    if not minimo <= a["precio_m2"] <= maximo:
        return "precio_m2_fuera_de_rango"
    if a["estado_canonico"] == "sin_clasificar":
        return "estado_sin_clasificar"
    if a["tipo_precio"] not in params["comparables"]["tipos_precio"]:
        return "tipo_precio_no_aceptado"
    return None


def limpiar_anuncios(anuncios, params):
    """Normaliza estados, elimina duplicados y marca los anuncios inválidos.

    Devuelve las filas únicas, cada una con `motivo_invalido` (None si es válida) y la lista de
    orígenes e ids que absorbió como duplicados.
    """
    columnas = params["deduplicacion"]["claves_sin_id"]
    unicos, por_id, por_clave = [], {}, {}
    for fila in anuncios:
        area, precio = _positivo(fila["area_m2"]), _positivo(fila["precio"])
        a = {**fila, "area_m2": area, "precio": precio, "precio_m2": precio / area if area and precio else None,
             "estado_canonico": estado_canonico(fila["estado"], params)}
        valores = tuple(a[c] for c in columnas)
        clave = None if any(v in (None, "") for v in valores) else valores
        destino = por_id.get(a["id_anuncio"]) if a["id_anuncio"] else None
        if destino is None and clave is not None:
            destino = por_clave.get(clave)
        if destino is None:
            destino = {**a, "origenes_agrupados": [], "ids_agrupados": []}
            unicos.append(destino)
        destino["origenes_agrupados"].append(a["origen"])
        if a["id_anuncio"] and a["id_anuncio"] not in destino["ids_agrupados"]:
            destino["ids_agrupados"].append(a["id_anuncio"])
        if a["id_anuncio"]:
            por_id.setdefault(a["id_anuncio"], destino)
        if clave is not None:
            por_clave.setdefault(clave, destino)
    for u in unicos:
        u["motivo_invalido"] = _motivo_invalido(u, params)
    return unicos


def _es_propio(anuncio, inmueble):
    return (inmueble["origen"] in anuncio["origenes_agrupados"]
            or (inmueble["id_anuncio"] and inmueble["id_anuncio"] in anuncio["ids_agrupados"]))


def _estadisticas(valores):
    n = len(valores)
    if n == 0:
        return {"n": 0, "mediana": None, "promedio": None, "minimo": None, "maximo": None,
                "desviacion": None, "coef_variacion": None}
    promedio = statistics.mean(valores)
    desv = statistics.stdev(valores) if n >= 2 else None
    return {"n": n, "mediana": round(statistics.median(valores)), "promedio": round(promedio),
            "minimo": round(min(valores)), "maximo": round(max(valores)),
            "desviacion": round(desv) if desv is not None else None,
            "coef_variacion": round(desv / promedio, 4) if desv is not None else None}


def _confianza(n, cv, reglas):
    for nivel in ("alta", "media"):
        r = reglas[nivel]
        if n >= r["min_comparables"] and cv is not None and cv <= r["max_coef_variacion"]:
            return nivel, (f"{n} comparables (mínimo {r['min_comparables']}) y coeficiente de variación "
                           f"{cv:.2%} (máximo {r['max_coef_variacion']:.0%})")
    m = reglas["media"]
    texto_cv = "sin dato" if cv is None else f"{cv:.2%}"
    return "baja", (f"{n} comparables y coeficiente de variación {texto_cv}: no cumple el nivel media "
                    f"(mínimo {m['min_comparables']} comparables, máximo {m['max_coef_variacion']:.0%})")


def bloque_mercado(inmueble, anuncios_limpios, params):
    """Comparables, estadísticas de precio/m² y nivel de confianza."""
    estado, area = estado_canonico(inmueble["estado"], params), float(inmueble["area_m2"])
    tol = params["comparables"]["tolerancia_area"]
    zona = [a for a in anuncios_limpios
            if a["motivo_invalido"] is None and a["zona"] == inmueble["zona"] and not _es_propio(a, inmueble)]
    comparables = sorted((a for a in zona if a["estado_canonico"] == estado and abs(a["area_m2"] - area) <= tol * area),
                         key=lambda a: a["precio_m2"])
    est = _estadisticas([a["precio_m2"] for a in comparables])
    nivel, motivo = _confianza(est["n"], est["coef_variacion"], params["confianza"])
    por_estado = {}
    for e in params["estados"]:
        s = _estadisticas([a["precio_m2"] for a in zona if a["estado_canonico"] == e])
        por_estado[e] = {"n": s["n"], "mediana": s["mediana"]}
    return {
        "estado_referencia": estado,
        "precio_m2": est,
        "confianza": {"nivel": nivel, "motivo": motivo},
        "comparables": [{"origen": a["origen"], "id_anuncio": a["id_anuncio"], "fuente": a["fuente"],
                         "area_m2": a["area_m2"], "precio": round(a["precio"]), "precio_m2": round(a["precio_m2"])}
                        for a in comparables],
        "precio_m2_por_estado_en_zona": por_estado,
    }


def bloque_entorno(inmueble, transporte, equipamientos, params):
    """Estación más cercana y conteos dentro del radio."""
    p = params["entorno"]
    lat, lon, radio = float(inmueble["lat"]), float(inmueble["lon"]), p["radio_busqueda_m"]

    def distancia(f):
        flon, flat = f["geometry"]["coordinates"]
        return haversine_m(lat, lon, flat, flon)

    estaciones = sorted(((distancia(f), f) for f in transporte["features"]
                         if f["properties"]["sistema"] in p["sistemas_transporte"]), key=lambda par: par[0])
    cercana = None
    if estaciones:
        d, f = estaciones[0]
        cercana = {"nombre": f["properties"]["nombre"], "sistema": f["properties"]["sistema"], "distancia_m": round(d)}
    conteo = {c: 0 for c in p["categorias_equipamientos"]}
    for f in equipamientos["features"]:
        if f["properties"]["categoria"] in conteo and distancia(f) <= radio:
            conteo[f["properties"]["categoria"]] += 1
    return {"radio_busqueda_m": radio, "estacion_mas_cercana": cercana,
            "estaciones_en_radio": sum(1 for d, _ in estaciones if d <= radio),
            "equipamientos_en_radio": conteo, "aproximado": inmueble["precision_coordenadas"] != "cruce"}


def _en_anillo(lon, lat, anillo):
    dentro, j = False, len(anillo) - 1
    for i in range(len(anillo)):
        (xi, yi), (xj, yj) = anillo[i][:2], anillo[j][:2]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            dentro = not dentro
        j = i
    return dentro


def bloque_norma(inmueble, pot, params):
    n = params["norma"]
    if inmueble["municipio"] not in n["municipios_cubiertos"]:
        return {"tratamiento_pot": None, "estado": "no_disponible",
                "motivo": f"La capa POT cubre solo: {', '.join(n['municipios_cubiertos'])}"}
    if pot is None:
        return {"tratamiento_pot": None, "estado": "pendiente_capa", "motivo": f"Falta el archivo {n['archivo_pot']}"}
    lat, lon = float(inmueble["lat"]), float(inmueble["lon"])
    for f in pot["features"]:
        g = f.get("geometry") or {}
        poligonos = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
        for exterior, *huecos in poligonos:
            if _en_anillo(lon, lat, exterior) and not any(_en_anillo(lon, lat, h) for h in huecos):
                return {"tratamiento_pot": f["properties"].get(n["campo_tratamiento"]), "estado": "encontrado", "motivo": ""}
    return {"tratamiento_pot": None, "estado": "fuera_de_capa", "motivo": "El punto no cae en ningún polígono"}


LIMPIOS = limpiar_anuncios(ANUNCIOS, PARAMS)


def _calidad_zona(zona):
    crudos = [a for a in ANUNCIOS if a["zona"] == zona]
    unicos = [a for a in LIMPIOS if a["zona"] == zona]
    invalidos = Counter(a["motivo_invalido"] for a in unicos if a["motivo_invalido"])
    return {"anuncios_en_zona": len(crudos), "duplicados_eliminados": len(crudos) - len(unicos),
            "anuncios_unicos": len(unicos), "anuncios_invalidos": dict(sorted(invalidos.items())),
            "anuncios_validos": len(unicos) - sum(invalidos.values())}


def generar_ficha(inmueble):
    """Arma la ficha completa con el formato de ejemplos/ficha_ejemplo.json."""
    mercado = bloque_mercado(inmueble, LIMPIOS, PARAMS)
    usados = {c["origen"] for c in mercado["comparables"]}
    comparables = [a for a in LIMPIOS if a["origen"] in usados]
    faltantes = []
    if not inmueble["direccion"]:
        faltantes.append("direccion")
    if inmueble["precision_coordenadas"] != "cruce":
        faltantes.append("coordenadas_exactas")
    if not any(a["fecha"] for a in comparables):
        faltantes.append("fecha_publicacion_comparables")
    if not any(a["tipo_precio"] == "cierre" for a in comparables):
        faltantes.append("precios_de_cierre")
    return {
        "id_inmueble": inmueble["id_inmueble"],
        "inmueble": {
            "direccion": inmueble["direccion"] or None, "barrio": inmueble["barrio"],
            "municipio": inmueble["municipio"], "zona": inmueble["zona"], "area_m2": float(inmueble["area_m2"]),
            "estado_texto": inmueble["estado"], "estado": estado_canonico(inmueble["estado"], PARAMS),
            "coordenadas": {"lat": float(inmueble["lat"]), "lon": float(inmueble["lon"]),
                            "precision": inmueble["precision_coordenadas"], "fuente": inmueble["fuente_coordenadas"]},
        },
        "mercado": mercado,
        "calidad_datos_zona": _calidad_zona(inmueble["zona"]),
        "entorno": bloque_entorno(inmueble, TRANSPORTE, EQUIPAMIENTOS, PARAMS),
        "norma": bloque_norma(inmueble, POT, PARAMS),
        "datos_faltantes": faltantes,
        "metadatos": {
            "version_parametros": PARAMS["version"], "fecha_generacion": date.today().isoformat(),
            "fecha_capas_osm": TRANSPORTE["metadata"]["fecha_osm"],
            "fuentes": ["data/anuncios_muestra.csv", "OpenStreetMap (ODbL)"] + ([PARAMS["norma"]["archivo_pot"]] if POT else []),
        },
    }

# %%
# Generar una ficha por inmueble en salidas/participante/
os.makedirs("salidas/participante", exist_ok=True)
for inmueble in INMUEBLES:
    ficha = generar_ficha(inmueble)
    with open(f"salidas/participante/{inmueble['id_inmueble']}.json", "w", encoding="utf-8") as f:
        json.dump(ficha, f, ensure_ascii=False, indent=2)
print("Fichas generadas:", sorted(os.listdir("salidas/participante")))

# %%
# Resumen de las fichas
resumen = pd.DataFrame([{
    "inmueble": i["id_inmueble"], "estado": (f := generar_ficha(i))["mercado"]["estado_referencia"],
    "n": f["mercado"]["precio_m2"]["n"], "mediana_m2": f["mercado"]["precio_m2"]["mediana"],
    "cv": f["mercado"]["precio_m2"]["coef_variacion"], "confianza": f["mercado"]["confianza"]["nivel"],
    "estacion": (f["entorno"]["estacion_mas_cercana"] or {}).get("nombre"),
    "distancia_m": (f["entorno"]["estacion_mas_cercana"] or {}).get("distancia_m"), "norma": f["norma"]["estado"],
} for i in INMUEBLES])
display(resumen)

# %% [markdown]
# ## Validación contra el cálculo manual
#
# `validacion/calculo_manual.md` calcula a mano INM-04, INM-06 e INM-09.

# %%
resultado = subprocess.run([sys.executable, "validacion/validar.py", "salidas/participante"], capture_output=True, text=True)
print(resultado.stdout or resultado.stderr)
print("✅ Coincide con el cálculo manual" if resultado.returncode == 0 else "❌ Revise las diferencias")
