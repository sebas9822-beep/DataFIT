# Registro de decisiones

Las decisiones de criterio quedan aquí y, cuando son un número o un interruptor, en `config/params.yaml` (técnicas) o `config/parametros.yaml` (reglas oficiales del reto). Este archivo se escribe a mano; el resto de `auditoria/` lo genera la aplicación.

## Fase 5 · datos oficiales del reto (14-nov-2026)

| # | Decisión | Motivo | Dónde vive |
|---|---|---|---|
| D-15 | Las reglas de negocio salen del archivo oficial `config/parametros.yaml` (v1.0), copiado sin cambios. `params.yaml` solo agrega parámetros técnicos. | Una sola fuente por regla; cuando Venture One edite sus parámetros, la aplicación los sigue. | `config/parametros.yaml`, `reglas_negocio` en `params.yaml` |
| D-16 | El repositorio oficial queda completo y sin modificar en `reto_oficial/`; la aplicación lee sus datos desde allí. | Trazabilidad con la fuente y comparación directa con la referencia. | `rutas` en `params.yaml` |
| D-17 | La ficha sigue el esquema oficial (`ejemplos/ficha_ejemplo.json`). Los campos de trazabilidad del proyecto se agregan sin cambiar ni quitar campos oficiales. | El validador oficial debe leer la ficha tal cual. | `src/ficha/ficha.py` |
| D-18 | **Distancias con haversine** (radio 6.371.008,8 m) en la ficha; la misma medición en EPSG:9377 queda como control en cada ficha y en `auditoria/control_distancias.csv`. Decisión de Sebastián. | El cálculo manual oficial usa haversine y el validador tolera ±5 m. EPSG:9377 coincide con la distancia real a ±1 m; haversine la alarga hasta 10 m (INM-06: 1994 frente a 1984 m). | `entorno.metodo_distancia`, `entorno.control_proyectado` |
| D-19 | Deduplicación oficial: R1 mismo `id_anuncio`, R2 misma (`zona`, `area_m2`, `precio`), en el orden del CSV, conservando la primera fila. Cada fila absorbida queda con regla, fila de coincidencia y puntaje 1,0. | Regla oficial; la auditoría cumple la regla de trazabilidad del proyecto. | `deduplicacion` en ambos archivos |
| D-20 | La regla de duplicados por tolerancias (±3 % de área y precio) de la versión 0.2 deja de fusionar: solo lista candidatos para revisión. | La regla oficial es exacta; fusionar por tolerancia cambiaría el cálculo manual. | `deduplicacion.revision_tolerancias` |
| D-21 | Pedido y cierre por separado: la referencia usa cierres si hay al menos 3; si no, pedidos. Con la muestra actual (todo pedido) el resultado es idéntico al oficial. | Regla del proyecto; el archivo oficial acepta ambos tipos sin separarlos. | `precios` |
| D-22 | La flexibilización queda implementada pero inactiva. | La definición oficial de comparable no prevé ampliar la tolerancia; con los datos actuales ningún inmueble queda por debajo de 3 comparables. | `comparables.flexibilizacion.activa: false` |
| D-23 | El estado del inmueble y de los anuncios se normaliza como en el cálculo manual (minúsculas, sin tildes, sin "?", espacios simples). | Regla oficial. | `limpieza.py` |
| D-24 | `fecha_generacion` es la fecha del día salvo que se fije en `ejecucion.fecha_generacion`; la hora vive en el manifiesto. | Campo oficial de la ficha; dos corridas del mismo día dan fichas idénticas. | `ejecucion.fecha_generacion` |
| D-25 | La solución del notebook de Colab usa solo biblioteca estándar, PyYAML y pandas, y se verifica contra la aplicación. | Requisito del entregable en Colab; tres implementaciones independientes coinciden. | `notebooks/reto_ficha_colab_solucion.py` |
| D-26 | `.gitattributes` fija LF en el código y deja los datos sin conversión. | La huella sha256 de los archivos de entrada debe ser la misma en Windows y en Colab. | `.gitattributes` |
| D-27 | La capa POT se guarda en `data/geo/pot_tratamientos.geojson` (no en `reto_oficial/`, que es solo lectura) y se descarga con `scripts/descargar_pot.py` desde la capa "Tratamiento POT vigente" del servidor de mapas de la Alcaldía, la que indica el README del reto. El campo de tratamiento se busca sin distinguir mayúsculas. | La capa usa `tratamiento` en minúscula y `parametros.yaml` dice `TRATAMIENTO`; así no se edita el archivo oficial. | `rutas.pot_geojson`, `pot_descarga`, `entorno.py` |
| D-28 | Las pruebas contra el cálculo manual y la referencia corren sin capa POT. | Son las condiciones en que Venture One hizo el cálculo manual. | `tests/conftest.py` |

## Fases 1 a 4 · base de la aplicación (14-nov-2026)

| # | Decisión | Estado en fase 5 |
|---|---|---|
| D-01 | La base es `ficha_v1` (propuesta de Opus) con 10 módulos en `src/ficha/`. | Vigente |
| D-02 | Pedido y cierre nunca se mezclan por defecto. | Vigente (D-21) |
| D-03 | Referencia con cierres si alcanzan el mínimo. | Vigente (D-21) |
| D-04 | La confianza se evalúa sobre la base elegida. | Vigente; regla de confianza oficial (sin requisito de cierres) |
| D-05 | Anuncios sin estado o tipo de precio se excluyen con motivo. | Vigente con los códigos oficiales (`estado_sin_clasificar`, `tipo_precio_no_aceptado`) |
| D-06 | Cada pareja de duplicados guarda puntaje y regla. | Vigente (D-19) |
| D-07 | Regla por `id_anuncio` repetido. | Vigente: es la R1 oficial |
| D-08 | Flexibilización escalonada activa. | Reemplazada por D-22 (inactiva) |
| D-09 | Capa sin CRS o fuera del área detiene el proceso. | Vigente; los GeoJSON oficiales son WGS84 por RFC 7946 |
| D-10 | CRS métrico EPSG:9377. | Vigente como CRS de control (D-18) |
| D-11 | `valor_indicativo` apagado. | Vigente |
| D-12 | La ficha no lleva hora de ejecución. | Vigente (D-24) |
| D-13 | Entorno conda `venture-one`, Python 3.12, conda-forge. | Vigente |
| D-14 | Salidas en `salidas/`, auditoría en `auditoria/`. | Vigente |

## Decisiones abiertas (requieren a la dirección de Venture One)

La aplicación aplica hoy la regla oficial en todos los casos y deja el efecto medido en `auditoria/perfil_datos.md`.

| # | Decisión | Dato que la motiva |
|---|---|---|
| A-02 | Tratamiento de anuncios sin `estado`. | 25 filas sin estado (21 % del CSV); 20 de 54 únicas en El Salvador quedan inválidas. |
| A-03 | Duplicados ambiguos: ids distintos con la misma zona, área y precio. | 3 grupos (2 en El Salvador, 1 en Ditaires). Por eso INM-01 e INM-03 dan el mismo mercado. |
| A-04 | Comparables entre municipios dentro de una zona. | La zona `ditaires_itagui` incluye un anuncio de Envigado. |
| A-05 | Capa POT: descarga y validación de la norma de INM-04 e INM-06. | Capa identificada: `ServiciosCiudad/MAP6_Indicadores/MapServer/7`, campos `codigo_tratamiento`, `tratamiento`, `categoria_tratamiento`, `clase_suelo`; CRS nativo EPSG:9377. El 15-nov-2026 todo el servidor de mapas, MapGIS 9, GeoMedellín y el catálogo respondían 503 desde el equipo de Sebastián y desde la nube; el sitio principal sí respondía. Al descargarla, INM-04 e INM-06 dejan de ser `pendiente_capa` y el validador oficial marcará esa diferencia hasta crear `validacion/esperado.json` con la norma confirmada en MapGIS. |
| A-08 | **Representante de un grupo de duplicados.** La regla oficial conserva la primera fila del CSV aunque no tenga estado. | 8 anuncios válidos quedan absorbidos por una fila sin estado. Con `representante: mas_completo`, 5 de 11 inmuebles cambian su mercado (INM-04: 9 → 10 comparables, mediana 4.375.000 → 4.310.788). |
| A-09 | Posibles republicaciones con precio ajustado. | 10 parejas casi iguales (±3 % de área y precio) que la regla exacta no agrupa; varias son comparables de INM-04 (REMODELAR:85 y :88; Bombona:10 y :15). |

## Resueltas en fase 5

| # | Decisión | Resolución |
|---|---|---|
| A-01 | Método de distancia y tolerancia del validador. | D-18: haversine en la ficha, EPSG:9377 como control. |
| A-06 | Valores definitivos de radio, tolerancia, mínimos y confianza. | Los del archivo oficial `parametros.yaml` v1.0 (D-15). |
| A-07 | Esquema final del JSON y plantilla de la ficha. | Esquema oficial más campos de trazabilidad (D-17); vista de lectura de la plantilla oficial. |

Cuando se resuelva una decisión abierta: pasarla a "Resueltas" con fecha, actualizar el parámetro y la prueba que la protege, y regenerar `auditoria/perfil_datos.md`.
