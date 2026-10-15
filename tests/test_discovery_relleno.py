"""Comprueba el arreglo del fallo 2: un caso relleno con "no mencionado" no cuenta como completo.

No llama a ningún LLM (se simula su respuesta) ni toca tu problem_case.json:
trabaja sobre una copia temporal de la plantilla vacía.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_discovery_relleno.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import json
import shutil
import tempfile
from types import SimpleNamespace

import utils
from utils import es_valor_de_relleno
import agents.discovery_agent_logic as discovery

PLANTILLA = Path("data_structures/problem_case_recovery.json")


def caso_temporal() -> Path:
    tmp = Path(tempfile.mkdtemp()) / "problem_case.json"
    shutil.copy(PLANTILLA, tmp)
    utils.PROBLEM_CASE_PATH = tmp  # la tool lee y escribe aquí, no en el caso real
    return tmp


def leer(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# 1) Qué cuenta como relleno y qué no
for valor in ["no mencionado", "No mencionado.", "NO ESPECIFICADO", "No se menciona en la conversación",
              "N/A", "Sin información", "desconocido", "-", "", "   ", None]:
    assert es_valor_de_relleno(valor), f"debería ser relleno: {valor!r}"
for valor in ["FarmaNorte", "Azure", "No tienen equipo de datos interno", "Ninguna regulación específica",
              "Desconocido; estiman unos 2 TB de histórico", "Confirmado por el usuario: no aplica"]:
    assert not es_valor_de_relleno(valor), f"es un dato real: {valor!r}"
print("OK 1: los rellenos se reconocen y los datos reales no se confunden con ellos")

# 2) El escenario de la revisión: el LLM rellena todo con "no mencionado" y da el caso por cerrado
path = caso_temporal()
campos = [k for k, v in leer(path).items() if isinstance(v, dict) and isinstance(v.get("value"), str)]
respuesta_simulada = {
    "updates": [{"field_key": k, "value": "no mencionado", "operation": "replace"} for k in campos]
               + [{"field_key": "evidencias", "value": ["mensaje corto"], "operation": "merge"}],
    "assistant_reply": "Perfecto, ya tengo todo lo que necesito.",
    "update_status": "completed",
}
respuesta_simulada["updates"][0] = {"field_key": "nombre_del_cliente", "value": "ClienteX", "operation": "replace"}

os.environ["BIG_MODEL"] = "simulado/modelo"
discovery.litellm.completion = lambda **kw: SimpleNamespace(
    choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(respuesta_simulada, ensure_ascii=False)))]
)

respuesta_chat = discovery.run_agent("hola, quiero un proyecto de datos", chat_history=[])
caso = leer(path)
assert caso["status"] == "pending fields", f"el caso no debería estar completo: {caso['status']}"
assert caso["nombre_del_cliente"]["value"] == "ClienteX", "el dato real sí debe guardarse"
assert caso["sector"]["value"] == "", "'no mencionado' no debe guardarse"
assert "ya tengo todo" not in respuesta_chat, "el chat no debe despedirse si faltan datos"
assert respuesta_chat.startswith("Aún me faltan"), respuesta_chat
print("OK 2: el caso sigue en 'pending fields' y el chat pregunta lo que falta")
print("      Chat:", respuesta_chat)

# 3) Un caso guardado antes del arreglo con "no mencionado" también cuenta como pendiente
caso_viejo = {"status": "completed",
              "sector": {"description": "Sector", "value": "no mencionado"},
              "cloud_provider": {"description": "Cloud", "value": "Azure"}}
pendientes = discovery._get_pending_fields(caso_viejo)
assert pendientes == ["sector: Sector"], pendientes
print("OK 3: un 'no mencionado' ya guardado cuenta como pendiente")

# 4) Una respuesta explícita del usuario sí se guarda y completa el campo
path = caso_temporal()
discovery._apply_plan({"updates": [{"field_key": "regulacion_y_compliance",
                                    "value": "Confirmado por el usuario: no aplica", "operation": "replace"}],
                       "update_status": None})
assert leer(path)["regulacion_y_compliance"]["value"] == "Confirmado por el usuario: no aplica"
print("OK 4: 'Confirmado por el usuario: no aplica' se guarda como respuesta válida")

print("\nTodo bien: el fallo 2 está arreglado.")