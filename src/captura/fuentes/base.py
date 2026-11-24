"""Piezas comunes de los conectores.

Reglas no negociables (concepto unificado del buscador de predios):
- Sin evasión: ante 403, 429 o CAPTCHA la fuente se detiene y queda como `bloqueado`.
- Un fallo de extracción nunca se reporta como "cero resultados".
- Los datos personales se eliminan al capturar, no después.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

# Las pruebas inyectan aquí un httpx.MockTransport para no salir a la red.
TRANSPORTE: httpx.AsyncBaseTransport | None = None


class FuenteBloqueada(Exception):
    """La fuente respondió 403/429 o pidió verificación humana: no se insiste."""


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Ejecucion:
    fuente: str
    capa: str = ""
    inicio: str = field(default_factory=ahora)
    fin: str = ""
    estado: str = "en_curso"      # ok | parcial | bloqueado | fallo_estructura | error | omitida
    registros: int = 0
    descartados: int = 0
    llamadas: int = 0
    mensaje: str = ""

    def cerrar(self, estado: str, mensaje: str = "") -> "Ejecucion":
        self.estado, self.fin = estado, ahora()
        if mensaje:
            self.mensaje = f"{self.mensaje} · {mensaje}" if self.mensaje else mensaje
        return self

    def a_dict(self) -> dict:
        return asdict(self)


def nuevo_cliente(p: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(p["params"]["red"]["timeout_s"]),
        headers={"User-Agent": p["user_agent"]},
        follow_redirects=True,
        transport=TRANSPORTE,
    )


async def pedir(cli: httpx.AsyncClient, p: dict, metodo: str, url: str, *, reintentos: int | None = None,
                **kw) -> httpx.Response:
    """Solicitud con reintentos acotados solo ante errores de red o 5xx."""
    red = p["params"]["red"]
    tope = red["reintentos"] if reintentos is None else reintentos
    ultimo: Exception | None = None
    for intento in range(tope + 1):
        try:
            r = await cli.request(metodo, url, **kw)
            if r.status_code in (403, 429):
                raise FuenteBloqueada(f"HTTP {r.status_code} en {httpx.URL(url).host}")
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            return r
        except FuenteBloqueada:
            raise
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            ultimo = e
            if intento < tope:
                await asyncio.sleep(red["espera_base_s"] * (2 ** intento))
    raise ultimo if ultimo else RuntimeError("solicitud fallida")


def limpiar_personales(obj, prohibidos: set[str]):
    """Quita campos de contacto de un dict/lista anidados (comparación sin mayúsculas)."""
    if isinstance(obj, dict):
        return {k: limpiar_personales(v, prohibidos) for k, v in obj.items() if k.lower() not in prohibidos}
    if isinstance(obj, list):
        return [limpiar_personales(x, prohibidos) for x in obj]
    return obj


_TEL = re.compile(r"(?<!\d)(?:\+?57[\s.-]?)?(?:3\d{2}|60\d)[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_CORREO = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def depurar_texto(texto: str | None) -> str | None:
    """Borra teléfonos y correos incrustados en texto libre."""
    if not texto:
        return texto
    return _CORREO.sub("[correo]", _TEL.sub("[tel]", texto))


def primero(attrs: dict, candidatos: list[str] | tuple[str, ...]):
    """Primer valor no vacío entre varios nombres de campo (sin distinguir mayúsculas)."""
    for c in candidatos or []:
        v = attrs.get(c.lower())
        if v is not None and str(v).strip() not in ("", "None", "Null", "null"):
            return v
    return None
