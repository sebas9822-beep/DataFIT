# Ficha de fuente · Ofertas del OIME (Observatorio Inmobiliario de Medellín)

| | |
|---|---|
| Servicio | `vivienda_ciudad_terri/VM_Oferta_Comercial_Oime`, capa 1 (ofertas tomadas de internet) |
| Productor | Subsecretaría de Catastro, Alcaldía de Medellín |
| Acceso | ArcGIS REST público, sin autenticación |
| CRS nativo | EPSG:4326, según declara el servicio (verificado con `scripts/probar_fuentes.py`) |
| Verificado | 20-nov-2026 |
| Volumen | Unas 28.000 ofertas en la ciudad, todas del año 2023 |
| Tipo de precio | **Pedido** (oferta publicada). No hay precio de cierre |

## Qué se toma

Operación (venta o arriendo), tipo de inmueble, estrato, áreas, precio, administración,
habitaciones, baños, parqueaderos, piso, barrio, año y coordenadas.

## Qué no se toma

La capa trae `phone`, `agency`, `description` y otros campos de contacto. **No se piden en la
consulta** y, por si la estructura cambia, también están en la lista de campos prohibidos.

La capa 0 del mismo servicio (ofertas levantadas en campo) no se usa: trae nombres y teléfonos
de personas y responde con error de sistema de referencia.

## Limitaciones

- Es histórica: sirve de referencia, no como oferta vigente.
- Una misma propiedad puede aparecer publicada más de una vez.
- La ubicación es la que publicó el anunciante; puede estar desplazada.

## Pendiente

**A-01:** confirmar con la Subsecretaría de Catastro las condiciones de uso y de conservación de
esta capa antes de usarla en entregables. Mientras tanto se guarda sin datos personales.
