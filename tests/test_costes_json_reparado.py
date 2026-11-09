"""Comprueba el arreglo del fallo 50: un JSON de costes con un error de sintaxis se repara o se dice dónde falla.

En la ejecución sin discovery del 28 de octubre (Iberdrola, ministral-14b-2512), la respuesta era un JSON completo
entre ```json, con «componentes» en la raíz, pero no se podía leer: algún error de sintaxis lo rompía. El lector
saltaba al primer objeto que sí se leía (un componente) y el log decía «ningún objeto JSON tiene los campos de la
raíz», que despistaba. Las dos veces pasó lo mismo, así que el informe salió sin costes.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada que pasa cada respuesta por el
guardrail real de su tarea, con el bucle de reintentos de CrewAI. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_json_reparado.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import json
import logging
from types import SimpleNamespace

from crewai import Agent, Crew
from crewai.tasks.task_output import TaskOutput

import agents.estimation_crew as ec
from agents.estimation_crew import CostEstimationReport, _leer_json

MODELO = "mistral/ministral-14b-2512"
COMPONENTE = {"componente": "Azure IoT Hub (ingestión de 2000 concentradores MQTT, 50K dispositivos en piloto)",
              "categoria": "infraestructura", "precio_unitario_eur": 21, "unidades": 2, "unidad": "unidad S1",
              "periodicidad": "mensual", "referencia": "Precio público de Azure"}
VALIDO = json.dumps({
    "componentes": [COMPONENTE],
    "resumen_ejecutivo": {"meses_proyecto": 6, "presupuesto_cliente_max_eur": 600_000,
                          "justificacion": "Precios públicos de Azure."},
    "supuestos": ["La integración de alertas a SAP Fiori se implementará en fase 2."],
}, ensure_ascii=False, indent=2)

# Errores de sintaxis típicos, sobre una respuesta como la del 28 oct (```json, el JSON entero)
COMA_FINAL = "```json\n" + VALIDO.replace('fase 2."\n  ]', 'fase 2.",\n  ]') + "\n```"
COMILLAS = "```json\n" + VALIDO.replace("Precio público de Azure", 'Precio "público" de Azure') + "\n```"
COMENTARIO = "```json\n" + VALIDO.replace('"supuestos"', '// supuestos del piloto\n  "supuestos"') + "\n```"
OPERACION = "```json\n" + VALIDO.replace('"unidades": 2,', '"unidades": 2 * 30,') + "\n```"
# Una coma de más y además un precio en letra: se repara la sintaxis, pero el precio no vale
IRREPARABLE = COMA_FINAL.replace('"precio_unitario_eur": 21', '"precio_unitario_eur": "veintiuno"')

# 1) Coma de más, comillas sin escapar o un comentario: se repara y se lee entero
for nombre, respuesta in [("coma final", COMA_FINAL), ("comillas sin escapar", COMILLAS), ("comentario", COMENTARIO)]:
    informe = _leer_json(respuesta, CostEstimationReport)
    assert informe.componentes[0].unidades == 2 and len(informe.supuestos) == 1, nombre
print("OK 1: con una coma de más, comillas sin escapar o un comentario, el JSON se repara y se lee entero")

# 2) Una operación en vez de un número se calcula (json_repair se quedaría con el 2 y perdería el × 30)
assert _leer_json(OPERACION, CostEstimationReport).componentes[0].unidades == 60
print("OK 2: «2 * 30» en vez de un número se lee como 60")

# 2b) Lo que no se arregla dice qué falla y dónde estaba el error de sintaxis, no «ningún objeto JSON tiene los campos…»
try:
    _leer_json(IRREPARABLE, CostEstimationReport)
    raise AssertionError("debía fallar")
except ValueError as error:
    motivo = str(error)
assert "precio_unitario_eur" in motivo and "no es válido cerca del carácter" in motivo, motivo
assert "ningún objeto JSON tiene los campos" not in motivo
print("OK 2b: si tras repararlo sigue sin valer, el motivo dice el campo que falla y dónde estaba el error de sintaxis")

# 3) Con el bucle de reintentos real: se repara a la primera; si no se puede, la repetición pide revisar la sintaxis
pendientes: list[str] = []
contextos: list[str] = []


def execute_task(self, task, context=None, tools=None):
    contextos.append(context or "")
    return pendientes.pop(0)


Agent.execute_task = execute_task
respuestas: dict[str, list[str]] = {}


def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        primera, *resto = respuestas[task.name]
        pendientes[:] = resto
        salida = TaskOutput(description=task.description, raw=primera, agent=task.agent.role)
        task.output = task._invoke_guardrail_function(salida, task.agent, [], task._guardrail)
    return SimpleNamespace(raw="")


Crew.kickoff = kickoff_simulado
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
registros: list[logging.LogRecord] = []
captura = logging.Handler()
captura.emit = registros.append
logging.getLogger("aidm").addHandler(captura)
EQUIPO = json.dumps({"perfiles": [{"perfil": "Data Engineer", "nivel": "Senior", "horas_mes": 160}],
                     "meses_min": 6, "meses_max": 6})


def estimar(costes: list[str]):
    respuestas.update(costes=costes, perfiles=["## Data Engineer\n\n- Pipelines."], equipo=[EQUIPO])
    return ec.run_estimation_crew("PROPUESTA", MODELO, problem_case={}, componentes="ARQUITECTURA")


html = estimar([COMA_FINAL]).cost_estimation
assert "Azure IoT Hub" in html and "no se han podido estimar" not in html
assert any("JSON reparado con json_repair" in r.getMessage() for r in registros), "el log debe decir qué se reparó"
print("OK 3: la respuesta con la coma de más pasa a la primera, y el log dice qué se reparó")

contextos.clear()
html = estimar([IRREPARABLE, VALIDO]).cost_estimation
assert "Azure IoT Hub" in html and len(contextos) == 1
assert "Revisa la sintaxis" in contextos[0] and "precio_unitario_eur" in contextos[0]
print("OK 4: si no se puede reparar, la repetición señala el error y pide revisar la sintaxis")

print("\nTodo bien: el fallo 50 está arreglado (en la parte que se puede comprobar sin el LLM real).")