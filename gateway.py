"""Gateway de modelos: cada tarea de AIDM pide aquí su modelo.

Decisión del 20 oct: dejar de usar el mismo BIG_MODEL en casi todo y asignar a cada agente o tarea el suyo.
La asignación está en data_structures/modelos.yaml. Por defecto es la de antes: BIG_MODEL en casi todo y los
REASONING_* en parte del roadmap.

Para probar otro modelo en una tarea sin tocar el yaml, ponlo en el .env como MODELO_<TAREA>:
    MODELO_COSTES=groq/openai/gpt-oss-120b
"""
import os
import re
from pathlib import Path

import yaml
from crewai import LLM
from dotenv import load_dotenv

load_dotenv(".env")

MODELOS_PATH = Path("data_structures/modelos.yaml")
_VARIABLE = re.compile(r"\$\{(\w+)\}")


def _config() -> dict:
    """Se lee en cada llamada: un cambio en el yaml vale para la siguiente ejecución, sin reiniciar Streamlit."""
    with open(MODELOS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolver(valor) -> str:
    """«${BIG_MODEL}» → el valor de esa variable del .env; un nombre de modelo se queda igual."""
    return _VARIABLE.sub(lambda m: os.getenv(m.group(1), ""), str(valor or "")).strip()


def modelo(tarea: str) -> str:
    """El modelo de una tarea: MODELO_<TAREA> del .env; si no, el del yaml; si queda vacío, el de «por_defecto».

    Escribe en el log qué modelo usa cada tarea y de dónde sale, para comparar ejecuciones.
    """
    config = _config()
    variable = f"MODELO_{tarea.upper()}"
    elegido, origen = os.getenv(variable, "").strip(), f"{variable} del .env"
    if not elegido:
        tareas = config.get("tareas") or {}
        if tarea not in tareas:
            print(f"[gateway] Ojo: la tarea «{tarea}» no está en {MODELOS_PATH}; se usa el modelo por defecto")
        elegido, origen = _resolver(tareas.get(tarea)), MODELOS_PATH.name
    if not elegido:
        elegido, origen = _resolver(config.get("por_defecto")), "por defecto"
    print(f"[gateway] {tarea}: {elegido or '(sin modelo)'} ({origen})")
    return elegido


def parametros(model_name: str, **extra) -> dict:
    """Reintentos, tope de salida y modelo de reserva de un modelo, para el LLM de CrewAI o litellm.completion.

    Lo que se pase en `extra` (p. ej. num_retries=3) manda sobre las reglas.
    """
    config = _config()
    reglas = (config.get("modelos") or {}).get(model_name) or {}
    params = {"num_retries": 2}
    # Cloudflare corta a 256 tokens si no se le indica un tope
    max_tokens = reglas.get("max_tokens") or (4096 if model_name.startswith("cloudflare/") else None)
    if max_tokens:
        params["max_tokens"] = max_tokens
    if reglas.get("reserva"):
        params["fallbacks"] = [{"model": reglas["reserva"], "max_tokens": reglas.get("max_tokens_reserva", 6000)}]
    return {**params, **extra}


def build_llm(model_name: str):
    """LLM para CrewAI con reintentos, tope de salida explícito y modelo de reserva (antes estaba en utils.py)."""
    if model_name.startswith("gemini/"):
        return model_name  # cliente nativo de CrewAI, no admite estos parámetros
    return LLM(model=model_name, **parametros(model_name))
