# Contexto · aplicación única de Venture One

**Encargo.** Reto 13 de EstudIA (Alcaldía de Medellín) para Venture One S.A.S.: una ficha automática
de mercado y entorno por inmueble, reproducible y auditable. El proyecto se construyó en dos momentos
y esta aplicación los une.

| | Momento 1 · captura | Momento 2 · ficha |
|---|---|---|
| Qué hace | Ubica un predio y captura y depura su entorno a 1 km | Calcula el precio por m² de comparables, la confianza, el entorno y la norma |
| Insumo | Servidor de mapas de la Alcaldía, catastro, OIME, OpenStreetMap | Anuncios de mercado, inmuebles de prueba y capas geográficas del reto |
| Origen | `Aplicación vfinal\Aplicación fase1` (v1.1.0) | `Insumos para desarrollar el modelo\Aplicación\ficha_v1` (v0.3.0) |
| Contexto original | `docs/heredados/contexto_momento1.md` | `docs/heredados/contexto_momento2.md`, `solucion_reto_momento2.md` |

## Los cuatro propósitos y cómo responde la aplicación única

| Propósito | Respuesta |
|---|---|
| **Operativo:** menos esfuerzo por oportunidad | El comercial escribe una dirección y los datos del inmueble; captura, ETL y ficha corren solos |
| **Analítico:** los mismos criterios para todos | Las reglas viven en YAML y el cálculo lee siempre del almacén, no de quien hizo la consulta |
| **De control:** qué se usó y qué se descartó | Bitácora del ETL, rechazos con motivo, duplicados con regla, huellas de configuración y de archivos, fichas versionadas |
| **Estratégico:** el criterio tácito se vuelve proceso | Qué es comparable, qué distancia vale, cómo se lee el estado y cuántos datos bastan están en `parametros.yaml` y `almacen.yaml`, y las decisiones pendientes en `auditoria/decisiones.md` |

## Fuera de alcance

Evaluación o recomendación de inversión, avalúos y concepto jurídico sobre el POT. La ficha describe.

## Glosario

| Término | Significado |
|---|---|
| Predio | El punto capturado: una dirección con su lote, barrio y norma |
| Inmueble | La unidad que se cotiza: tipo, área y estado |
| Cotización | Un comercial + un cliente + un inmueble + una captura, con su evidencia |
| Ficha | El documento de mercado, entorno y norma de un inmueble |
| Base de mercado | Un conjunto de ofertas con sus propias reglas (muestra de Venture One, OIME) |
| Comparable | Oferta válida y no duplicada que cumple el criterio de la base |
| Confianza | Alta, media o baja según el número de comparables y su dispersión. Mide los datos, no el inmueble |
| Hecho / dimensión | Tablas del esquema estrella: lo que se mide y por qué se agrupa |
