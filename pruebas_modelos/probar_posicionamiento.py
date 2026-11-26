"""Fallo 135: una sola llamada del cuadrante de posicionamiento, para verla antes de una ejecución.

Llama al modelo de verdad, con las claves del .env (una petición de Groq; no gasta Gemini). No va en tests/ y no
escribe data_structures/posicionamiento.json: enseña el JSON tras las guardias, lo que han quitado y lo que ha tardado.

Uso (desde la carpeta del proyecto, con el venv activado):
    python pruebas_modelos/probar_posicionamiento.py                      # Groq solo, con data_structures/problem_case.json
    python pruebas_modelos/probar_posicionamiento.py --caso .claude/casos/moda_cantabrica.json
    python pruebas_modelos/probar_posicionamiento.py --modelo cloudflare/@cf/openai/gpt-oss-120b
    python pruebas_modelos/probar_posicionamiento.py --perfil             # como en la ejecución: Groq y Cloudflare de reserva
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

import litellm

import gateway  # carga el .env
from agents import posicionamiento
from agents.discovery_agent_logic import _leer_json


def main() -> int:
    args = argparse.ArgumentParser(description="Una llamada del cuadrante de posicionamiento (fallo 135)")
    args.add_argument("--caso", default="data_structures/problem_case.json")
    args.add_argument("--modelo", default="groq/openai/gpt-oss-120b", help="sin reservas")
    args.add_argument("--perfil", action="store_true", help="el perfil del yaml, con sus reservas")
    opciones = args.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    caso = json.loads((RAIZ / opciones.caso).read_text(encoding="utf-8"))
    if falta := posicionamiento.faltan(caso):
        print(f"[posicionamiento] Caso sin datos (falta {', '.join(falta)}): no se llama al modelo. "
              f"Copia antes un caso: copy .claude\\casos\\moda_cantabrica.json data_structures\\problem_case.json")
        return 1
    mensajes = posicionamiento.mensajes(caso)
    prompt = posicionamiento.tokens_aproximados(mensajes[0]["content"] + mensajes[1]["content"])
    print(f"Prompt: unos {prompt} tokens (tope {posicionamiento.PROMPT_MAX}) + max_tokens {posicionamiento.MAX_TOKENS}"
          f" = {prompt + posicionamiento.MAX_TOKENS} (Groq gratis: 8.000 por minuto)")
    modelo = None if opciones.perfil else opciones.modelo
    if not opciones.perfil and opciones.modelo.startswith("groq/"):
        admite = litellm.get_supported_openai_params(model=opciones.modelo.partition("/")[2], custom_llm_provider="groq")
        print(f"LiteLLM {'pasa' if 'reasoning_effort' in (admite or []) else 'NO pasa (lo quita drop_params)'} "
              f"reasoning_effort a Groq con este modelo")

    inicio = time.monotonic()
    try:
        bruto = posicionamiento.pedir(caso, modelo)
    except Exception as exc:
        print(f"\nLa llamada ha fallado tras {time.monotonic() - inicio:.1f} s: {type(exc).__name__}: {exc}")
        return 1
    segundos = time.monotonic() - inicio
    datos = posicionamiento.limpiar(_leer_json(bruto, "posicionamiento", "no hay cuadrante"), caso)
    print(f"\nTiempo de la llamada: {segundos:.1f} s")
    if datos is None:
        print("Tras las guardias: sin cuadrante. Respuesta en bruto:\n" + bruto)
        return 1
    print("JSON tras las guardias:\n" + json.dumps(datos, ensure_ascii=False, indent=2))
    gateway.resumen_consumo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
