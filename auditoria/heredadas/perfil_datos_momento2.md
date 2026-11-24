# Perfil y curación de los datos del reto

Generado el 2026-11-14 con la aplicación ficha 0.3.0 y parámetros v1.0 (huella `89a6efd4e4bc3c53`). Se regenera con `python scripts/perfilar_datos.py`.

## 1. Entradas

| archivo | registros | columnas | sha256 |
|---|---|---|---|
| reto_oficial/data/anuncios_muestra.csv | 117 | 14 | 79245590ed3a |
| reto_oficial/data/inmuebles_prueba.csv | 11 | 13 | 566290f7fa55 |
| reto_oficial/data/geo/transporte.geojson | 42 | implícito WGS84 (RFC 7946, sin miembro 'crs') | 55128b4b264b |
| reto_oficial/data/geo/equipamientos.geojson | 2714 | implícito WGS84 (RFC 7946, sin miembro 'crs') | 2a811f7af4b3 |
| reto_oficial/data/geo/pot_tratamientos.geojson | 0 | pendiente | pendiente |

- Las capas GeoJSON no traen miembro `crs`: por RFC 7946 son WGS84 (EPSG:4326), y así las lee geopandas. Sus coordenadas caen dentro del área de estudio, lo que confirma el CRS.
- Capa POT: **pendiente**. Sin ella, la norma de los inmuebles de Medellín queda `pendiente_capa`.

## 2. Calidad por columna (anuncios)

| columna | vacios | pct_vacio | no_reconocidos |
|---|---|---|---|
| id_anuncio | 50 | 42.7 |  |
| fuente | 0 | 0.0 |  |
| fecha | 117 | 100.0 |  |
| zona | 0 | 0.0 |  |
| municipio | 0 | 0.0 |  |
| barrio | 25 | 21.4 |  |
| area_m2 | 1 | 0.9 |  |
| precio | 0 | 0.0 |  |
| habitaciones | 5 | 4.3 |  |
| banos | 9 | 7.7 |  |
| parqueadero | 82 | 70.1 | '-', '?' |
| estado | 25 | 21.4 |  |
| tipo_precio | 0 | 0.0 |  |
| origen | 0 | 0.0 |  |

- `fecha` está vacía en las 117 filas y todos los precios son `pedido`: ninguna ficha puede tener precios de cierre ni fecha de publicación (se declaran en `datos_faltantes`).
- `id_anuncio` vacío en Facebook y letreros es intencional (el enlace identifica a una persona).

## 3. Normalización del estado

| texto | normalizado | canonico | filas |
|---|---|---|---|
| OK | ok | habitable | 16 |
| Vivir | vivir | habitable | 9 |
| HABITABLE POR REMODELAR | habitable por remodelar | habitable | 6 |
| Ok | ok | habitable | 2 |
| VIVIR | vivir | habitable | 1 |
| vivir | vivir | habitable | 1 |
| Remodelar | remodelar | por_remodelar | 20 |
| remodelar | remodelar | por_remodelar | 5 |
| Reformar | reformar | por_remodelar | 3 |
| SIN REMODELAR (OBRA NEGRA) | sin remodelar (obra negra) | por_remodelar | 3 |
| SIN REMODELAR | sin remodelar | por_remodelar | 2 |
| REMODELADA | remodelada | remodelado | 13 |
| REMODELADO | remodelado | remodelado | 10 |
| REMODELADA ? | remodelada | remodelado | 1 |
| (vacío) |  | sin_clasificar | 25 |

- 25 filas quedan `sin_clasificar` (todas por estado vacío) y no pueden ser comparables.

## 4. Duplicados (regla oficial)

| zona | filas | duplicados | unicos | R1_id_anuncio | R2_claves_sin_id |
|---|---|---|---|---|---|
| buenos_aires_el_salvador | 82 | 28 | 54 | 8 | 20 |
| castellana_laureles | 16 | 3 | 13 | 3 | 0 |
| ditaires_itagui | 19 | 2 | 17 | 0 | 2 |

**Grupos ambiguos** (ids distintos con la misma zona, área y precio). La regla oficial los fusiona; pueden ser el mismo inmueble en varias inmobiliarias o unidades iguales de un edificio:

| grupo_dup | zona | area_m2 | precio | ids | miembros |
|---|---|---|---|---|---|
| extension_poligono.xlsx:REMODELAR:68 | buenos_aires_el_salvador | 73.0 | 300.000.000 | arrendamientos_santafe:V10746; fincaraiz:7387557 | 5 |
| extension_poligono.xlsx:REMODELAR:69 | buenos_aires_el_salvador | 57.0 | 225.000.000 | arrendamientos_santafe:V12132; fincaraiz:10766727 | 3 |
| factibilidad_proyecto2.xlsx:SECTORIAL:5 | ditaires_itagui | 73.0 | 490.000.000 | fincaraiz:192937244; fincaraiz:192959138; fincaraiz:193131396 | 3 |

**Anuncios válidos absorbidos por un representante inválido.** La regla oficial conserva la primera fila del CSV aunque no tenga estado; sus duplicados con estado se pierden como comparables:

| origen | estado_texto | estado_canonico | id_representante | motivo_representante |
|---|---|---|---|---|
| extension_poligono.xlsx:REMODELAR:77 | Remodelar | por_remodelar | extension_poligono.xlsx:REMODELAR:69 | estado_sin_clasificar |
| extension_poligono.xlsx:REMODELAR:78 | Remodelar | por_remodelar | extension_poligono.xlsx:REMODELAR:68 | estado_sin_clasificar |
| extension_poligono.xlsx:El salvador:3 | Remodelar | por_remodelar | extension_poligono.xlsx:REMODELAR:60 | estado_sin_clasificar |
| extension_poligono.xlsx:El salvador:5 | Remodelar | por_remodelar | extension_poligono.xlsx:REMODELAR:68 | estado_sin_clasificar |
| extension_poligono.xlsx:El salvador:6 | Remodelar | por_remodelar | extension_poligono.xlsx:REMODELAR:69 | estado_sin_clasificar |
| extension_poligono.xlsx:El salvador:7 | Remodelar | por_remodelar | extension_poligono.xlsx:REMODELAR:68 | estado_sin_clasificar |
| extension_poligono.xlsx:El salvador:11 | Ok | habitable | extension_poligono.xlsx:REMODELAR:39 | estado_sin_clasificar |
| extension_poligono.xlsx:El salvador:12 | OK | habitable | extension_poligono.xlsx:REMODELAR:65 | estado_sin_clasificar |

Candidatos casi iguales que la regla exacta no agrupa (área y precio a ±3 %, solo para revisión; no se fusionan): **10**. Detalle en `auditoria/duplicados_revision.csv`.

| origen_a | origen_b | zona | estado_a | estado_b | area_a | area_b | dif_precio_pct | puntaje |
|---|---|---|---|---|---|---|---|---|
| extension_poligono.xlsx:REMODELAR:85 | extension_poligono.xlsx:REMODELAR:88 | buenos_aires_el_salvador | habitable | habitable | 64.0 | 64.0 | 0.02 | 0.6667 |
| extension_poligono.xlsx:REMODELAR:83 | extension_poligono.xlsx:REMODELAR:85 | buenos_aires_el_salvador | por_remodelar | habitable | 65.0 | 64.0 | 0.0 | 0.7436 |
| extension_poligono.xlsx:REMODELAR:83 | extension_poligono.xlsx:REMODELAR:88 | buenos_aires_el_salvador | por_remodelar | habitable | 65.0 | 64.0 | 0.02 | 0.4103 |
| extension_poligono.xlsx:Bombona:10 | extension_poligono.xlsx:Bombona:15 | buenos_aires_el_salvador | habitable | habitable | 65.0 | 65.0 | 0.0233 | 0.6124 |
| factibilidad_castellana.xlsx:SECTORIAL:6 | factibilidad_castellana.xlsx:SECTORIAL:13 | castellana_laureles | habitable | habitable | 89.0 | 90.0 | 0.0 | 0.8148 |
| factibilidad_castellana.xlsx:SECTORIAL:5 | factibilidad_castellana.xlsx:SECTORIAL:6 | castellana_laureles | habitable | habitable | 91.0 | 89.0 | 0.0169 | 0.3512 |
| factibilidad_castellana.xlsx:SECTORIAL:5 | factibilidad_castellana.xlsx:SECTORIAL:13 | castellana_laureles | habitable | habitable | 91.0 | 90.0 | 0.0169 | 0.5344 |
| factibilidad_castellana.xlsx:SECTORIAL:9 | factibilidad_castellana.xlsx:SECTORIAL:14 | castellana_laureles | remodelado | habitable | 100.0 | 100.0 | 0.0225 | 0.6255 |
| factibilidad_proyecto2.xlsx:SECTORIAL:3 | factibilidad_proyecto2.xlsx:SECTORIAL:6 | ditaires_itagui | remodelado | remodelado | 60.0 | 61.0 | 0.0209 | 0.3779 |
| factibilidad_proyecto2.xlsx:SECTORIAL:6 | factibilidad_proyecto2.xlsx:SECTORIAL:10 | ditaires_itagui | remodelado | remodelado | 61.0 | 62.0 | 0.0209 | 0.3823 |

## 5. Validez de las filas únicas

| zona | estado_sin_clasificar | sin_area | válido |
|---|---|---|---|
| buenos_aires_el_salvador | 20 | 1 | 33 |
| castellana_laureles | 0 | 0 | 13 |
| ditaires_itagui | 0 | 0 | 17 |

## 6. Precio/m² de los anuncios válidos por zona y estado (COP)

| zona | estado_canonico | n | minimo | p25 | mediana | p75 | maximo | cv |
|---|---|---|---|---|---|---|---|---|
| buenos_aires_el_salvador | habitable | 15 | 2.777.778 | 3.722.573 | 3.909.091 | 4.972.755 | 6.615.385 | 0.263 |
| buenos_aires_el_salvador | por_remodelar | 18 | 2.247.191 | 3.967.949 | 4.708.333 | 5.146.302 | 6.744.186 | 0.300 |
| castellana_laureles | habitable | 6 | 5.487.805 | 6.092.818 | 6.463.980 | 6.508.520 | 8.900.000 | 0.178 |
| castellana_laureles | remodelado | 7 | 5.402.174 | 6.300.236 | 6.969.697 | 8.179.787 | 9.111.111 | 0.189 |
| ditaires_itagui | por_remodelar | 5 | 4.833.333 | 5.396.825 | 5.625.000 | 5.967.742 | 6.428.571 | 0.106 |
| ditaires_itagui | remodelado | 12 | 6.060.606 | 6.827.336 | 6.997.154 | 7.316.335 | 9.009.901 | 0.105 |

Atípicos por rango intercuartílico (1,5 × IQR) dentro de cada zona y estado. **Solo diagnóstico**: la regla oficial excluye únicamente precio/m² fuera de [1.000.000; 20.000.000], y no hay filas fuera de ese rango.

| zona | estado | origen | area_m2 | precio_m2 |
|---|---|---|---|---|
| castellana_laureles | habitable | factibilidad_castellana.xlsx:SECTORIAL:14 | 100.0 | 8.900.000 |
| ditaires_itagui | remodelado | factibilidad_proyecto2.xlsx:SECTORIAL:2 | 66.0 | 6.060.606 |
| ditaires_itagui | remodelado | factibilidad_proyecto2.xlsx:SECTORIAL:21 | 101.0 | 9.009.901 |

## 7. Zonas de mercado y municipios

| zona | envigado | itagui | medellin |
|---|---|---|---|
| buenos_aires_el_salvador | 0 | 0 | 82 |
| castellana_laureles | 0 | 0 | 16 |
| ditaires_itagui | 1 | 18 | 0 |

- La zona `ditaires_itagui` incluye un anuncio de Envigado. Los comparables se eligen por zona (regla oficial), así que puede entrar en el cálculo de un inmueble de Itagüí.
- `barrio` es texto libre con errores ("salvadror/palmas") y vacíos; no interviene en el cálculo.

## 8. Inmuebles de prueba

| id_inmueble | zona | municipio | area_m2 | estado | estado_canonico | precision_coordenadas | comparte_coordenadas_con |
|---|---|---|---|---|---|---|---|
| INM-01 | buenos_aires_el_salvador | medellin | 73.0 | Remodelar | por_remodelar | cruce |  |
| INM-02 | buenos_aires_el_salvador | medellin | 57.0 | Remodelar | por_remodelar | cruce |  |
| INM-03 | buenos_aires_el_salvador | medellin | 73.0 | Remodelar | por_remodelar | cruce |  |
| INM-04 | buenos_aires_el_salvador | medellin | 71.0 | OK | habitable | cruce |  |
| INM-05 | buenos_aires_el_salvador | medellin | 67.0 | Remodelar | por_remodelar | barrio |  |
| INM-06 | castellana_laureles | medellin | 90.0 | REMODELADO | remodelado | barrio | INM-07 |
| INM-07 | castellana_laureles | medellin | 90.0 | HABITABLE POR REMODELAR | habitable | barrio | INM-06 |
| INM-08 | castellana_laureles | medellin | 92.0 | REMODELADO | remodelado | barrio |  |
| INM-09 | ditaires_itagui | itagui | 73.0 | REMODELADA | remodelado | barrio | INM-10, INM-11 |
| INM-10 | ditaires_itagui | itagui | 64.0 | SIN REMODELAR (OBRA NEGRA) | por_remodelar | barrio | INM-09, INM-11 |
| INM-11 | ditaires_itagui | itagui | 63.0 | SIN REMODELAR (OBRA NEGRA) | por_remodelar | barrio | INM-09, INM-10 |

- 7 de 11 inmuebles tienen coordenadas de barrio: su entorno es aproximado y comparten punto con otros inmuebles.

## 9. Control de distancias: haversine (oficial) frente a EPSG:9377

| id | estacion | haversine_m | epsg9377_m | diferencia_m | equip_800m_haversine | equip_800m_epsg9377 | equip_que_cambian |
|---|---|---|---|---|---|---|---|
| INM-01 | Bicentenario | 605 | 602 | -3 | 65 | 66 | 1 |
| INM-02 | Bicentenario | 637 | 635 | -2 | 63 | 63 | 0 |
| INM-03 | Bicentenario | 832 | 830 | -2 | 47 | 47 | 0 |
| INM-04 | Buenos Aires | 1000 | 998 | -2 | 38 | 38 | 0 |
| INM-05 | Pabellón del agua EPM | 174 | 173 | -1 | 104 | 105 | 1 |
| INM-06 | Santa Lucía | 1994 | 1984 | -10 | 59 | 59 | 0 |
| INM-07 | Santa Lucía | 1994 | 1984 | -10 | 59 | 59 | 0 |
| INM-08 | Estadio | 1516 | 1511 | -5 | 78 | 78 | 0 |
| INM-09 | Sabaneta | 1743 | 1739 | -4 | 21 | 21 | 0 |
| INM-10 | Sabaneta | 1743 | 1739 | -4 | 21 | 21 | 0 |
| INM-11 | Sabaneta | 1743 | 1739 | -4 | 21 | 21 | 0 |

- Haversine usa una esfera; cerca del ecuador alarga las distancias norte-sur hasta un 0,5 %. EPSG:9377 coincide con la distancia geodésica sobre el elipsoide a ±1 m.
- La ficha usa haversine porque es el método del cálculo manual oficial (decisión del 14-nov-2026); EPSG:9377 queda como control.

## 10. Sensibilidad: representante de duplicados

Si cada grupo de duplicados conservara la fila válida más completa (en vez de la primera del CSV), 5 de 11 inmuebles cambiarían su mercado:

| id | estado | n_oficial | mediana_oficial | cv_oficial | confianza_oficial | n_mas_completo | mediana_mas_completo | cv_mas_completo | confianza_mas_completo | cambia |
|---|---|---|---|---|---|---|---|---|---|---|
| INM-01 | por_remodelar | 10 | 4.555.556 | 0.2221 | media | 12 | 4.388.889 | 0.2112 | media | True |
| INM-02 | por_remodelar | 13 | 4.776.119 | 0.169 | media | 15 | 4.750.000 | 0.1842 | media | True |
| INM-03 | por_remodelar | 10 | 4.555.556 | 0.2221 | media | 12 | 4.388.889 | 0.2112 | media | True |
| INM-04 | habitable | 9 | 4.375.000 | 0.2357 | media | 10 | 4.310.788 | 0.2289 | media | True |
| INM-05 | por_remodelar | 11 | 4.666.667 | 0.1927 | media | 14 | 4.407.363 | 0.1938 | media | True |
| INM-06 | remodelado | 6 | 6.729.529 | 0.17 | media | 6 | 6.729.529 | 0.17 | media | False |
| INM-07 | habitable | 5 | 6.483.516 | 0.197 | media | 5 | 6.483.516 | 0.197 | media | False |
| INM-08 | remodelado | 6 | 7.314.636 | 0.1609 | media | 6 | 7.314.636 | 0.1609 | media | False |
| INM-09 | remodelado | 9 | 7.058.824 | 0.0639 | alta | 9 | 7.058.824 | 0.0639 | alta | False |
| INM-10 | por_remodelar | 3 | 5.396.825 | 0.1457 | media | 3 | 5.396.825 | 0.1457 | media | False |
| INM-11 | por_remodelar | 3 | 5.625.000 | 0.1417 | media | 3 | 5.625.000 | 0.1417 | media | False |

La regla oficial sigue siendo la de las fichas (`deduplicacion.representante: primera_fila`). Cambiarla es una decisión de Venture One (A-08 en `auditoria/decisiones.md`).

## 11. Limitaciones que ningún proceso corrige

- Sin fechas ni precios de cierre: el precio/m² es de oferta (pedido), no de transacción.
- Los anuncios no tienen coordenadas: los comparables se eligen por zona, no por distancia.
- 7 de 11 inmuebles tienen coordenadas de barrio: su entorno es aproximado.
- La capa POT no está: la norma de Medellín queda `pendiente_capa`; Itagüí y Envigado no tienen capa.
- Las capas de OSM dependen de lo que mapearon los voluntarios: pueden faltar estaciones (Metroplús) o equipamientos.
