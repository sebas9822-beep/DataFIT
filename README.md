# Venture One · Aplicación única: captura, almacén y ficha del inmueble

Una sola aplicación local que reúne los dos momentos del proyecto:

- **Momento 1 · Captura.** El comercial registra al cliente, ubica el predio y la aplicación reúne y
  depura lo que hay a 1 km: catastro, norma (POT), equipamientos, transporte, comercio y ofertas.
- **Momento 2 · Ficha del inmueble.** Con los datos ya depurados y cargados a un almacén en
  **esquema estrella**, el motor de ficha calcula el precio por m² de comparables, la confianza,
  el entorno y la norma, y deja la ficha en Markdown en la carpeta del cliente.

Entre los dos hay un **ETL** (extracción, transformación y carga) que es el centro de la aplicación:
lo que alimenta la ficha y el tablero comercial sale siempre del almacén, nunca de datos sin depurar.

```
 base operativa (captura.db)                almacén (almacen.db, esquema estrella)
 capturas · predios · entorno   ──┐
 ofertas · clientes · cotizaciones ├─ ETL ─►  dimensiones conformadas + hechos  ──►  ficha del inmueble (.md / .json)
 insumos del reto (CSV, GeoJSON) ──┘          bitácora, rechazos y controles      ──►  tablero de supervisión
```

La aplicación describe datos. No recomienda comprar ni invertir, y el cruce con el POT es
informativo: no reemplaza un concepto de norma urbanística.

## El caso y cómo lo resuelve la aplicación

**El reto.** Venture One SAS compra, renueva y vende o arrienda inmuebles en Medellín (reto empresarial 13 de
EstudIA). Antes de decidir una compra, su equipo de inversión arma a mano la información de cada oportunidad:
busca comparables uno por uno en portales, consulta MapGIS y otros geoportales, toma capturas y lo consolida en
hojas de cálculo cuyo formato cambia de un análisis a otro. El caso pide una **ficha automática por inmueble** con
el precio por m² de comparables según su estado, el transporte, los equipamientos y la norma urbanística, que diga
además qué datos faltan y cuánta confianza merece cada resultado. **La decisión de inversión queda fuera del
alcance:** la ficha prepara la evidencia; la decisión sigue en manos del equipo de Venture One.

Las tres lecturas del caso que guiaron el diseño coinciden en el fondo: el problema no es dibujar un mapa ni sacar
un promedio. Es convertir un proceso manual, que depende de quien lo hace, en uno reproducible y trazable que
aplique el mismo criterio a todos los inmuebles.

### De cada limitación a lo que hace la aplicación

El caso nombra cuatro limitaciones del proceso actual. Cada una corresponde a uno de los propósitos del proyecto.

| Lo que señala el caso | Propósito | Cómo lo resuelve la aplicación | Dónde verlo |
|---|---|---|---|
| Es lento: consolidar a mano limita cuántas oportunidades se analizan, y las buenas duran poco | Operativo | Un proceso automático de punta a punta. Las 11 fichas oficiales salen en 2,6 s. En uso real, una cotización completa (entorno a 1 km, anuncios del día y ficha) tardó 182 s | `iniciar.bat reto` · "El orden del proceso" |
| No deduplica ni clasifica los comparables de forma consistente | Analítico | Reglas fijas y escritas: estados normalizados, duplicados por identificador y por zona, área y precio, y el mismo inmueble publicado en dos sitios enlazado como uno. Dos corridas sobre los mismos datos dan fichas idénticas byte a byte | `config/parametros.yaml` · `src/ficha/deduplicacion.py` · `src/captura/curaduria/anuncios.py` |
| No deja registro de los datos faltantes | Control | Nada se borra: cada anuncio fuera del cálculo lleva su `motivo_exclusion` y cada duplicado su regla, su puntaje y con cuál coincide. Lo que falta dice "sin dato"; nunca se inventa. Cada corrida del ETL concilia leídos = cargados + duplicados + rechazados, y la ficha trae las fuentes, los parámetros y su huella | `auditoria/` · tablas `fact_oferta` y `etl_rechazo` · bloque `trazabilidad` de la ficha |
| Depende de la persona que hace el análisis | Estratégico | El criterio que hoy vive en las personas queda escrito como parámetros que se cambian sin tocar código: qué es comparable, cuánta diferencia de área se tolera, qué radio se mira, cuántos datos bastan. Cada decisión de criterio queda registrada con su motivo y su efecto | `config/*.yaml` · `auditoria/decisiones.md` |

### Del inmueble a la ficha

| Paso que pide el caso | Qué hace | Dónde |
|---|---|---|
| Recibir el inmueble y los archivos | Lee inmuebles, anuncios, capas y parámetros; revisa columnas, tipos, coordenadas, áreas y precios | `src/ficha/carga.py`, `validacion.py` · en uso real, la captura (`src/captura`) |
| Normalizar | Unidades, barrios, estado del inmueble y tipo de precio (pedido o cierre) | `src/ficha/limpieza.py` · `src/almacen/normalizacion.py` |
| Deduplicar | Reglas oficiales; el duplicado se enlaza al que se conserva, no se borra | `src/ficha/deduplicacion.py` |
| Seleccionar comparables | Misma zona y mismo estado, área ±30 %, pedido y cierre por separado | `src/ficha/mercado.py` · `parametros.yaml › comparables` |
| Calcular el mercado | Mediana, rango y número de comparables del precio por m² | `src/ficha/mercado.py` |
| Calcular el entorno | Estación más cercana y su distancia, equipamientos por categoría a 800 m y polígono del POT cuando hay capa. Distancias con haversine y control en EPSG:9377 | `src/ficha/entorno.py` · `entorno_almacen.py` |
| Medir la confianza | Alta, media o baja según cuántos comparables hay y cuánto se dispersan sus precios. Mide la calidad de los datos, nunca la rentabilidad | `src/ficha/confianza.py` · `parametros.yaml › confianza` |
| Emitir la ficha | JSON con el contrato oficial y Markdown para leer. El JSON es la fuente; el Markdown, una vista | `src/ficha/ficha.py` · `desde_almacen.py` |
| Validar | Contra el cálculo manual de los 3 inmuebles y contra la implementación de referencia del reto | `reto_oficial/validacion/` · `scripts/comparar_con_referencia.py` |

### Cómo se comprobó

| Criterio de aceptación | Resultado |
|---|---|
| Procesa los inmuebles de prueba sin intervenir registro por registro | 11 de 11 fichas |
| Reproduce el cálculo manual de los 3 inmuebles | Validador oficial 3/3 |
| Coincide con la implementación de referencia del reto | 11/11 en todos los campos oficiales |
| Identifica y documenta duplicados e inválidos | 117 anuncios = 63 válidos + 33 duplicados + 21 inválidos, cada uno con su regla o su motivo |
| No inventa lo que falta | "Sin dato"; la ficha baja la confianza o queda `incompleta` o `sin_mercado` |
| Distingue precio pedido y precio de cierre | Se calculan y reportan por separado (la muestra solo trae precios pedidos) |
| Reglas visibles y editables sin tocar código | `config/parametros.yaml`, el archivo oficial sin cambios |
| Es reproducible | Dos corridas idénticas byte a byte |
| No recomienda comprar | Aviso fijo en cada ficha y una prueba que revisa el lenguaje del código, la interfaz y las fichas |
| Pruebas automáticas | 203 en verde, 1 omitida (solo aplica en Linux) |

Queda pendiente:

- **Norma del POT.** La capa de tratamientos también está pendiente en el repositorio oficial del reto: 8 fichas
  dicen `pendiente_capa` y 3 `no_disponible`. La ficha nunca supone una norma que la capa no compruebe.
- **Google Colab.** `notebooks/reto_ficha_colab_solucion.ipynb` produce las mismas 11 fichas y el 3/3 en un Linux
  limpio; falta correrlo en Colab.
- **Línea base.** No hay una medida de cuánto tarda hoy una ficha manual, así que el ahorro de tiempo todavía no se
  puede cuantificar.

### Lo que respeta y lo que va más allá del caso

El **núcleo del reto** (`iniciar.bat reto` o `python -m ficha.pipeline`) trabaja solo con los archivos entregados:
sin scraping, sin datos personales, sin sistemas internos y sin recomendar compra.

La aplicación completa agrega tres extensiones que el caso no pidió y que quedan **a decisión de la dirección** de
Venture One:

1. **Capa comercial.** Comerciales que registran al cliente (el inversionista privado) y cotizan; supervisores con
   tablero, metas y seguimiento; un administrador; el estado de la ficha con el cliente y una hoja de una página para
   entregarle.
2. **Anuncios vigentes de inmobiliarias.** La muestra del reto es fija y en uso real los comparables tienen que ser
   recientes (máximo 6 meses). Claude Code lee solo los sitios encendidos del catálogo, se detiene ante un bloqueo y
   no recibe datos del cliente. Esto va más allá de la condición "sin scraping" del reto, y los términos de uso de
   cada sitio siguen por revisar (AU-14).
3. **Datos del cliente.** Documento, nombre y celular, con su autorización. Viven solo en la base local: el almacén
   guarda el documento enmascarado y con huella, el celular no sale de la base, y nada de eso va a Claude ni a este
   repositorio.

### Lo que falta definir con Venture One

- **Los parámetros son de ejemplo.** El propio archivo oficial lo dice: ±30 % de área, 800 m de radio, confianza alta
  con 5 comparables y coeficiente de variación hasta 15 %, media con 3 y hasta 25 %. La dirección debe confirmarlos
  o cambiarlos.
- **Reglas que mueven el resultado.** Qué anuncio conservar en un grupo de duplicados: con "el más completo" en vez
  de "el primero del archivo", el inmueble INM-04 pasa de 9 a 10 comparables y su mediana de 4.375.000 a
  4.310.788 COP/m². También si se fusionan las posibles republicaciones y si en uso real se filtra por estado (AU-01).
- **La línea base** de tiempo por ficha y de oportunidades analizadas por mes, para medir el beneficio.

El detalle está en `auditoria/decisiones.md` y en `auditoria/heredadas/decisiones_momento2_ficha_v1.md`.

## Cómo arrancar

**Doble clic en `iniciar.bat`.** Se abre el navegador en `http://127.0.0.1:8765`.

Si la aplicación de la fase 1 sigue abierta, ciérrala antes: usa el mismo puerto y lo que se capture allá
no llega a esta base. `iniciar.bat verificar` avisa cuando el puerto está ocupado por otra copia.

| Orden | Qué hace |
|---|---|
| `iniciar.bat` | Abre la aplicación (un segundo doble clic muestra la que ya está abierta) |
| `iniciar.bat verificar` | Revisa el equipo, el almacén, la sesión de Claude y cada fuente |
| `iniciar.bat etl` | Corre el ETL completo y deja el almacén al día |
| `iniciar.bat reto` | Genera las 11 fichas oficiales del reto **desde el almacén** y corre el validador oficial |
| `iniciar.bat fichas` | Genera las fichas de las cotizaciones que aún no la tienen |
| `iniciar.bat rebuscar` | Busca de nuevo en las inmobiliarias y rehace las fichas anteriores |
| `iniciar.bat inmobiliarias` | Prueba qué sitios del catálogo se dejan leer |
| `iniciar.bat probar` | Pruebas automáticas y revisión de las fuentes en vivo |
| `iniciar.bat clave comercial1` | Cambia la clave de un usuario (comercial, supervisor o administrador) |

Usa el entorno conda `venture-one` (Python 3.12). No hay que instalar nada adicional.

**Desde una copia nueva de este repositorio** (Anaconda Prompt, en la carpeta del repositorio):

```powershell
conda create -n venture-one -c conda-forge python=3.12 -y
conda activate venture-one
pip install -r requirements.txt
iniciar.bat reto      # 11 fichas oficiales desde el almacén y validador oficial
iniciar.bat           # la aplicación completa
```

Las bases (`data/*.db`), las fichas de clientes (`fichas_clientes/`), los respaldos (`respaldos/`), las salidas
y el `.env` no están en el repositorio: tienen datos de clientes o llaves. La aplicación crea las bases vacías al
arrancar. Para las llaves opcionales, copia `.env.example` como `.env`.

## Quién entra y qué ve

| Rol | Usuarios | Qué puede hacer |
|---|---|---|
| **Comercial** | `comercial1` … `comercial10` | Registrar clientes, cotizar y ver **solo sus** cotizaciones, capturas y fichas. Ve su meta del mes y el seguimiento que le dejó el supervisor |
| **Supervisor** | `supervisor1`, `supervisor2` | Tablero comercial con gráficas, todas las cotizaciones y fichas, clientes, metas, seguimiento y el panel de calidad de datos. No cotiza |
| **Administrador** | `admin` | Usuarios, catálogo de inmobiliarias, respaldos, bitácora de cambios y resumen de la plataforma. **No ve cotizaciones ni clientes** |

La clave inicial es igual al usuario (para pruebas). `config/usuarios.yaml` trae los usuarios con los que
arranca la aplicación; de ahí en adelante se gestionan en la pantalla del administrador, y para un usuario
que él creó, cambió o eliminó manda la base. La cotización de otro comercial responde "no encontrada": ni
siquiera se confirma que exista.

## La administración

Entra el usuario `admin`. Tiene cinco vistas:

- **Resumen:** usuarios activos, cuántos siguen con la clave inicial, inmobiliarias encendidas, capturas que
  quedaron sin cerrar y fecha del último respaldo. Sin datos de clientes.
- **Usuarios:** crear, cambiar nombre, rol y a quién supervisa, restablecer la clave, eliminar y reactivar. Un
  usuario eliminado deja de entrar y conserva su historial. No se puede dejar la plataforma sin administrador.
- **Inmobiliarias:** agregar, cambiar, encender o apagar y eliminar sitios del catálogo. Un sitio eliminado pasa a
  descartados con su motivo. El cambio se escribe en `config/inmobiliarias.yaml` tocando solo ese sitio, deja copia
  del archivo anterior en `config/historial/` y los comerciales lo ven sin reiniciar. Un sitio nuevo queda como
  "sin probar" hasta que corra `iniciar.bat inmobiliarias`.
- **Respaldos:** un `.zip` en `respaldos/` con la base operativa, la configuración y las fichas. El `.env` no se
  copia. Para recuperarlo: cerrar la aplicación, copiar `data/captura.db`, `config/` y `fichas_clientes/` desde el
  `.zip` y correr `iniciar.bat etl`. Los respaldos tienen datos de clientes: guárdalos fuera del equipo.
- **Bitácora:** cada cambio, con quién lo hizo. Nunca guarda una clave.

## Entregar la ficha al cliente

- **Imprimir para el cliente.** En la ficha, el botón abre una hoja de una página lista para imprimir o guardar
  como PDF. Trae el inmueble, el precio de referencia por m² y su equivalente para el área del inmueble, qué tan
  firme es esa referencia, lo que hay cerca y, en una frase, cómo se obtuvo. No muestra nada del procesamiento
  (corridas, huellas, duplicados, bases) ni el documento del cliente. Describe el mercado: **no es un avalúo ni
  aconseja comprar o invertir**, y lo dice al pie. Textos y redondeos en `reglas.yaml › hoja_cliente`; plantilla en
  `src/ficha/hoja_cliente.html`.
- **Estado con el cliente.** Toda ficha nace "Por entregar". El comercial registra cada cambio: entregada, aceptada
  o rechazada (con el motivo y lo que dijo el cliente). Queda el historial completo y vale el último. Se ve en sus
  cotizaciones, en la historia del cliente y en la lista del supervisor. Estados y motivos en
  `reglas.yaml › gestion_cliente`.

## El recorrido del comercial

Cada dato dice en pantalla si es obligatorio u opcional. El paso abierto se destaca y los ya hechos se
pliegan a una línea; se reabren con un clic.

1. **00 · Cliente.** Documento (CC, NIT o CE), nombre, **celular** y autorización de datos: todos obligatorios.
   Enseguida, **qué busca el cliente**: uno o varios tipos de inmueble.
2. **01 · Predio.** Tres formas de empezar: marcar el punto en el mapa, escribir la dirección o buscar un
   barrio o comuna y elegirlo de la lista. La dirección y el conjunto se detectan solos y se pueden corregir o borrar.
3. **02 · Inmueble.** Tipo (entre los que busca el cliente) y área, obligatorios. Los demás campos dependen del
   tipo: un lote solo pide área. Se pueden agregar características adicionales.
4. **03 · Inmobiliarias.** Dónde se buscan los anuncios vigentes. Todas vienen encendidas; se apagan una a una o
   todas. Las fuentes oficiales ya no se eligen: se consultan siempre.
5. **04 · Entorno, mercado y ficha.** El avance muestra los 11 pasos en dos bloques (ver "El orden del proceso").
6. La ficha queda en `fichas_clientes/<NIT o cédula>/FICHA_<fecha>_<cotización>_v<n>.md` (con su `.json`),
   se ve en pantalla y se puede descargar. Regenerarla crea una versión nueva; la anterior se conserva.

## El orden del proceso

Dos bloques, porque los datos tienen dos ritmos: el entorno es del predio y se reusa 30 días
(`reglas.yaml › captura.reusar_captura_dias`); el mercado es de la cotización y siempre es del día.

| # | Paso | Qué deja |
|---|---|---|
| | **Entorno del predio** | |
| 1 | Ubicación | Punto en EPSG:4326 con su precisión. Si hay una captura reciente del mismo punto, se reusa y se salta al paso 8 |
| 2 | Predio y norma | Catastro, barrio, comuna y tratamiento del POT |
| 3 | Barrido de 1 km | Lo que traen las fuentes oficiales y OpenStreetMap |
| 4 | Depuración por reglas | Clasificación, duplicados enlazados y pares dudosos en cola de revisión |
| 5 | Guardado provisional | Todo en la base, con la captura "en curso". Si Claude falla después, el barrido no se pierde |
| 6 | Curaduría con Claude | Corrige esas mismas filas: nombres, categorías y pares dudosos. Opcional |
| 7 | Indicadores y cierre | Indicadores recalculados sobre lo ya curado; la captura queda cerrada (`ok` o `parcial`) |
| | **Mercado de la cotización** | |
| 8 | Inmobiliarias (RPA con Claude Code) | Anuncios del día de lo que pidió el cliente. Arranca al terminar el paso 2 y corre al tiempo con 3 a 7 |
| 9 | Depuración de anuncios | El mismo inmueble publicado en dos sitios queda enlazado; se mide la vigencia |
| 10 | Carga al almacén (ETL) | Esquema estrella con todo depurado. Solo entran capturas cerradas |
| 11 | Ficha del inmueble | Markdown y JSON en la carpeta del cliente |

- **Guardado en dos tiempos.** El paso 5 guarda y el 7 cierra. Mover el guardado al final dejaría sin datos una
  captura en la que Claude no respondió; mover los indicadores antes de la curaduría los calcularía sobre datos
  que después cambian (decisión DU-32).
- **Búsqueda adelantada.** El paso 8 solo necesita barrio y comuna. Mientras corre no toca la base: lo que trae
  se guarda cuando la captura cierra bien, y se descarta si la captura falla (DU-34).
- **Depuración de anuncios.** Reglas fijas, sin Claude (`src/captura/curaduria/anuncios.py`). Enlaza dos anuncios
  de sitios distintos con la misma operación, tipo y barrio, y área y precio dentro de la tolerancia de
  `almacen.yaml › bases_mercado.portales.duplicados_entre_sitios`. El repetido no se borra: queda con
  `regla_dup = R3_entre_sitios`, su puntaje y el anuncio que se conserva, y la ficha lo dice (DU-33).

## Estructura

```
config/     reglas.yaml (negocio de la captura, clientes, inmueble, fichas, supervisión) · params.yaml (técnico)
            taxonomia.yaml · fuentes.yaml · usuarios.yaml (comerciales y supervisores)
            parametros.yaml (reglas OFICIALES del reto, sin cambios) · ficha.yaml (técnico del motor de ficha)
            almacen.yaml (ETL y esquema estrella: bases de mercado, zonas, transporte, calidad)
src/captura/   Momento 1: fuentes, curaduría, base operativa, acceso y roles, clientes, API y servidor
src/almacen/   ETL y esquema estrella: esquema · normalizacion · etl · calidad
src/ficha/     Momento 2: limpieza, deduplicacion, mercado, confianza (reglas oficiales)
               desde_almacen · entorno_almacen · plantilla_cliente · servicio (motor sobre el almacén)
               carga · entorno · validacion · pipeline (lote oficial sobre archivos, se conserva)
src/gestion/   metas · seguimiento · tablero (lee del almacén)
web/index.html interfaz única: comercial y supervisión
data/          captura.db (operativa) · almacen.db (estrella)
fichas_clientes/  una carpeta por NIT o cédula
reto_oficial/  repositorio oficial del reto (solo lectura): datos, validador y referencia
validacion/    referencia_momento2/ (las 11 fichas de ficha_v1 v0.3.0, para comparar)
tests/         pruebas de la captura · tests/m2 (lote oficial) · test_integracion.py (aplicación única)
docs/          modelo_estrella.md · plantilla_ficha_inmueble.md · modelo_datos.md · conectar_fuentes.md
auditoria/     decisiones.md (DU-xx tomadas, AU-xx abiertas) · heredadas/ (decisiones de los dos momentos)
respaldos/     copias de seguridad que crea el administrador (.zip; no se suben a ningún repositorio)
```

## Anuncios vigentes: el RPA de inmobiliarias

En cada cotización Claude Code (sesión del plan) lee las inmobiliarias encendidas y trae anuncios de **lo que
pidió el cliente**, en el barrio del predio y con el área del inmueble.

- Solo abre los dominios de `config/inmobiliarias.yaml`: van como permiso de la herramienta.
- No evade bloqueos: un sitio que no deja leer se anota y se sigue con el siguiente.
- No recibe datos del cliente: solo zona, tipos y rango de área.
- Lo que devuelve se valida (sitio, enlace, precio, área, tipo). Lo que no pasa queda con su motivo.
- **Cómo comprobar que corrió:** el paso "Inmobiliarias" del avance, el panel "Ofertas inmobiliarias" de la
  cotización (qué pasó en cada sitio, cuántos anuncios, enlaces) y la tabla `ejecuciones` (`fuente = 'portales'`).
- **Catálogo:** `config/inmobiliarias.yaml`, con 72 sitios (6 portales y 66 inmobiliarias) y 26 descartados con su motivo.
  Cada sitio trae la URL de búsqueda que funcionó en la prueba externa; el RPA la adapta al barrio y al tipo pedidos.
- **Lectura por grupos:** los sitios se leen de 6 en 6, cuatro grupos a la vez (`params.yaml › rpa`). Con muchos sitios
  encendidos la búsqueda tarda varios minutos.
- **Qué sitios se dejan leer:** `iniciar.bat inmobiliarias` prueba el catálogo con el motor real y deja el informe en
  `salidas/`. Con `--guardar --apagar` anota el resultado de cada sitio y deja apagados los que no se dejaron leer.
- **Fichas anteriores:** `iniciar.bat rebuscar` busca de nuevo, depura los anuncios y rehace la ficha de las cotizaciones ya hechas.
- **Otra forma de leer los sitios:** se evaluó un modelo alternativo (Crawl4AI, modelo externo, Polars y búsqueda en
  Google para los sitios bloqueados) y no se adoptó (DU-35). Lo que se encontró al evaluarlo quedó en AU-23.
- Agregar una inmobiliaria: una entrada en `config/inmobiliarias.yaml` con su nombre, su dominio y su URL de búsqueda.

**Vigencia.** Un comparable no puede tener más de 6 meses de publicado (`config/almacen.yaml › vigencia`). Las
ofertas OIME son de 2023: ya no son comparables y la ficha las muestra aparte, como referencia histórica.

## El almacén y el ETL

Detalle completo en `docs/modelo_estrella.md`.

- **Dimensiones conformadas:** fecha, fuente, ubicación (barrio y zona de mercado), tipo de inmueble,
  estado del inmueble, tipo de oferta (operación y pedido/cierre), categoría, lugar, comercial,
  cliente, predio e inmueble.
- **Hechos:** `fact_oferta` (mercado), `fact_oferta_predio` (qué ofertas forman el mercado de un predio),
  `fact_entorno` (lugares y distancias), `fact_cotizacion`, `fact_interes` (qué tipo de inmueble pidió cada
  cliente), `fact_ficha` y `fact_meta`.
- **Control:** cada corrida deja su conciliación (leídos = cargados + duplicados + rechazados), los
  rechazos con su motivo, los valores que cambió y los controles de calidad (ok, alerta, falla).
- **Cuándo corre:** al terminar cada captura (solo esa captura), al abrir el tablero si la gestión
  cambió, con `iniciar.bat etl` y desde el panel "Calidad de datos" del supervisor.

## Reglas que el código hace cumplir

| Regla | Dónde |
|---|---|
| Ningún número de negocio en el código | `config/`; prueba `test_ningun_numero_de_negocio_en_el_codigo` (los cuatro paquetes) |
| Nada se borra en silencio | `motivo_exclusion` en la base operativa y en `fact_oferta`; `etl_rechazo`; las fichas conservan sus versiones |
| Pedido y cierre no se mezclan | `dim_tipo_oferta`; la ficha los reporta por separado; control `pedido_y_cierre_separados` |
| Bases de mercado separadas | `almacen.yaml › bases_mercado`: cada base se calcula aparte; la ficha dice cuál usó |
| Coordenadas en EPSG:4326, distancias en metros | `CHECK` en el esquema; haversine en la ficha y control en EPSG:9377 |
| Flexibilización visible | `mercado.criterio_comparables` y texto de la ficha |
| Sin recomendación de inversión | aviso fijo, alcance de la confianza y prueba de lenguaje sobre código, interfaz y fichas |
| Cada comercial ve solo lo suyo | `captura/api.py` (permisos) y `test_un_comercial_no_ve_las_cotizaciones_de_otro` |
| Datos personales acotados | Solo documento, nombre y celular del cliente; en el almacén el documento va enmascarado y con huella. El administrador no ve clientes ni cotizaciones; la hoja para el cliente no lleva su documento |
| Administración con rastro | `captura/admin.py`: usuario eliminado = inactivo con su historial; sitio eliminado = descartado con motivo; `admin_bitacora` sin claves |

## Verificación

| Prueba | Resultado |
|---|---|
| Validador oficial del reto, fichas leídas del almacén (INM-04, INM-06, INM-09) | 3/3 |
| 11 fichas contra la implementación de referencia del reto | 11/11 en todos los campos oficiales |
| 11 fichas contra las de `ficha_v1` v0.3.0 (Momento 2) | 11/11, incluidos control EPSG:9377 y huellas |
| Muestra oficial en el almacén | 117 anuncios = 63 válidos + 33 duplicados + 21 inválidos |
| Lote oficial sobre archivos (`python -m ficha.pipeline`) | Se conserva; sus 67 pruebas siguen en verde |
| Pruebas automáticas en Windows (entorno `venture-one`, 23-nov-2026) | 203 en verde, 1 omitida (solo aplica en Linux) |
| Administración, estado con el cliente y hoja para imprimir, en el navegador sobre una copia de la base (v2.2.0) | Administrador: crear y eliminar un usuario, agregar, apagar y eliminar una inmobiliaria con su motivo, respaldo de la base real en 2 s (13,4 MB) y bitácora con cada cambio. Comercial: las fichas existentes aparecen "Por entregar"; rechazar exige el motivo; cada cambio queda en la historia del cliente; la hoja para el cliente cabe en una página carta |
| Orden nuevo con datos reales, sobre una copia (Laureles, apartamento de 80 m², 45 sitios encendidos) | 182 s en total: la captura con curaduría tardó 177 s y la búsqueda en inmobiliarias corrió 80 s al mismo tiempo · 41 anuncios de 20 sitios · 2 repetidos entre sitios enlazados (Duque Giraldo con Alberto Álvarez; Zitios con Panorama) · ficha con 38 comparables, confianza media |
| Prueba de lectura del catálogo con el motor real (23-nov-2026, apartamento en Laureles) | 71 sitios en 121 s: 28 con anuncios, 20 legibles sin anuncios, 23 que no se dejaron leer · 57 anuncios |
| Ficha real rehecha con la búsqueda ampliada (Alejandría, apartamento de 80 m²) | 46 sitios en 58 s · 22 comparables vigentes de 6 sitios · confianza baja (CV 26 %) |
| RPA real desde la interfaz (Villa Nueva; apartamento y casa) | 56 s · 17 anuncios de Fincaraíz · Metrocuadrado no se dejó leer · ficha con 3 comparables vigentes |
| Interfaz en el navegador, sobre una copia de la base | Comercial: cliente con NIT, predio, inmueble, captura reusada, ETL y ficha. Supervisor: tablero, metas, seguimiento y calidad de datos. Un comercial recibe 404 en lo de otro |
| ETL completo sobre la base real, en Windows | 5 s por corrida; 0 fallas de calidad |

```powershell
.\iniciar.bat probar                    # todo
.\scripts\probar.ps1 tests\test_integracion.py   # solo la aplicación única
```

## Qué necesita y qué es opcional

Internet (servidor de mapas de la Alcaldía y OpenStreetMap), Claude Code con sesión iniciada
(recomendado, para la curaduría) y, opcionales, las llaves de Google en `.env`. El paso a paso está
en `docs/conectar_fuentes.md`.

## Decisiones abiertas

Están en `auditoria/decisiones.md`. Las que más pesan en el resultado de una ficha:

- **AU-01 · Comparables.** Anuncios vigentes de las inmobiliarias: venta, mismo tipo, área ±30 % y **sin filtro
  de estado**, porque los anuncios no lo declaran de forma comparable. La ficha lo dice.
- **AU-13 · Anuncios sin fecha.** Valen por el día en que se vieron publicados.
- **AU-14 · Términos de uso** de los 71 sitios permitidos: revisión pendiente.
- **AU-21 · Tolerancia de los repetidos entre sitios:** hoy área ±1 % y precio ±2 %; falta calibrarla.
