# Venture One · aplicación única (captura, almacén y ficha del inmueble)

Aplicación local (Python + SQLite + un HTML). Momento 1 captura y depura el entorno de un predio;
un ETL lo lleva a un almacén en esquema estrella; Momento 2 arma la ficha del inmueble desde el
almacén. Responder en español, directo y técnico.

## Reglas duras

1. Ningún número de negocio en el código: `config/reglas.yaml`, `parametros.yaml` (oficial), `almacen.yaml`; lo técnico en `params.yaml` y `ficha.yaml`.
2. Nada se borra en silencio: `motivo_exclusion`; duplicados con regla, puntaje y `coincide_con` (lugares y anuncios); lo que no entra a un hecho va a `etl_rechazo`; las fichas conservan versiones.
2b. El guardado es en dos tiempos: provisional después de las reglas y cierre después de la curaduría. No mover el guardado al final ni los indicadores antes de la curaduría (DU-32). Solo una captura cerrada alimenta el almacén.
3. Precio pedido y de cierre nunca se mezclan. Tampoco dos bases de mercado en una misma estadística.
3b. Un comparable de una cotización no supera el plazo de `almacen.yaml › vigencia` (6 meses). Lo que queda fuera se informa como referencia histórica, con su motivo.
4. Entrada en EPSG:4326; distancias en metros (haversine) con control en EPSG:9377. No asignar un CRS sin verificar el de la capa.
5. Si se relaja un criterio de comparables, queda visible en la ficha.
6. La aplicación describe: sin recomendación de inversión ni concepto jurídico sobre el POT. La confianza mide los datos, no el inmueble. La hoja para el cliente da un precio de referencia, no un avalúo ni un consejo de compra.
7. La ficha y el tablero leen SOLO del almacén. Si falta un dato, se carga por el ETL; no se lee de la base operativa.
8. Cada comercial ve solo sus cotizaciones, capturas y fichas. El supervisor ve a quienes tiene a cargo. El administrador gestiona usuarios, inmobiliarias y respaldos y no ve cotizaciones ni clientes.
9. Datos personales: solo documento, nombre y celular del cliente. No van a Claude; en el almacén el documento va enmascarado y el celular no entra.
10. Las llaves solo en `.env`. Sin evasión de bloqueos: 403, 429 o CAPTCHA detienen la fuente. El RPA de inmobiliarias solo lee los dominios de `config/inmobiliarias.yaml`.
11. `reto_oficial/`, `config/parametros.yaml` y `validacion/` son de solo lectura.
12. Todo cambio va con su prueba; `pytest` en verde, validador oficial 3/3 y referencia 11/11 antes de entregar.
13. Las decisiones de criterio son de Venture One: `auditoria/decisiones.md` (DU-xx tomadas, AU-xx abiertas).

## Comandos (PowerShell normal, desde la raíz)

- Arrancar: `.\iniciar.bat` · Verificar: `.\iniciar.bat verificar`
- ETL completo: `.\iniciar.bat etl` (`python -m almacen.etl`) · una captura: `python -m almacen.etl --captura <id>`
- Fichas oficiales desde el almacén: `.\iniciar.bat reto` (`python -m ficha.servicio reto`)
- Fichas pendientes: `.\iniciar.bat fichas` · rehacer las anteriores con búsqueda nueva: `.\iniciar.bat rebuscar`
- Probar qué inmobiliarias se dejan leer: `.\iniciar.bat inmobiliarias` (`python scripts\probar_inmobiliarias.py --guardar --apagar`)
- Pruebas: `.\scripts\probar.ps1` · solo integración: `.\scripts\probar.ps1 tests\test_integracion.py`
- Clave de un usuario: `.\iniciar.bat clave supervisor1`

Python: `C:\Users\SEBAS\anaconda3\envs\venture-one\python.exe`. Siempre `PYTHONUTF8=1` y `PYTHONPATH=src`.
Los `.ps1` y `.bat` van sin tildes.

## Dónde está cada cosa

- Flujo de la captura: `src/captura/servicio.py` · API, permisos y servidor: `src/captura/api.py`
- Roles y sesiones: `src/captura/acceso.py` · usuarios iniciales en `config/usuarios.yaml`; después los gestiona el administrador
- Administración (usuarios, catálogo de inmobiliarias, respaldos, bitácora): `src/captura/admin.py` · rutas `/api/admin/…`
- Estado de la ficha con el cliente (historial): `src/gestion/estado_cliente.py` · estados en `reglas.yaml › gestion_cliente`
- Hoja para el cliente (imprimir o PDF): `src/ficha/hoja_cliente.py` y su plantilla `hoja_cliente.html` · textos en `reglas.yaml › hoja_cliente`
- Clientes, cotizaciones, lo que busca el cliente y datos del inmueble: `src/captura/clientes.py`
- Orden del proceso: entorno del predio, pasos 1 a 7 (`servicio.PASOS`) · mercado de la cotización, pasos 8 a 11 (`api.PASOS_FICHA`). Detalle en el README
- RPA de inmobiliarias con Claude Code: `src/captura/fuentes/portales.py` · arranca adelantado desde `api._BusquedaAdelantada`; con captura reusada, `api.buscar_inmobiliarias`
- Depuración de anuncios (repetidos entre sitios y vigencia), antes del ETL: `src/captura/curaduria/anuncios.py` · parámetros en `almacen.yaml › bases_mercado.portales.duplicados_entre_sitios`
- Base operativa y su migración: `src/captura/bd.py`
- Esquema estrella: `src/almacen/esquema.py` · ETL: `src/almacen/etl.py` · controles: `src/almacen/calidad.py`
- Bases de mercado, zonas, transporte y umbrales de calidad: `config/almacen.yaml`
- Motor de ficha sobre el almacén: `src/ficha/desde_almacen.py`, `entorno_almacen.py`
- Estructura de la ficha en Markdown: `src/ficha/plantilla_cliente.py` y `docs/plantilla_ficha_inmueble.md`
- De la cotización al archivo: `src/ficha/servicio.py`
- Metas, seguimiento y tablero: `src/gestion/`
- Interfaz: `web/index.html` (un solo archivo; todo dato pasa por `esc()`)

## Puntos donde hay que detenerse y preguntar

- Cambiar una regla oficial (`parametros.yaml`): estados, rango de precio/m², tolerancia de área, deduplicación, confianza o radio.
- Cambiar `almacen.yaml › bases_mercado` (qué base alimenta la ficha y con qué criterio) o las zonas de mercado.
- Cambiar el método de distancia o el CRS de control.
- Quién ve qué: roles, `supervisa`, reuso de capturas entre comerciales.
- Cualquier salida que se acerque a una recomendación de inversión.

## No hacer sin que Sebastián lo pida

Commits, subir a GitHub, agregar inmobiliarias o cambiar `permitida` en `config/inmobiliarias.yaml` (eso lo hace el administrador desde su pantalla), instalar paquetes, borrar fichas, capturas o respaldos.
