"""Conexión con Claude para la curaduría.

Dos caminos, elegidos en `params.yaml` (claude.proveedor):
- `claude_code`: Claude Code en modo no interactivo (`claude -p`), con la sesión del plan Claude
  que ya está abierta en este equipo. No necesita llave de API.
- `api`: API de Anthropic con ANTHROPIC_API_KEY (paquete `anthropic`).
Si ninguno está disponible, la captura sigue solo con reglas y lo declara.

En ambos casos Claude no tiene herramientas: recibe un lote JSON y devuelve JSON con un esquema fijo.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path


_CANDADO_SISTEMA = threading.Lock()


class ClaudeNoDisponible(Exception):
    pass


def detectar(p: dict) -> dict:
    """Qué proveedor se usará: {proveedor, disponible, modelo, detalle}."""
    cfg = p["params"]["claude"]
    pedido = cfg["proveedor"]
    exe = shutil.which(cfg["ejecutable"])
    tiene_api = bool(p["secretos"]["anthropic"])
    if pedido == "ninguno":
        return {"proveedor": "ninguno", "disponible": False, "modelo": None, "detalle": "desactivado en params.yaml"}
    if pedido in ("api", "auto") and tiene_api:
        try:
            import anthropic  # noqa: F401
            return {"proveedor": "api", "disponible": True, "modelo": cfg["modelo_api"], "detalle": "API de Anthropic"}
        except ImportError:
            if pedido == "api":
                return {"proveedor": "api", "disponible": False, "modelo": cfg["modelo_api"],
                        "detalle": "falta el paquete anthropic (pip install anthropic)"}
    if pedido in ("claude_code", "auto") and exe:
        return {"proveedor": "claude_code", "disponible": True, "modelo": cfg["modelo_cli"],
                "detalle": "Claude Code (sesión del plan Claude)", "ejecutable": exe}
    faltante = "ANTHROPIC_API_KEY" if pedido == "api" else "Claude Code en el PATH ni ANTHROPIC_API_KEY"
    return {"proveedor": pedido, "disponible": False, "modelo": None, "detalle": f"no se encontró {faltante}"}


def _por_cli(p: dict, det: dict, sistema: str, mensaje: str, esquema: dict, cfg: dict | None = None,
             herramientas: list[str] | None = None, permitidas: list[str] | None = None) -> dict:
    cfg = cfg or p["params"]["claude"]
    carpeta = Path(p["rutas"]["trabajo_claude"])
    carpeta.mkdir(parents=True, exist_ok=True)       # carpeta neutra: sin CLAUDE.md ni ajustes de proyecto
    arch = carpeta / f"sistema_{hashlib.sha256(sistema.encode()).hexdigest()[:12]}.txt"
    with _CANDADO_SISTEMA:                             # varias llamadas a la vez (lotes del RPA) comparten el archivo
        if not arch.exists():
            arch.write_text(sistema, encoding="utf-8")
    exe = det["ejecutable"]
    usa_esquema = not exe.lower().endswith((".cmd", ".bat"))   # los lanzadores .cmd dañan las comillas del JSON
    cmd = [exe, "-p", cfg["instruccion_cli"], "--output-format", "json", "--model", cfg["modelo_cli"],
           "--system-prompt-file", str(arch), "--tools", ",".join(herramientas or []), "--disallowedTools", "mcp__*",
           "--no-session-persistence", "--safe-mode", "--max-turns", str(cfg["max_turnos"])]
    if permitidas:
        cmd += ["--allowedTools", *permitidas]
    if usa_esquema:
        cmd += ["--json-schema", json.dumps(esquema, ensure_ascii=True, separators=(",", ":"))]
    entorno = dict(os.environ, PYTHONUTF8="1")
    entorno.pop("ANTHROPIC_API_KEY", None)            # este camino usa la sesión del plan, no una llave
    try:
        r = subprocess.run(cmd, input=mensaje, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=cfg["timeout_s"], cwd=str(carpeta), env=entorno)
    except subprocess.TimeoutExpired as e:
        raise ClaudeNoDisponible(f"Claude Code no respondió en {cfg['timeout_s']} s") from e
    except OSError as e:
        raise ClaudeNoDisponible(f"no se pudo ejecutar Claude Code: {e}") from e
    salida = (r.stdout or "").strip()
    try:
        sobre = json.loads(salida)
    except ValueError as e:
        raise ClaudeNoDisponible(f"salida no JSON de Claude Code (código {r.returncode}): "
                                 f"{(salida or r.stderr or '')[:200]}") from e
    if isinstance(sobre, list):                        # por si la versión emite la lista de eventos
        sobre = next((x for x in reversed(sobre) if isinstance(x, dict) and x.get("type") == "result"), {})
    if r.returncode != 0 or sobre.get("is_error"):
        raise ClaudeNoDisponible(f"Claude Code devolvió error: {str(sobre.get('result') or r.stderr)[:200]}")
    datos = sobre.get("structured_output")
    if datos is None:
        datos = extraer_json(str(sobre.get("result") or ""))
    return {"datos": datos, "modelo": cfg["modelo_cli"], "costo_usd": sobre.get("total_cost_usd"),
            "proveedor": "claude_code", "turnos": sobre.get("num_turns"),
            "denegados": [str(d.get("tool_name") or "") for d in sobre.get("permission_denials") or [] if isinstance(d, dict)]}


def invocar_con_web(p: dict, sistema: str, mensaje: str, esquema: dict, dominios: list[str], cfg: dict) -> dict:
    """Claude Code con búsqueda y lectura web, limitada a `dominios` (y sus www). Usa la sesión del plan.

    La lista de dominios es el permiso de la herramienta: lo que no esté ahí se le niega aunque lo intente.
    """
    det = detectar(p)
    if not det.get("ejecutable"):
        exe = shutil.which(p["params"]["claude"]["ejecutable"])
        if not exe:
            raise ClaudeNoDisponible("la búsqueda en inmobiliarias necesita Claude Code instalado y con sesión iniciada")
        det = dict(det, ejecutable=exe)
    permitidas = ["WebSearch"] + [f"WebFetch(domain:{h})" for d in dominios for h in (d, f"www.{d}")]
    return _por_cli(p, det, sistema, mensaje, esquema, cfg=cfg, herramientas=["WebSearch", "WebFetch"], permitidas=permitidas)


def _por_api(p: dict, sistema: str, mensaje: str, esquema: dict) -> dict:
    import anthropic
    cfg = p["params"]["claude"]
    cliente = anthropic.Anthropic(api_key=p["secretos"]["anthropic"])
    try:
        resp = cliente.messages.create(
            model=cfg["modelo_api"], max_tokens=cfg["max_tokens_api"], system=sistema,
            messages=[{"role": "user", "content": mensaje}],
            output_config={"format": {"type": "json_schema", "schema": esquema}})
    except Exception as e:
        raise ClaudeNoDisponible(f"API de Anthropic: {type(e).__name__}: {str(e)[:200]}") from e
    texto = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    return {"datos": extraer_json(texto), "modelo": cfg["modelo_api"], "costo_usd": None, "proveedor": "api"}


def extraer_json(texto: str):
    """JSON del texto de respuesta, aunque venga entre cercas de código."""
    t = texto.strip()
    ini, fin = t.find("{"), t.rfind("}")
    if ini < 0 or fin <= ini:
        raise ClaudeNoDisponible("la respuesta de Claude no trae JSON")
    try:
        return json.loads(t[ini:fin + 1])
    except ValueError as e:
        raise ClaudeNoDisponible(f"JSON inválido en la respuesta de Claude: {e}") from e


# Las pruebas reemplazan esta función para no llamar al servicio real.
def invocar(p: dict, sistema: str, mensaje: str, esquema: dict) -> dict:
    """Envía un lote y devuelve {datos, modelo, costo_usd, proveedor}. Lanza ClaudeNoDisponible."""
    det = detectar(p)
    if not det["disponible"]:
        raise ClaudeNoDisponible(det["detalle"])
    if det["proveedor"] == "api":
        return _por_api(p, sistema, mensaje, esquema)
    return _por_cli(p, det, sistema, mensaje, esquema)
