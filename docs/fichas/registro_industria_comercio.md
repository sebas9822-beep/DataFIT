# Ficha de fuente · Registro de Industria y Comercio (establecimientos)

| | |
|---|---|
| Servicio | `ccio_ind_turism/VA_Establecimiento_Comercial`, capa 6 |
| Productor | Secretaría de Hacienda, Alcaldía de Medellín |
| Acceso | ArcGIS REST público, sin autenticación |
| CRS nativo | EPSG:9377 (se consulta con entrada y salida en EPSG:4326) |
| Verificado | 20 y 21-nov-2026 |
| Filtro | `accion='A' AND fecha_cancelacion IS NULL` (establecimientos activos) |
| Volumen | 14.664 activos a 1 km de CR 43A # 1-50 (El Poblado) |

## Cómo se usa

Es una capa masiva: se guarda completa en `entorno_items` con `en_mapa = 0` y se resume aparte
(conteo, densidad por km² y reparto por categoría según el código CIIU). No entra a los conteos
de las categorías ni al mapa.

## Particularidades comprobadas

- **Contribuyentes sin local.** Cerca de un tercio de las filas dicen "NO POSEE ESTABLECIMIENTOS".
  Se guardan con `motivo_exclusion = sin_establecimiento_fisico` y no cuentan como establecimiento.
- **Filas repetidas.** El mismo contrato aparece dos veces con datos idénticos (99 filas en la
  prueba). Se agrupan por `id_contrato` y queda dicho en el estado de la capa.
- **Fecha de inicio.** Algunas filas tienen una `fecha_inicio` que el servidor no logra entregar
  y hace fallar la página completa. Por eso el campo no se pide (decisión D-23, abierta A-11).
- **Fecha de registro.** Es la fecha de carga del lote, igual para miles de filas: no se pide.
- **Paginación.** El desplazamiento cuenta filas antes del filtro de distancia; la capa se lee
  por llave (D-21).

## Limitaciones

- Es un registro tributario: un establecimiento registrado puede haber cerrado sin cancelar.
- La ubicación sale de la dirección declarada; puede no coincidir con la puerta del local.
- La clasificación por CIIU depende del código que declaró el contribuyente.
