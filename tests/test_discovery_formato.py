"""Comprueba que el discovery no pierde turnos por detalles de formato (fallo 22).

Los casos salen de la ejecución real del 14 de octubre con Ministral 14B:
  1. assistant_reply llega como objeto {"text": "..."} en vez de texto
  2. un valor trae saltos de línea sin escapar dentro del JSON
  3. un print con "≥" falla cuando la salida va a un archivo en Windows (cp1252)

No llama a ningún LLM ni toca ningún archivo.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_discovery_formato.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import io
import types

from agents.discovery_agent_logic import _parse_plan
from utils import LIST_FIELDS, TEXT_FIELDS

CAMPOS = set(TEXT_FIELDS) | set(LIST_FIELDS)

# 1) assistant_reply como objeto
raw = '''```json
{
  "updates": [{"field_key": "plazo_objetivo", "value": "Piloto en 4 meses", "operation": "replace"}],
  "assistant_reply": {"text": "¿Existen regulaciones que deban considerarse?"},
  "update_status": "pending fields"
}
```'''
plan = _parse_plan(raw, CAMPOS)
assert plan["assistant_reply"] == "¿Existen regulaciones que deban considerarse?", plan["assistant_reply"]
assert len(plan["updates"]) == 1
print("OK 1: si assistant_reply llega como {\"text\": ...}, se usa su texto")

# 2) Saltos de línea sin escapar dentro de un valor (JSON estrictamente inválido)
raw = '''```json
{
  "updates": [
    {"field_key": "restriccion_presupuestaria", "value": "Máximo 90.000 € el primer año", "operation": "replace"},
    {"field_key": "kpis_de_exito", "value": "KPIs:
      1. Bajar los retrasos un 20 %
      2. Informe semanal automatizado", "operation": "replace"}
  ],
  "assistant_reply": "¿Algo más?",
  "update_status": "pending fields"
}
```'''
plan = _parse_plan(raw, CAMPOS)
assert [u["field_key"] for u in plan["updates"]] == ["restriccion_presupuestaria", "kpis_de_exito"], plan
assert "Bajar los retrasos un 20 %" in plan["updates"][1]["value"]
print("OK 2: un valor con saltos de línea sin escapar ya no hace perder el turno entero")

# 3) Un print con "≥" cuando la salida es cp1252 (lo que pasa en Windows al redirigir a un archivo)
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

grafo = types.ModuleType("graph")

def run_graph(prompt, chat_history=None):
    print("Raw output del agente de discovery: precisión ≥ 70 %")
    return {"assistant_response": "Respuesta normal del discovery", "completion_reached": False}

grafo.run_graph = run_graph
sys.modules["graph"] = grafo

salida_original = sys.stdout
sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")  # como un archivo en Windows
try:
    at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
    at.run()
    at.chat_input[0].set_value("hola").run()
finally:
    sys.stdout = salida_original

respuesta = at.chat_message[-1].markdown[0].value
assert respuesta == "Respuesta normal del discovery", f"el print con ≥ ha roto el turno: {respuesta[:80]}"
print("OK 3: un print con '≥' ya no rompe el turno aunque la salida vaya a un archivo")

print("\nTodo bien: el discovery ya no pierde turnos por el formato.")