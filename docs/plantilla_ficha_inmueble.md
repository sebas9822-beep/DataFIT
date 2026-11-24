# Estructura de la ficha del inmueble (Markdown)

Es la ficha que recibe el cliente. La genera `src/ficha/plantilla_cliente.py` a partir del JSON de la
ficha; no calcula nada. Reglas: `config/reglas.yaml › fichas`.

## Dónde queda

```
fichas_clientes/
└── <NIT o cédula del cliente>/            solo dígitos
    ├── FICHA_<AAAAMMDD>_<COTIZACIÓN>_v1.md
    ├── FICHA_<AAAAMMDD>_<COTIZACIÓN>_v1.json
    └── FICHA_<AAAAMMDD>_<COTIZACIÓN>_v2.md   regenerar crea otra versión; la anterior se conserva
```

- `<AAAAMMDD>` es la fecha de la cotización (hora de Colombia); `<COTIZACIÓN>`, los 8 primeros caracteres de su id.
- El `.json` trae la ficha completa (todos los comparables y la trazabilidad). El `.md` es su lectura.
- La base operativa (`fichas`) guarda la ruta, la versión, cuál está vigente y la huella SHA-256 de
  cada archivo: si alguien edita el `.md`, la aplicación avisa que ya no coincide.

## Secciones

| # | Sección | Contenido | Sale de |
|---|---|---|---|
| — | **Encabezado YAML** | `ficha`, `version`, `generada_en`, `cotizacion`, `comercial_usuario`, `comercial_nombre`, `cliente_tipo_documento`, `cliente_documento`, `cliente_nombre`, `inmueble`, `barrio`, `base_mercado`, `confianza`, `comparables`, `mediana_cop_m2` | `cotizacion` y `mercado` |
| — | **Título y trazabilidad comercial** | Comercial que asesora, cliente (tipo y número de documento) y versión | `cotizacion` |
| 1 | **Inmueble** | Dirección y conjunto, barrio y comuna, características declaradas (las que aplican al tipo y las adicionales), **lo que busca el cliente**, CBML, propiedad horizontal, valor de referencia del suelo, coordenadas y su precisión | `inmueble`, `predio` |
| 2 | **Mercado** | Base usada, tipo de precio, criterio de comparables y **plazo de vigencia**, estadísticas, **confianza** con su motivo, pedido y cierre por separado, flexibilización, tabla de comparables con enlace y fecha, **oferta por tipo de inmueble solicitado**, **referencia histórica (no vigente)**, otras bases consultadas | `mercado`, `busqueda_cliente`, `referencia_historica` |
| 3 | **Entorno** | Estación más cercana, estaciones en el radio, equipamientos por categoría (radio de la ficha y barrido completo), método de distancia y control en EPSG:9377 | `entorno`, `trazabilidad.control_distancia` |
| 4 | **Norma urbanística (informativa)** | Tratamiento del POT con su código, categoría y clase de suelo, y la nota de alcance | `norma` |
| 5 | **Calidad de los datos** | Anuncios, duplicados (incluidos los repetidos entre sitios), válidos e inválidos por motivo; datos faltantes; fuentes con novedad | `calidad_datos_zona`, `datos_faltantes` |
| 6 | **Trazabilidad** | Cotización y huella de su evidencia, comercial y cliente, corrida del ETL, inmueble y predio en el almacén, huellas de parámetros, reglas (con la tolerancia de los repetidos entre sitios) y fuentes | `trazabilidad`, `metadatos` |
| — | **Aviso** | "Ficha descriptiva… No constituye evaluación ni recomendación de inversión, ni concepto jurídico…" | `aviso` |

## Lo que la ficha siempre dice

1. **Quién y para quién:** el comercial que asesora y el cliente que la recibe, en el encabezado, en la
   tabla inicial y en la trazabilidad.
2. **De dónde sale cada cifra:** la base de mercado, el criterio de comparables, las fuentes y la corrida
   del ETL que cargó los datos.
3. **Qué no se pudo saber:** los datos faltantes con nombre propio (precios de cierre, estado de los
   comparables, fecha de publicación…). Un dato que no existe queda como "sin dato", nunca inventado.
4. **Qué no es:** no es un avalúo, no recomienda comprar y no es un concepto de norma.

## Estados de una ficha

| Estado | Qué significa | Qué hacer |
|---|---|---|
| `ok` | Hay comparables y la ficha está completa | — |
| `sin_mercado` | Ninguna oferta vigente cumple el criterio | La ficha trae entorno, norma y la referencia histórica. Encender las inmobiliarias y volver a buscar |
| `incompleta` | Faltan el tipo o el área del inmueble | Completar los datos en la pantalla de la cotización; se genera una versión nueva |

## Campos propios de la aplicación única en el JSON

Además del esquema oficial del reto (`reto_oficial/docs/plantilla_ficha.md`), que no cambia:

| Campo | Contenido |
|---|---|
| `cotizacion` | Ficha y versión, cotización, comercial, cliente, captura y huella de la evidencia |
| `predio` | CBML, propiedad horizontal, lote, pisos, valor de referencia del suelo, estación masiva más cercana |
| `mercado.base_mercado`, `mercado.bases` | Base principal y resumen de cada base consultada, con el motivo si no aplicó |
| `mercado.vinculo`, `mercado.radio_m` | Si los comparables entran por zona o por radio |
| `mercado.comparables[].distancia_m`, `.fecha`, `.barrio`, `.url` | Lo que el almacén sabe de cada comparable |
| `mercado_otras_bases` | El bloque de mercado completo de las bases que no fueron la principal |
| `mercado.vigencia` | Plazo en meses, fecha de corte, cuántas ofertas quedaron fuera y cómo se trató lo que no tiene fecha |
| `mercado.comparables[].vista_el` | Día en que se vio publicado un anuncio que no muestra su fecha de publicación |
| `mercado.repetidos_entre_sitios` | Cuántos anuncios se enlazaron como el mismo inmueble publicado en otro sitio y con qué tolerancia de área y de precio. La ficha lo dice en los avisos del mercado |
| `trazabilidad.regla_deduplicacion.entre_sitios` | La regla aplicada: tolerancias, si exige el mismo barrio y qué datos deben coincidir cuando existen |
| `referencia_historica` | Por base: el mismo cálculo sin la regla de vigencia (n, mediana, mínimo, máximo, CV y entre qué fechas se publicaron). No son comparables |
| `busqueda_cliente` | Tipos que pidió el cliente, cuál se cotiza y, por tipo, ofertas vigentes e históricas con su mediana |
| `inmueble.caracteristicas` | Características adicionales que anotó el comercial |
| `entorno.lugares_por_categoria` | Conteo por categoría de la captura: en el radio de la ficha y en el barrido |
| `trazabilidad.almacen` | Inmueble, predio, corrida del ETL, versión del esquema y huellas |
