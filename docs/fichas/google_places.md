# Ficha de fuente · Google Places (New) y Geocoding

| | |
|---|---|
| Servicios | Places API (New) `places:searchNearby`, Geocoding API, Maps JavaScript API |
| Acceso | Con llave. Dos llaves separadas: servidor (`GOOGLE_SERVER_KEY`) y navegador (`GOOGLE_BROWSER_KEY`) |
| Estado | Opcional. Sin llaves la aplicación trabaja sin Google |
| Probado en vivo | **No**: no había llaves en el equipo. La lógica está cubierta con pruebas simuladas |

## Qué se guarda y por cuánto tiempo

| Dato | Regla |
|---|---|
| `place_id` | Permanente |
| Latitud y longitud | Máximo 30 días (`expira_en`); después se borran |
| Nombre del lugar | No se guarda. Solo se muestra durante la sesión |
| Categoría propia (educación, salud…) | Permanente: es clasificación de la aplicación |
| Calificaciones, teléfonos, reseñas | No se piden (subirían el costo y no se pueden conservar) |

## Cómo se consulta

Diez grupos de tipos, uno por solicitud, con máscara de campos mínima. Si una solicitud llega al
tope de 20 resultados, el círculo se subdivide en siete subcírculos y la categoría queda marcada
como saturada. El contenido de Google **no se envía a Claude**.

## Pendiente

**A-06:** validación jurídica del uso y creación de las llaves, con restricción por API e IP
(servidor) y por referente `http://127.0.0.1:8765/*` (navegador).
