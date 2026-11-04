"""Gateway de modelos: cada tarea de AIDM pide aquí su modelo.

Cada tarea tiene un perfil según el trabajo que hace (extracción, cálculo, redacción…), y cada perfil es una lista
de modelos en orden: se usa el primero y los demás son sus reservas, de otros proveedores, por si falla, se queda
sin cupo gratuito o tarda más de lo que permite el yaml. Todo está en data_structures/modelos.yaml; en el .env solo
van las claves de los proveedores.

Para probar otro modelo en una tarea sin tocar el yaml, ponlo en el .env como MODELO_<TAREA>: va primero y las
reservas del perfil siguen detrás.
    MODELO_COSTES=gemini/gemini-3.6-flash

Cada llamada deja una línea «[gateway]» en el log, con la tarea, el modelo que ha respondido (o la reserva), los
tokens y los segundos. Con GRABAR_LLAMADAS=1 en el .env, además se guardan los prompts y las respuestas en
pruebas_modelos/grabaciones/, para compararlos con pruebas_modelos/comparar.py. Llevan datos del cliente: esa carpeta
no se sube.
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import litellm
import yaml
from crewai import LLM
from dotenv import load_dotenv

load_dotenv(".env")

MODELOS_PATH = Path("data_structures/modelos.yaml")
GRABACIONES = Path("pruebas_modelos/grabaciones")
TIMEOUT = 300  # segundos por llamada si el yaml no dice otra cosa; el 3 nov, el equipo esperó 2 × 600 s a Gemini
_VARIABLE = re.compile(r"\$\{(\w+)\}")


class ModeloDeTarea(str):
    """El nombre del modelo, que se usa como un texto normal, pero que recuerda su tarea y sus reservas: así
    build_llm() y parametros() les ponen las reservas del perfil y la etiqueta con que se registra cada llamada."""

    def __new__(cls, nombre: str, tarea: str = "", reservas: tuple[str, ...] = ()):
        modelo = super().__new__(cls, nombre)
        modelo.tarea = tarea
        modelo.reservas = tuple(reservas)
        return modelo


def _config() -> dict:
    """Se lee en cada llamada: un cambio en el yaml vale para la siguiente ejecución, sin reiniciar Streamlit."""
    with open(MODELOS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolver(valor) -> str:
    """«${VARIABLE}» → su valor en el .env; un nombre de modelo se queda igual."""
    return _VARIABLE.sub(lambda m: os.getenv(m.group(1), ""), str(valor or "")).strip()


def _escribir(linea: str) -> None:
    """Una línea entera de una vez y sin esperar: con el log en un archivo, print() las dejaba en el búfer y salían
    desordenadas respecto a los avisos de LiteLLM."""
    sys.stdout.write(linea + "\n")
    sys.stdout.flush()


def modelos_de(tarea: str, config: dict | None = None) -> tuple[str, list[str]]:
    """El perfil de la tarea y su lista de modelos, con MODELO_<TAREA> delante si está en el .env. Una tarea que no
    está en el yaml usa el perfil de «por_defecto»; un «perfil» con «/» se toma como un modelo suelto."""
    config = config if config is not None else _config()
    perfiles, tareas = config.get("perfiles") or {}, config.get("tareas") or {}
    if tarea not in tareas:
        _escribir(f"[gateway] Ojo: la tarea «{tarea}» no está en {MODELOS_PATH}; se usa el perfil por defecto")
    perfil = str(tareas.get(tarea) or config.get("por_defecto") or "")
    lista = perfiles.get(perfil) or ([perfil] if "/" in perfil else [])
    lista = [m for m in (_resolver(x) for x in lista) if m]
    propio = os.getenv(f"MODELO_{tarea.upper()}", "").strip()
    if propio:
        lista = [propio] + [m for m in lista if m != propio]
    return perfil, lista


def modelo(tarea: str) -> ModeloDeTarea:
    """El modelo de una tarea (el primero de su perfil), con sus reservas. Lo escribe en el log."""
    perfil, lista = modelos_de(tarea)
    if not lista:
        _escribir(f"[gateway] {tarea}: sin modelo (perfil «{perfil}»); revisa {MODELOS_PATH}")
        return ModeloDeTarea("", tarea)
    origen = f"MODELO_{tarea.upper()} del .env" if os.getenv(f"MODELO_{tarea.upper()}", "").strip() else f"perfil {perfil}"
    reservas = f"; reservas: {', '.join(lista[1:])}" if lista[1:] else ""
    _escribir(f"[gateway] {tarea}: {lista[0]} ({origen}{reservas})")
    return ModeloDeTarea(lista[0], tarea, tuple(lista[1:]))


def _reglas(model_name: str, config: dict | None = None) -> dict:
    """Reintentos, tope de salida y tope de tiempo de un modelo, según el yaml. Cloudflare sin regla lleva 4096 de
    tope de salida, porque si no corta a 256."""
    config = config if config is not None else _config()
    reglas = (config.get("modelos") or {}).get(str(model_name)) or {}
    params = {"num_retries": reglas.get("num_retries", 2),
              "timeout": reglas.get("timeout") or config.get("timeout_por_defecto") or TIMEOUT}
    max_tokens = reglas.get("max_tokens") or (4096 if str(model_name).startswith("cloudflare/") else None)
    if max_tokens:
        params["max_tokens"] = max_tokens
    return params


def parametros(model_name: str, **extra) -> dict:
    """Para el LLM de CrewAI o litellm.completion: las reglas del modelo y, si viene de modelo(), las reservas de su
    perfil (cada una con sus reglas) y la tarea, que se usa al registrar la llamada. `extra` manda sobre las reglas."""
    config = _config()
    params = _reglas(model_name, config)
    reservas = getattr(model_name, "reservas", ())
    if reservas:
        params["fallbacks"] = [{"model": r, **_reglas(r, config)} for r in reservas]
    tarea = getattr(model_name, "tarea", "")
    if tarea:
        params["metadata"] = {"aidm_tarea": tarea}
    return {**params, **extra}


def build_llm(model_name: str):
    """LLM para CrewAI con las reglas del modelo, sus reservas y su tarea."""
    params = parametros(model_name)
    if str(model_name).startswith("gemini/"):
        # Gemini también por LiteLLM: el cliente nativo de CrewAI no admite reservas y pide instalar google-genai
        params["provider"] = "litellm"
    return LLM(model=str(model_name), **params)


def completar(model: str, messages: list[dict], **kwargs):
    """litellm.completion con los parámetros del gateway (discovery, Terraform). Queda registrada como las demás."""
    return litellm.completion(model=str(model), messages=messages, **parametros(model, **kwargs))


# --- Registro de cada llamada: una línea en el log y, con GRABAR_LLAMADAS=1, el prompt y la respuesta ---

_bloqueo = threading.Lock()
_archivo: Path | None = None  # uno por arranque del programa, al grabar la primera llamada


def _miles(n) -> str:
    return f"{int(n or 0):,}".replace(",", ".")


def _registrar(tarea: str, modelo_usado: str, mensajes, respuesta: str, uso: dict | None, segundos: float | None,
               error: str = "") -> None:
    uso = uso or {}
    entrada, salida = uso.get("prompt_tokens"), uso.get("completion_tokens")
    tiempo = f" · {segundos:.1f} s".replace(".", ",") if segundos is not None else ""
    if error:
        _escribir(f"[gateway] {tarea or '¿tarea?'} · {modelo_usado} falló{tiempo}: {error[:300]}")
    else:
        tokens = f" · {_miles(entrada)} tokens de entrada y {_miles(salida)} de salida" if entrada is not None else ""
        _escribir(f"[gateway] {tarea or '¿tarea?'} · {modelo_usado}{tokens}{tiempo}")
    if os.getenv("GRABAR_LLAMADAS", "").strip().lower() not in ("1", "si", "sí", "true"):
        return
    global _archivo
    with _bloqueo:
        if _archivo is None:
            GRABACIONES.mkdir(parents=True, exist_ok=True)
            _archivo = GRABACIONES / f"{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        if isinstance(mensajes, str):
            mensajes = [{"role": "user", "content": mensajes}]
        registro = {"fecha": datetime.now().isoformat(timespec="seconds"), "tarea": tarea, "modelo": modelo_usado,
                    "mensajes": mensajes or [], "respuesta": respuesta or "", "tokens_entrada": entrada,
                    "tokens_salida": salida, "segundos": segundos, "error": error or None}
        with open(_archivo, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")


def _modelo_usado(respuesta, kwargs: dict) -> str:
    """El modelo pedido o, si respondió una reserva, «pedido → reserva X». LiteLLM dice en la respuesta qué modelo y
    qué proveedor han respondido; se busca entre las reservas de la llamada."""
    pedido = str(kwargs.get("model", ""))
    usado = str(getattr(respuesta, "model", "") or "")
    proveedor = str((getattr(respuesta, "_hidden_params", None) or {}).get("custom_llm_provider") or "")
    reservas = [str(r.get("model", "") if isinstance(r, dict) else r) for r in kwargs.get("fallbacks") or []]
    for reserva in reservas:
        if usado and reserva.endswith(usado) and (not proveedor or reserva.startswith(f"{proveedor}/")):
            return f"{pedido} → reserva {reserva}"
    if proveedor and not pedido.startswith(f"{proveedor}/"):
        otra = next((r for r in reservas if r.startswith(f"{proveedor}/")), f"{proveedor}/{usado}")
        return f"{pedido} → reserva {otra}"
    return pedido


def _completion_registrada(*args, **kwargs):
    """litellm.completion, y si la llamada viene del gateway (lleva su tarea), se registra. La usan tanto CrewAI como
    completar(), así que sale una línea por llamada, con el modelo que de verdad ha respondido. Antes se registraba
    con los eventos de CrewAI, que solo saben el primer modelo: el 3 nov, el log decía Gemini 3.8 Flash en las 7 fases
    del roadmap y no había respondido en ninguna."""
    original = _completion_registrada.original
    metadata = kwargs.get("metadata")
    tarea = metadata.get("aidm_tarea") if isinstance(metadata, dict) else None
    if not tarea:
        return original(*args, **kwargs)
    inicio = time.monotonic()
    try:
        respuesta = original(*args, **kwargs)
    except Exception as exc:
        try:
            _registrar(tarea, str(kwargs.get("model", "")), kwargs.get("messages"), "", None,
                       time.monotonic() - inicio, f"{type(exc).__name__}: {exc}")
        except Exception:
            pass  # el registro nunca puede romper la llamada
        raise
    try:
        uso = getattr(respuesta, "usage", None)
        uso = {"prompt_tokens": getattr(uso, "prompt_tokens", None),
               "completion_tokens": getattr(uso, "completion_tokens", None)} if uso else None
        texto = respuesta.choices[0].message.content or ""
        _registrar(tarea, _modelo_usado(respuesta, kwargs), kwargs.get("messages"), texto, uso,
                   time.monotonic() - inicio)
    except Exception:
        pass  # una respuesta en streaming, por ejemplo: se devuelve igual, sin registrar
    return respuesta


if not getattr(litellm.completion, "aidm_gateway", False):  # una sola vez, aunque el módulo se importe de nuevo
    _completion_registrada.original = litellm.completion
    _completion_registrada.aidm_gateway = True
    litellm.completion = _completion_registrada