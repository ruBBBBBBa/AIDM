"""Comprueba el arreglo del fallo 21: el discovery no guarda cifras que el usuario no ha dicho.

Los valores salen de la ejecución real del 14 de octubre con Ministral 14B.
No llama a ningún LLM (se simula su respuesta) ni toca tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_discovery_inventos.py
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
import agents.discovery_agent_logic as discovery
from agents.discovery_agent_logic import _cifras, _sin_cifras_inventadas

DICHO = ("Todo en cloud. El TMS en SQL Server, la telemetría de los camiones que llega por una API cada "
         "5 minutos y el ERP, que es Navision. Un piloto funcionando en 4 meses. Como máximo 90.000 € el primer año.")

# 1) Qué cuenta como cifra
assert _cifras("90.000 €") == _cifras("90000 euros") == {"90000"}
assert _cifras("(1) SQL Server 2019, 2. Navision") == {"2019"}, "las enumeraciones no son cifras"
assert _cifras("100% en la nube") == set(), "«100 %» es «todo», no una cifra"
print("OK 1: 90.000 y 90000 cuentan igual; (1), 2. y «100 %» no cuentan como cifras")

# 2) Valores reales de la ejecución: se bloquean solo los que llevan cifras inventadas
updates = [
    {"field_key": "volumen_de_datos", "operation": "replace",
     "value": "Volumen estimado: telemetría de flota ~100-500 MB/hora + históricos de cientos de GB a TB"},
    {"field_key": "plazo_objetivo", "operation": "replace",
     "value": "Primer MVP en 4 meses, para un subconjunto de la flota (ej: 20-30% de la flota total)"},
    {"field_key": "modelo_de_despliegue", "operation": "replace", "value": "100% en la nube (Azure)"},
    {"field_key": "integration_scope", "operation": "replace",
     "value": "(1) TMS en SQL Server, (2) telemetría por API cada 5 minutos, (3) ERP Navision"},
    {"field_key": "restriccion_presupuestaria", "operation": "replace", "value": "Máximo 90000 € el primer año"},
    {"field_key": "evidencias", "operation": "merge",
     "value": ["Telemetría cada 5 minutos", "La telemetría (100-500 MB/hora) limita el alcance del MVP"]},
]
limpios = {u["field_key"]: u["value"] for u in _sin_cifras_inventadas(updates, DICHO)}
assert "volumen_de_datos" not in limpios, "el volumen inventado no debe guardarse"
assert "plazo_objetivo" not in limpios, "el «20-30 % de la flota» no lo ha dicho el usuario"
assert {"modelo_de_despliegue", "integration_scope", "restriccion_presupuestaria"} <= limpios.keys()
assert limpios["evidencias"] == ["Telemetría cada 5 minutos"], "solo se quita la evidencia con cifras inventadas"
print("OK 2: se bloquean el volumen y el plazo inventados; lo que dijo el usuario se guarda")

# 3) El discovery completo, con el LLM simulado devolviendo el volumen inventado
tmp = Path(tempfile.mkdtemp()) / "problem_case.json"
shutil.copy("data_structures/problem_case_recovery.json", tmp)
utils.PROBLEM_CASE_PATH = tmp  # la tool lee y escribe aquí, no en tu caso real

prompts_enviados = []
respuesta = {
    "updates": [updates[0], {"field_key": "modelo_de_despliegue", "operation": "replace", "value": "Todo en cloud"}],
    "assistant_reply": "¿Cuál es el plazo objetivo?",
    "update_status": "pending fields",
}

def llm_simulado(**kwargs):
    prompts_enviados.append(kwargs["messages"][0]["content"])
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps(respuesta, ensure_ascii=False)))])

os.environ["BIG_MODEL"] = "simulado/modelo"
discovery.litellm.completion = llm_simulado
historial = [{"role": "assistant", "content": "Describe tu proyecto"}, {"role": "user", "content": DICHO}]
discovery.run_agent(DICHO, chat_history=historial)

caso = json.loads(tmp.read_text(encoding="utf-8"))
assert caso["modelo_de_despliegue"]["value"] == "Todo en cloud"
assert caso["volumen_de_datos"]["value"] == "", "el volumen inventado no debe llegar al caso"
print("OK 3: en el discovery completo, el volumen inventado no llega al caso y el resto sí")

# 4) El prompt nuevo
prompt = prompts_enviados[0]
assert "SOLO lo que el usuario ha dicho" in prompt and "Nunca escribas cifras" in prompt
assert "no un objeto" in prompt, "debe pedir assistant_reply como texto"
assert "Sé descriptivo" not in prompt and "que puedas deducir" not in prompt, "quedan instrucciones del prompt viejo"
print("OK 4: el prompt pide solo lo dicho, sin cifras inventadas y con assistant_reply como texto")

print("\nTodo bien: el fallo 21 está arreglado (en la parte que se puede comprobar sin el LLM real).")