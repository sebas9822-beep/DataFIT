# Modelo de datos de la base operativa

> Esta es la base donde la aplicación **escribe** (`data/captura.db`). El análisis no se hace aquí: el ETL la
> lleva al almacén en esquema estrella (`data/almacen.db`), descrito en `docs/modelo_estrella.md`.

Base: `data/captura.db` (SQLite). El esquema está en `src/captura/bd.py` y se crea solo al arrancar.
Todas las coordenadas se guardan en **EPSG:4326** (`lat`, `lng`) y, cuando se pudieron proyectar,
también en **EPSG:9377** (`x_9377`, `y_9377`), que es el sistema nativo que declaran las capas de
Medellín. Las distancias están en metros.

## Tablas

| Tabla | Una fila es… | Para qué sirve en la fase siguiente |
|---|---|---|
| `capturas` | Una captura: la dirección, el punto, el conjunto, el estado y la huella de configuración | Punto de partida de cada ficha |
| `predios` | El predio de la captura: lote, construcción, barrio, norma POT, valor de referencia, cuadrante, estación más cercana | Sección de predio y norma |
| `entorno_items` | Un registro de una fuente dentro del radio (un colegio, un paradero, un establecimiento) | Entorno, conteos y distancias |
| `ofertas` | Una oferta inmobiliaria histórica dentro del radio | Referencia de mercado (precio pedido) |
| `indicadores` | Un indicador calculado de la captura | Lectura rápida del entorno |
| `resumen_categoria` | Conteo por categoría y fuente | Control de cobertura por fuente |
| `ejecuciones` | Una consulta a una capa, con su estado | Trazabilidad: qué respondió y qué falló |
| `curaduria_log` | Un cambio hecho o propuesto a un dato | Auditoría de la depuración |
| `cola_revision` | Un par de registros que podría ser el mismo lugar y no se resolvió solo | Decisión humana pendiente |
| `fuentes` | Una fuente del catálogo, con su licencia | Atribución |

**Vista `v_entorno_curado`**: un registro por lugar real (el maestro de cada entidad), sin excluidos.
Es la tabla que conviene leer cuando solo se necesitan los lugares ya depurados.

## Comerciales, clientes y cotizaciones

| Tabla | Una fila es… | Campos clave |
|---|---|---|
| `comerciales` | Un usuario que puede entrar | `usuario`, `nombre`, `clave_hash` + `sal` (PBKDF2; nunca la clave), `activo`, `clave_cambiada_en` (NULL = sigue con la inicial) |
| `sesiones` | Una sesión abierta | `token_sha256` (huella, no el token), `usuario`, `expira_en`, `cerrada_en` |
| `accesos` | Un evento de acceso | `usuario`, `evento` (`entrada`, `clave_errada`, `bloqueado`, `salida`, `clave_cambiada`), `creado_en` |
| `clientes` | Un cliente, identificado por su cédula | `documento` (único, solo dígitos), `nombre` (el último registrado), `autoriza_datos`, `autorizado_en`, `autorizado_por`, `texto_autorizacion_sha256`, `creado_por` |
| `cotizaciones` | Una captura pedida por un comercial para un cliente | `comercial`, `cliente_id`, `cliente_nombre` y `cliente_documento` (copia de ese momento), `captura_id`, `reusada`, `estado` (`en_curso`, `ok`, `parcial`, `error`), `mensaje`, `evidencia_json` + `evidencia_sha256`, `creada_en`, `terminada_en` |

**Vista `v_cotizaciones`**: una fila por cotización con el nombre del comercial, el cliente, la
dirección catastral, el barrio, la comuna y el CBML. Es la base de la agrupación visual.

Para contar, usa solo los estados de `reglas.yaml › cotizaciones.estados_que_cuentan` (hoy `ok` y
`parcial`): una cotización en `error` queda registrada con su motivo, pero no suma.

```sql
-- Cotizaciones por comercial
SELECT comercial_nombre, COUNT(*) AS cotizaciones, COUNT(DISTINCT cliente_id) AS clientes
FROM v_cotizaciones WHERE estado IN ('ok', 'parcial')
GROUP BY comercial ORDER BY cotizaciones DESC;

-- Cotizaciones por cliente
SELECT cliente_nombre, COUNT(*) AS cotizaciones, MAX(creada_en) AS ultima
FROM v_cotizaciones WHERE estado IN ('ok', 'parcial')
GROUP BY cliente_id ORDER BY cotizaciones DESC;

-- Por día y comercial
SELECT fecha, comercial, COUNT(*) FROM v_cotizaciones
WHERE estado IN ('ok', 'parcial') GROUP BY fecha, comercial;
```

## Campos clave de `entorno_items`

| Campo | Significado |
|---|---|
| `fuente`, `capa`, `id_externo` | De dónde salió el registro. Juntos lo identifican dentro de la captura |
| `categoria`, `subtipo` | Clasificación propia (la taxonomía de `config/taxonomia.yaml`) |
| `categoria_original` | El tipo tal como lo traía la fuente |
| `confianza_clasificacion` | `alta`, `media` o `baja`: qué tan directa fue la regla que clasificó |
| `nombre_original`, `nombre`, `nombre_clave` | Nombre como llegó, nombre depurado y forma reducida para comparar |
| `distancia_m`, `anillo_m` | Distancia al punto y anillo de caminabilidad en el que cae |
| `geometria` | `punto` o `poligono` (en polígonos la distancia es al borde más cercano) |
| `entidad_id`, `es_maestro` | Registros del mismo lugar comparten `entidad_id`; uno solo es el maestro |
| `regla_dup`, `puntaje_dup`, `coincide_con` | Por qué se enlazó: regla, puntaje (0 a 1) y con qué registro |
| `motivo_exclusion` | Si no es nulo, el registro está guardado pero no cuenta, y aquí dice por qué |
| `banderas_json` | Avisos de calidad: `sin_nombre`, `clasificacion_dudosa`, `dato_antiguo`, `centroide_poligono`, `ajustado_por_ia` |
| `completitud` | Fracción de campos básicos presentes (0 a 1) |
| `curado_por` | `regla` o `claude` |
| `en_mapa` | 1 = lugar que cuenta en su categoría; 0 = capa masiva (establecimientos, cámaras) que se resume aparte |
| `licencia`, `obtenido_en`, `expira_en` | Condiciones de uso, fecha de captura y vencimiento (solo Google) |

## Campos clave de `ofertas`

`tipo_precio` siempre dice si el precio es **`pedido`** o `cierre`. Hoy toda la capa OIME es de precio
pedido. `area_usada` y `area_tipo_usada` indican con qué área se calculó `precio_m2`.
`anio` permite separar la oferta histórica de cualquier oferta vigente que se sume después.
La capa de origen trae teléfonos, agencias y descripciones libres: esos campos no se piden ni se guardan.

## Motivos de exclusión que existen hoy

| Motivo | Dónde | Qué significa |
|---|---|---|
| `sin_coordenadas` | entorno | La fuente no entregó geometría utilizable |
| `sin_establecimiento_fisico` | establecimientos | Contribuyente registrado sin local ("NO POSEE ESTABLECIMIENTOS") |
| `sin_coordenadas`, `sin_precio`, `sin_area`, `area_fuera_de_rango` | ofertas | La oferta no permite ubicarla o calcular un precio por m² confiable |

Lo que cae fuera del radio no se guarda: queda contado en `ejecuciones.descartados` de su capa.

## Reglas de Google que el esquema hace cumplir

- `nombre`, `nombre_original`, `nombre_clave` y `categoria_original` son siempre nulos en filas de Google.
- `lat`, `lng`, `subtipo` y `atributos_json` se borran al vencer `expira_en` (30 días). Quedan el
  `place_id` (en `id_externo`) y la categoría propia.
- Los indicadores tienen dos bases: `abiertas` (sin Google, permanente) y `todas` (con Google, vence).

## Consultas útiles

```sql
-- Lugares depurados de una captura, del más cercano al más lejano
SELECT categoria, subtipo, nombre, ROUND(distancia_m) AS m, fuente
FROM v_entorno_curado WHERE captura_id = :id AND en_mapa = 1 ORDER BY distancia_m;

-- Qué se enlazó como duplicado y por qué
SELECT nombre, fuente, coincide_con, regla_dup, ROUND(puntaje_dup, 2) AS puntaje
FROM entorno_items WHERE captura_id = :id AND es_maestro = 0;

-- Qué cambió Claude y qué se le rechazó
SELECT item_ref, campo, valor_antes, valor_despues, regla, aceptado
FROM curaduria_log WHERE captura_id = :id AND origen = 'claude';

-- Capas que no respondieron bien
SELECT fuente, capa, estado, mensaje FROM ejecuciones
WHERE captura_id = :id AND estado NOT IN ('ok', 'omitida');
```

## Aplicación única: inmueble, fichas, metas y seguimiento

La base de la fase 1 se conserva. Al arrancar, `bd.migrar` agrega lo que falte sin tocar los datos.

| Tabla o columna | Qué guarda |
|---|---|
| `comerciales.rol`, `comerciales.supervisa_json` | `comercial` o `supervisor`, y a quién supervisa (`"todos"` o una lista) |
| `clientes.tipo_documento` | `CC`, `NIT` o `CE`. El número sigue siendo único y nombra la carpeta de fichas |
| `cotizaciones.inm_tipo`, `inm_area_m2`, `inm_estado`, `inm_habitaciones`, `inm_banos`, `inm_parqueaderos` | Datos del inmueble que declaró el comercial |
| `cotizaciones.cliente_tipo_documento` | Copia del tipo de documento al momento de cotizar |
| `clientes.celular` | Celular del cliente. Solo aquí: enmascarado en pantalla y fuera del almacén |
| `cotizacion_intereses` | Lo que busca el cliente: una fila por tipo de inmueble pedido en la cotización, con su orden y su origen (`declarado` o `tipo_cotizado`). Solo se agregan filas |
| `cotizaciones.inm_extras_json` | Características adicionales del inmueble (`[{nombre, valor}]`) |
| `cotizaciones.portales_json`, `rpa_json` | Inmobiliarias encendidas al cotizar y resultado de la búsqueda (estado, mensaje, por sitio, consulta, duración) |
| `ejecuciones` con `fuente = 'portales'` | Una fila por sitio leído en cada búsqueda: estado, anuncios útiles, descartados y lo que dijo el lector |
| `ofertas` con `fuente = 'portales'` | Anuncios leídos por el RPA: `capa` = sitio, `url`, `tipo_precio = 'pedido'`, `fecha_publicacion` si el sitio la muestra y `obtenido_en` |
| `ofertas.regla_dup`, `puntaje_dup`, `coincide_con` | Anuncio repetido entre sitios, marcado por el paso "Depuración de anuncios": la regla (`R3_entre_sitios`), el parecido (1,0 = iguales) y el anuncio que se conserva (`portales:<id_externo>`). Vacías = no es repetido. Se recalculan en cada búsqueda |
| `cotizaciones.rpa_json › depuracion` | Resumen del paso: anuncios válidos, repetidos entre sitios, únicos, pares enlazados (con sus enlaces), regla aplicada y cómo quedan frente a la vigencia |
| `fichas` | Una fila por versión de ficha: cotización, versión, `vigente`, estado, rutas del `.md` y el `.json`, huellas SHA-256, resumen y corrida del ETL |
| `gestion_cliente` | Estado de la ficha con el cliente. Una fila por cada cambio que registra el comercial: estado, tipo de motivo y motivo (si el estado lo exige), nota, versión de la ficha y fecha. Solo se agregan filas; vale la última. Sin filas, la ficha está en el estado inicial (`por_entregar`) |
| `comerciales.editado_en`, `eliminado_en`, `creado_por` | Gestión del administrador. `editado_en` con valor = para ese usuario manda la base y `usuarios.yaml` ya no lo sincroniza. `eliminado_en` = eliminado: no entra, su historial se conserva |
| `comerciales.rol` | Ahora también `admin` |
| `admin_bitacora` | Qué hizo el administrador: acción, sobre qué (usuario, sitio o archivo), detalle con el antes y el después, y cuándo. Nunca guarda una clave |
| `metas` y `metas_historial` | Meta de cotizaciones y de clientes por comercial y mes; cada cambio queda en el historial |
| `seguimientos` | Notas del supervisor a un comercial: tipo, nota, compromiso, estado y quién lo cerró |

```sql
-- Fichas vigentes con su comercial y su cliente
SELECT f.ficha_id, f.estado, f.ruta_md, q.comercial, q.cliente_nombre
FROM fichas f JOIN cotizaciones q ON q.cotizacion_id = f.cotizacion_id WHERE f.vigente = 1;
```
