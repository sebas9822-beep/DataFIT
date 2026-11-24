# Contexto del proyecto

**Cliente:** Venture One S.A.S. (Medellín) — "Activos urbanos. Rentabilidad medible."
**Proyecto:** DataFIT · reto de la ficha automática de mercado y entorno inmobiliario.
**Esta aplicación:** fase 1, la **captura**. Reúne y deja depurados los datos de un predio y de su
entorno a 1 km. No produce la ficha ni selecciona comparables: eso es de la fase siguiente, que
lee la base que esta deja.

## Qué significa cada cosa aquí

| Término | En esta aplicación |
|---|---|
| Captura | Una consulta completa sobre un punto: ubicación, predio, barrido, depuración y guardado |
| Punto | La coordenada del inmueble, con su origen y su precisión (P1 placa exacta, P2 punto o placa cercana, P3 aproximada) |
| Registro | Un dato de una fuente dentro del radio (un colegio de GeoMedellín, un paradero, un establecimiento) |
| Lugar o entidad | El sitio real. Dos registros de fuentes distintas pueden ser el mismo lugar |
| Maestro | El registro que representa a un lugar cuando varias fuentes lo reportan |
| Categoría | Una de las diez de `taxonomia.yaml`: educación, salud, parques, cultura, seguridad, movilidad, abastecimiento, empresas, edificios y conjuntos, otros equipamientos |
| Capa masiva | Capa con miles de registros por kilómetro (establecimientos, cámaras): se guarda completa y se resume aparte |
| Curaduría | Depuración: normalizar nombres, clasificar, enlazar duplicados y marcar lo que no sirve |

## Documentos de origen

Los tres conceptos unificados que definieron el alcance (carpetas `Google`, `Datos` y `RPA` de
`Aplicación vfinal`):

- *Buscador de predios (RPA)*: niveles de precisión P1–P5, estados de ejecución, cero evasión de
  bloqueos, ficha de viabilidad por fuente.
- *Barrido de entorno a 1 km*: categorías, tipos de Google Places, subdivisión cuando una consulta
  se satura, reglas de almacenamiento de Google.
- *Fuentes de la ficha*: catálogo de capas (C1 oferta, C2 catastro, C3 entorno), licencias y
  datos personales.

Marca: `contenido_extra_do_venture_one.md` y la plantilla `visor_fichas.html`.

## Flujo de una captura

1. **Ubicación.** Nomenclatura oficial → Google (si hay llave) → Nominatim. El usuario puede
   marcar o arrastrar el punto.
2. **Predio y norma.** Barrio, lote, construcción, placa más cercana, valor de referencia del
   suelo, tratamiento y uso del POT, patrimonio, cuadrante y estación más cercana.
3. **Barrido.** Todas las capas activas de `fuentes.yaml` en paralelo, más OpenStreetMap y Google.
4. **Depuración por reglas.** Nombres, clasificación, duplicados, completitud y banderas.
5. **Guardado.** Todo queda en la base antes de llamar a Claude.
6. **Curaduría con Claude.** Solo lo dudoso; cada propuesta se valida y queda en la bitácora.
7. **Indicadores.** Conteos por anillo, distancias mínimas, cobertura básica y diversidad.

Si Claude no está disponible o falla, la captura queda completa con las reglas y lo declara.

## Límites de alcance (obligatorios)

- La aplicación **no recomienda** comprar, vender ni invertir, y no estima rentabilidad.
- El cruce con el POT **no es** un concepto jurídico ni urbanístico.
- El valor de la zona geoeconómica es una **referencia oficial del suelo**, no un precio de mercado.
- Las ofertas del OIME son **precio pedido** e históricas; nunca se presentan como cierre.
