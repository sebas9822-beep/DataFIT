# Contexto del proyecto y origen de la base

Síntesis de los insumos con los que se construyó la base: tres lecturas del propósito, tres propuestas de pipeline, tres guías de instalación y los requisitos del orquestador. Fecha: 14-nov-2026.

## 1. El reto

- **Qué es:** reto empresarial n.º 13 de EstudIA (Alcaldía de Medellín), clasificado como análisis de datos de nivel básico, con herramientas gratuitas (Google Colab y GitHub). Lo propone la Dirección General de Venture One SAS.
- **El negocio:** Venture One identifica, compra, renueva y vende o renta inmuebles subvalorados en Medellín. Su tesis es que la rentabilidad se construye antes de la compra.
- **El dolor:** el análisis previo de cada oportunidad es manual. Se buscan comparables uno por uno en portales, se toman capturas del geoportal y se consolida en hojas de cálculo cuyo formato cambia entre análisis. El resultado depende de quien lo hace, no se deduplica, no queda registro de los datos faltantes y la capacidad de análisis limita cuántas oportunidades se pueden revisar.
- **Lo que se pide:** una ficha automática de mercado y entorno por inmueble: precio por m² de comparables según estado (separando pedido de cierre), Metro más cercano, equipamientos en un radio, norma POT, nivel de confianza y faltantes declarados.

## 2. Los cuatro propósitos

| Propósito | Qué exige de la solución |
|---|---|
| Operativo | Reducir el esfuerzo de consolidar cada oportunidad y aumentar cuántos inmuebles se analizan |
| Analítico | Aplicar los mismos criterios a todas las oportunidades, sin importar quién corre el análisis |
| De control | Registrar qué datos se usaron, qué comparables se descartaron, qué estaba incompleto, qué parámetros se aplicaron y qué fuente soporta cada resultado |
| Estratégico | Convertir el criterio tácito de la dirección en un proceso reproducible: qué es comparable, cuánta distancia y diferencia de área se toleran, cómo se interpreta el estado, cuántos datos bastan |

El propósito estratégico es el que más pesa en el diseño: cada criterio de la dirección debe poder leerse y cambiarse en `config/params.yaml`, y cada cambio de criterio debe quedar en `auditoria/decisiones.md`.

## 3. Alcance

**Dentro:** recibir archivos controlados, validar, normalizar, deduplicar, seleccionar comparables, calcular estadísticas, cruzar capas geográficas y generar la ficha con su trazabilidad.

**Fuera:** evaluación o recomendación de inversión, viabilidad financiera, concepto jurídico sobre el POT, scraping de portales, datos personales, operaciones reales y sistemas internos de Venture One.

**Lo que la ficha no permite concluir:** que un inmueble esté subvalorado, que convenga comprarlo, que la muestra represente todo el mercado, ni que el cruce con el POT equivalga a una validación urbanística. Es la primera pieza de un sistema mayor (ficha descriptiva → valoración preliminar → evaluación de inversión → aprendizaje del portafolio); este proyecto cubre solo la primera.

**Riesgo a vigilar:** automatizar un criterio que la dirección aún no ha definido. Estandarizar datos no es estandarizar decisiones; por eso las decisiones abiertas se llevan a la persona y no se resuelven en el código.

## 4. Cruce de las tres propuestas de pipeline

| Tema | Opus (`ficha_v1`, proyecto ejecutable) | GPT (documento) | Gemini (cuaderno) | Lo adoptado |
|---|---|---|---|---|
| Forma | Paquete con módulos, parámetros YAML y pruebas | Paquete con 13 módulos, YAML, pandera, jinja2 | Cinco celdas de Colab, parámetros JSON | Paquete de Opus (10 módulos) |
| Limpieza | Normaliza precios, áreas y textos; bitácora de faltantes y descartes | Esquemas pandera y diccionarios de equivalencia | `dropna` y filtros fijos | Opus, más `motivo_exclusion`, exclusión explícita de sin estado y sin tipo de precio, y pandera para inmuebles e invariantes |
| Duplicados | Tolerancias de área, precio y fecha con union-find; el cierre no se fusiona con el pedido | Puntaje ponderado con umbrales y tabla de equivalencias | `drop_duplicates` por campos redondeados | Regla de Opus, más el puntaje y la tabla de auditoría de GPT (rapidfuzz para el barrio) |
| Comparables | Barrio y banda de área | Radio que se amplía por pasos hasta un máximo | Barrio y tolerancia de área | Barrio y banda de área, con flexibilización escalonada registrada (idea de GPT aplicada a la banda, porque los anuncios no traen coordenadas) |
| Pedido y cierre | Estadísticas separadas, pero referencia mezclada si faltan cierres | Siempre separados | Mezclados con un factor de ajuste al pedido | Siempre separados; la mezcla solo con parámetro explícito |
| Entorno | Metro, equipamientos por tipo y POT en EPSG:9377 | Igual, con varios radios | Igual, en EPSG:3116 | Opus, con verificación del CRS nativo y control de área de estudio |
| Confianza | Regla por n, dispersión y cierres | Puntaje ponderado de seis factores | Niveles por n | Regla de Opus, evaluada sobre la base elegida |
| Salida | JSON, resumen CSV, bitácora | JSON, HTML, CSV, log de auditoría | JSON y Markdown | JSON, Markdown legible (idea de Gemini), CSV y carpeta de auditoría |
| Validación | Compara con los 3 cálculos manuales | Los convierte en pruebas pytest | Sugiere `assert` | Ambas: comparación en el pipeline y prueba que se activa al llegar el archivo |

Se descartó de forma deliberada: el factor de ajuste que convierte pedido en cierre (mezcla las dos bases), la brecha de valorización entre estado actual y remodelado (se acerca a una evaluación de inversión) y el valor indicativo total (queda apagado por parámetro).

## 5. Cruce de las tres guías de instalación

| Tema | Opus | GPT | Gemini | Lo aplicado en este equipo |
|---|---|---|---|---|
| Gestor | Anaconda existente, entorno `ficha`, Python 3.11 | Miniforge nuevo, entorno `venture-one`, Python 3.12, `environment.yml` | `venv` con pip, Python 3.11 | Anaconda existente (conda 26.1.1), entorno `venture-one`, Python 3.12, `environment.yml` solo con conda-forge |
| Librerías geográficas | conda-forge | conda-forge | pip | conda-forge |
| Ubicación | `claude_trabajo\ficha_v1` | `C:\proyectos\venture-one` | `C:\Proyectos\venture-one-ficha-mercado` | `Aplicación\ficha_v1`, por indicación de Sebastián |
| Verificación | Pipeline y pytest | Scripts de verificación y prueba geográfica | Una línea de proyección | `scripts/verificar_entorno.py`, pipeline y pytest |

Los requisitos del orquestador añadieron: `jsonschema`, `jupytext`, `papermill` y `nbformat` al entorno; `CLAUDE.md`; `.claude/` con ajustes, seis agentes, dos skills y un hook; y las carpetas `validacion/`, `auditoria/` y `notebooks/`.

## 6. Fase 5

La fase 5 conectó los insumos oficiales del reto. La solución, sus resultados y su verificación están en `docs/solucion_reto.md`; la calidad de los datos, en `auditoria/perfil_datos.md`.
