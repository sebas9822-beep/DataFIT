# Conectar y verificar las fuentes

Guía paso a paso para dejar funcionando cada fuente de la captura y saber, en un minuto, si el
equipo está listo. Todo se hace desde la carpeta de la aplicación:
`C:\DataFIT\Análisis de datos\Aplicación vfinal\Aplicación fase1`.

| Fuente | Qué aporta | Qué hay que conectar |
|---|---|---|
| Catastro de Medellín | Ubicación oficial, lote, construcción, nombres de edificios y conjuntos | Nada: funciona sin llave |
| GeoMedellín | Equipamientos, transporte, POT, patrimonio, cámaras | Nada: funciona sin llave |
| Industria y Comercio | Establecimientos activos | Nada: funciona sin llave |
| OIME | Ofertas 2023 (precio pedido) | Nada técnico; falta confirmar condiciones de uso (A-01) |
| OpenStreetMap | Complemento de lugares; respaldo de direcciones | Un correo de contacto (paso 2) |
| Claude | Depuración de lo dudoso | Sesión de Claude Code (paso 4) |
| Google | Mapa de Google, autocompletado, Google Places | Dos claves de Google Cloud (paso 3) |
| Portales de anuncios | — | No se conectan todavía (paso 6) |

---

## 1. Verificar el equipo (siempre primero)

1. Abre PowerShell o una ventana de comandos en la carpeta de la aplicación.
2. Ejecuta:

   ```
   .\iniciar.bat verificar
   ```

3. Lee el resumen del final:
   - **Listo para capturar**: puedes abrir la aplicación.
   - **Hay avisos que no impiden capturar**: funciona, pero algo opcional falta o está lento.
   - **Hay fallas**: sigue la lista "Qué hacer", en orden.
4. El informe queda guardado en `salidas\verificacion_<fecha>.txt` por si hay que compartirlo.
   No contiene llaves.

Qué revisa:

| Bloque | Revisa |
|---|---|
| Equipo | Python 3.11 o superior y el entorno `venture-one`; paquetes; que la carpeta esté completa y se pueda escribir; espacio en disco; integridad de la base; cuántos comerciales siguen con la clave inicial; si el puerto 8765 está libre o si hay otra copia abierta; el archivo `.env` |
| Claude | Que Claude Code esté instalado, que tenga sesión y cuándo vence; una llamada de prueba mínima |
| Servidor de mapas | El directorio de servicios de la Alcaldía y una capa de cada fuente, con su sistema de coordenadas nativo |
| OpenStreetMap | Cada instancia de Overpass y Nominatim |
| Página | Lo que carga el navegador: librería del mapa, teselas y tipografías |
| Google | Formato de las claves y, si hay clave de servidor, una llamada de prueba a Geocoding y una a Places |

Opciones: `.\iniciar.bat verificar --sin-claude` (sin la llamada a Claude) y
`.\iniciar.bat verificar --sin-red` (solo el equipo).

Para el detalle capa por capa (campos que usa la aplicación y si siguen existiendo):
`.\iniciar.bat probar`.

---

## 2. OpenStreetMap: poner un correo de contacto

Overpass y Nominatim son servicios comunitarios y piden que cada herramienta se identifique con
un correo real. Sin él la aplicación usa `contacto@ventureone.com.co`, que es un supuesto (A-07).

1. En la carpeta de la aplicación, copia `.env.example` y renombra la copia a `.env`.
2. Ábrelo con el Bloc de notas y llena la última línea:

   ```
   CONTACTO=correo-real@ventureone.com.co
   ```

3. Guarda, cierra la aplicación si estaba abierta y vuelve a abrirla con `iniciar.bat`.
4. Verifica: `.\iniciar.bat verificar` debe mostrar `[ OK ] Archivo .env ... CONTACTO`.

Si Overpass no responde (pasa con frecuencia):

- El verificador lo marca como aviso. No es un problema del equipo.
- Al capturar, apaga el botón **OpenStreetMap** en el paso 02 de la pantalla. La captura sigue
  con las fuentes oficiales, que son las principales.
- Si quieres probar otra instancia pública, agrégala en `config\params.yaml › osm.overpass_urls`.
  La aplicación prueba las instancias en orden y espera como máximo 75 s en total.

---

## 3. Google: mapa, autocompletado y Google Places (opcional)

Sin Google la aplicación funciona completa con el mapa de OpenStreetMap. Con Google se suman el
mapa de Google, el autocompletado de direcciones y Google Places como fuente del barrido.

**Antes de empezar:** el uso de Google se cobra por consulta y tiene condiciones sobre qué se
puede guardar (la aplicación ya las cumple: ver `docs\fichas\google_places.md`). Valídalo con
jurídica antes de usarlo en entregables (A-06).

1. **Proyecto.** En la consola de Google Cloud crea un proyecto (por ejemplo "venture-one-captura")
   y asígnale una cuenta de facturación.
2. **Habilitar tres API** en "API y servicios › Biblioteca":
   - Maps JavaScript API (el mapa de la página)
   - Places API (New) — la nueva, no la heredada
   - Geocoding API
3. **Clave de navegador.** En "Google Maps Platform › Credenciales", "Crear credenciales › Clave de API".
   Edítala:
   - Nombre: `captura-navegador`
   - Restricción de aplicación: **Sitios web**, con `http://127.0.0.1:8765/*` y `http://localhost:8765/*`
   - Restricción de API: Maps JavaScript API y Places API (New)
4. **Clave de servidor.** Crea otra clave:
   - Nombre: `captura-servidor`
   - Restricción de aplicación: **Direcciones IP**, con la IP pública de tu red
   - Restricción de API: Places API (New) y Geocoding API

   Nunca uses la misma clave para las dos cosas: la de navegador viaja a la página.
   Si tu IP pública cambia (internet de hogar), esta clave dejará de responder: vuelve a poner la
   IP nueva, o deja solo la restricción de API mientras pruebas.
5. **Map ID (recomendado).** En "Google Maps Platform › Administración de mapas" crea un ID de mapa
   de tipo JavaScript. Sin él se usa `DEMO_MAP_ID`, que es solo para pruebas.
6. **Pegar en `.env`** (el mismo archivo del paso 2):

   ```
   GOOGLE_SERVER_KEY=la-clave-de-servidor
   GOOGLE_BROWSER_KEY=la-clave-de-navegador
   GOOGLE_MAP_ID=el-id-del-mapa
   ```

7. **Verificar:** `.\iniciar.bat verificar`. Debe decir `[ OK ] Geocoding API` y
   `[ OK ] Places API (New)`. Si dice FALLA, el mensaje de Google indica qué falta
   (normalmente una API sin habilitar o una restricción que no incluye esa API o esa IP).
8. **Abrir la aplicación** con `iniciar.bat`. El mapa debe ser el de Google y la dirección debe
   autocompletarse. Si Google rechaza la clave de navegador, la página lo avisa y vuelve a
   OpenStreetMap.

---

## 4. Claude: la depuración con IA

### Opción recomendada: Claude Code con la sesión del plan

Ya está instalado en este equipo y no necesita llave de API.

1. Revisa la sesión: `.\iniciar.bat verificar` muestra si está iniciada y cuándo vence.
2. Si no hay sesión o está por vencer: abre una terminal, escribe `claude`, usa `/login` con la
   cuenta del plan y cierra con `/exit`.
3. Vuelve a verificar: debe decir `[ OK ] Llamada de prueba`.

Si Claude no está disponible, la captura funciona igual y se depura solo con reglas; la pantalla
y la base lo dejan dicho.

### Alternativa: API de Anthropic (pago por uso)

1. Instala el paquete en el entorno de la aplicación:

   ```
   C:\Users\SEBAS\anaconda3\envs\venture-one\python.exe -m pip install anthropic
   ```

2. Pon la llave en `.env`: `ANTHROPIC_API_KEY=...`
3. Ojo: con `claude.proveedor: auto` (el valor actual de `config\params.yaml`), si hay llave de API
   la aplicación la prefiere sobre Claude Code y cada captura se cobra en la cuenta de la API.
   Para seguir con el plan aunque exista la llave, cambia a `proveedor: claude_code`.

### Modelo

`config\params.yaml › claude.modelo_cli` (hoy `haiku`). `sonnet` u `opus` corrigen mejor nombres
raros pero tardan más y consumen más del plan (A-09).

---

## 5. Agregar otra capa del servidor de mapas (sin tocar código)

El servidor de mapas de la Alcaldía tiene muchas más capas. Para sumar una al barrido:

1. **Buscarla.** Abre en el navegador
   `https://www.medellin.gov.co/servidormapas/rest/services`, entra a la carpeta, al servicio
   (`MapServer`) y anota el número de la capa.
2. **Revisarla.** En la página de la capa anota:
   - *Geometry Type* (punto o polígono)
   - *Spatial Reference* (el sistema de coordenadas nativo; la aplicación consulta en EPSG:4326 y
     mide en metros, pero se registra el nativo)
   - *Fields*: el campo del nombre y el del tipo
3. **Declararla.** Copia este bloque al final de `capas_entorno` en `config\fuentes.yaml` y llénalo:

   ```yaml
     - id: mi_capa_nueva                 # sin tildes ni espacios
       fuente: geomedellin               # una fuente del catálogo de arriba
       etiqueta: "Nombre legible de la capa"
       servicio: carpeta/NombreDelServicio
       capa: 0
       clasificador: fija                # fija = todo va a la misma categoría
       categoria: salud                  # una de las 10 de taxonomia.yaml
       nombre: [nombre]                  # campos candidatos para el nombre, en orden
       subtipo_defecto: centro_de_salud
       atributos: [cbml, direccion]      # campos que se guardan tal cual
       activa: true
       dedupe: true                      # participa en duplicados entre fuentes
       ia: true                          # Claude puede revisarla
       mapa: true                        # false si son miles de registros
   ```

4. **Probarla:** `.\iniciar.bat probar`. La capa nueva debe salir `ok`, con su CRS nativo y sin
   "faltan campos".
5. **Capturar** una dirección y revisar la sección 05 (Fuentes y trazabilidad) de la pantalla.
6. **Registrar** la decisión en `auditoria\decisiones.md` (qué capa, por qué, en qué categoría).

Para sumar un tipo de lugar de OpenStreetMap, agrega una fila `[clave, valor, categoria, subtipo]`
a la lista `osm` de `config\taxonomia.yaml` (por ejemplo `[amenity, veterinary, salud, veterinaria]`).

---

## 6. Fuentes que no se conectan todavía

| Fuente | Por qué no | Qué haría falta |
|---|---|---|
| Portales de anuncios (Fincaraíz, Metrocuadrado, Ciencuadras) | Sin ficha de viabilidad; al menos uno prohíbe robots y bases derivadas | Revisar términos y `robots.txt`, o un convenio; luego escribir el conector (A-08) |
| Municipios vecinos (Envigado, Itagüí, Sabaneta, Bello) | Sus capas oficiales no están catalogadas | Catalogar los servicios de cada municipio (A-10) |
| Precios de cierre | No hay fuente pública | Registros propios de Venture One; entran en la fase siguiente, separados del precio pedido |

---

## 7. Señales de problema

| Lo que ves | Qué significa | Qué hacer |
|---|---|---|
| Servidor de mapas: "no encuentra el servidor (DNS)" | No hay internet o la VPN lo bloquea | Revisa la conexión; prueba sin VPN |
| Overpass: "no conecta" o "no responde a tiempo" | El servicio comunitario está saturado | Apaga OpenStreetMap al capturar |
| Puerto 8765: "hay OTRA copia abierta" | Otra carpeta de la aplicación está corriendo | Cierra esa ventana negra y abre esta copia |
| Puerto 8765: "lo usa otro programa" | Otro programa tomó el puerto | `iniciar.bat --puerto 8766` |
| Claude: "no tiene sesión iniciada" | La sesión de Claude Code venció | `claude`, luego `/login` |
| Google: `REQUEST_DENIED` o HTTP 403 | API sin habilitar o restricción de la clave | Paso 3, puntos 2 a 4 |
| Captura "parcial" | Alguna capa no respondió | Sección 05 de la pantalla: dice cuál y por qué |
