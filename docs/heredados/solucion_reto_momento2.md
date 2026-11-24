# Solución del reto · ficha de mercado y entorno

Fecha: 14-nov-2026 · Aplicación v0.3.0 · Parámetros oficiales v1.0 · Capas OSM del 2026-11-13.

El reto pide una ficha por inmueble con el precio por m² de comparables según su estado, el transporte, los equipamientos cercanos y la norma urbanística. Esta página resume cómo responde la aplicación a cada insumo, qué produce y cómo se verificó.

## 1. Insumos del reto y cómo se usan

| Insumo pedido | Archivo oficial | Uso en la aplicación |
|---|---|---|
| Plantilla de la ficha | `reto_oficial/docs/plantilla_ficha.md` | Vista de lectura de cada ficha (`salidas/fichas_md/`) |
| JSON de ejemplo | `reto_oficial/ejemplos/ficha_ejemplo.json` | Esquema de salida; una prueba exige todos sus campos |
| Diccionario de datos | `reto_oficial/docs/diccionario_datos.md` | Bloque `esquema` de `params.yaml`, normalización y perfil de calidad |
| Parámetros | `reto_oficial/config/parametros.yaml` | Copiado sin cambios a `config/parametros.yaml`: única fuente de reglas de negocio |
| Cálculo manual de 3 inmuebles | `reto_oficial/validacion/` | Validador oficial en el pipeline y en las pruebas |
| Repositorio y Colab | `reto_oficial/notebooks/reto_ficha_colab.ipynb` | Solución completa en `notebooks/reto_ficha_colab_solucion.ipynb` |

## 2. Procedimiento (igual al cálculo manual)

1. **Normalizar el estado:** minúsculas, sin tildes, sin "?", espacios simples; mapeo a `remodelado`, `habitable` o `por_remodelar`. Sin mapeo: `sin_clasificar`.
2. **Deduplicar** en el orden del CSV: mismo `id_anuncio`, o misma (`zona`, `area_m2`, `precio`); se conserva la primera fila.
3. **Validar:** sin área, sin precio, precio/m² fuera de [1.000.000; 20.000.000], estado sin clasificar o tipo de precio no aceptado.
4. **Comparables:** misma zona y estado, área en ±30 %, sin el grupo de duplicados del propio inmueble.
5. **Estadísticas:** mediana, promedio, mínimo, máximo, desviación muestral (n − 1) y CV.
6. **Confianza:** alta (n ≥ 5 y CV ≤ 15 %), media (n ≥ 3 y CV ≤ 25 %), baja en otro caso.
7. **Entorno:** haversine a estaciones de metro, tranvía y metrocable; estaciones y equipamientos a 800 m o menos.
8. **Norma:** tratamiento POT si hay capa; `pendiente_capa` en Medellín sin capa; `no_disponible` fuera de Medellín.

Lo que agrega el proyecto, sin cambiar ningún resultado oficial: pedido y cierre por separado, motivo de exclusión de cada fila, auditoría de cada duplicado (regla, fila de coincidencia, grupo ambiguo), verificación del CRS de cada capa, control de distancias en EPSG:9377 y huella de parámetros y archivos en cada ficha.

## 3. Resultados

117 anuncios: 33 duplicados, 21 inválidos (20 sin estado, 1 sin área) y 63 válidos.

| Inmueble | Zona | Estado | n | Mediana COP/m² | CV | Confianza | Estación más cercana | Entorno | Norma |
|---|---|---|---:|---:|---:|---|---|---|---|
| INM-01 | El Salvador | por remodelar | 10 | 4.555.556 | 22,2 % | media | Bicentenario, 605 m | exacto | pendiente |
| INM-02 | El Salvador | por remodelar | 13 | 4.776.119 | 16,9 % | media | Bicentenario, 637 m | exacto | pendiente |
| INM-03 | El Salvador | por remodelar | 10 | 4.555.556 | 22,2 % | media | Bicentenario, 832 m | exacto | pendiente |
| INM-04 | El Salvador | habitable | 9 | 4.375.000 | 23,6 % | media | Buenos Aires, 1.000 m | exacto | pendiente |
| INM-05 | El Salvador | por remodelar | 11 | 4.666.667 | 19,3 % | media | Pabellón del agua EPM, 174 m | aproximado | pendiente |
| INM-06 | Castellana | remodelado | 6 | 6.729.529 | 17,0 % | media | Santa Lucía, 1.994 m | aproximado | pendiente |
| INM-07 | Castellana | habitable | 5 | 6.483.516 | 19,7 % | media | Santa Lucía, 1.994 m | aproximado | pendiente |
| INM-08 | Castellana | remodelado | 6 | 7.314.636 | 16,1 % | media | Estadio, 1.516 m | aproximado | pendiente |
| INM-09 | Ditaires | remodelado | 9 | 7.058.824 | 6,4 % | alta | Sabaneta, 1.743 m | aproximado | no disponible |
| INM-10 | Ditaires | por remodelar | 3 | 5.396.825 | 14,6 % | media | Sabaneta, 1.743 m | aproximado | no disponible |
| INM-11 | Ditaires | por remodelar | 3 | 5.625.000 | 14,2 % | media | Sabaneta, 1.743 m | aproximado | no disponible |

Todos los precios son de oferta (pedido): la muestra no tiene fechas ni precios de cierre, y cada ficha lo declara en `datos_faltantes`. La confianza mide la calidad y la suficiencia de los datos, no la conveniencia de comprar.

## 4. Verificación

| Prueba | Resultado |
|---|---|
| Validador oficial contra el cálculo manual (INM-04, INM-06, INM-09) | 3/3 sin diferencias |
| Implementación de referencia del reto, campo por campo | 11/11 fichas idénticas en todos los campos oficiales |
| Solución del notebook de Colab (solo biblioteca estándar, PyYAML y pandas) | 11/11 fichas idénticas a la aplicación |
| Pruebas automatizadas (reglas del proyecto, casos oficiales, notebook) | 60 en verde |
| Dos corridas seguidas | Fichas idénticas byte a byte |

## 5. Hallazgos de la curación de datos

Detalle y cifras en `auditoria/perfil_datos.md`.

- **La regla de duplicados pierde anuncios válidos.** Conserva la primera fila del grupo aunque no tenga estado: 8 anuncios con estado quedan absorbidos por filas sin estado. Conservar la fila válida más completa cambiaría el mercado de 5 inmuebles (INM-04: 9 → 10 comparables). Es decisión de Venture One (A-08).
- **Posibles republicaciones.** 10 parejas con área y precio a ±3 % que la regla exacta no agrupa; varias son comparables de INM-04 (A-09).
- **Duplicados ambiguos.** 3 grupos unen anuncios con ids distintos; por eso INM-01 e INM-03 tienen el mismo mercado (A-03).
- **Haversine frente a la distancia real.** Haversine alarga las distancias norte-sur hasta 10 m (INM-06: 1.994 frente a 1.984 m en EPSG:9377). La ficha usa haversine, como el cálculo manual, y trae el control.
- **Entorno aproximado.** 7 de 11 inmuebles tienen coordenadas de barrio y comparten punto con otros inmuebles.

## 6. Pendiente

1. Capa POT de Medellín (`pot_tratamientos.geojson`), que el geoportal bloqueó a las descargas automáticas. Con ella, INM-04 e INM-06 pasan de `pendiente_capa` a un tratamiento y hay que actualizar `esperado.json`.
2. Decisiones abiertas A-02 a A-09 de `auditoria/decisiones.md`.
