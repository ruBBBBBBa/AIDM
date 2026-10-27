"""Comprueba el arreglo del fallo 47: el JSON de costes se acepta aunque venga envuelto, y el log dice qué llegó.

En la ejecución sin discovery del 27 de octubre (Iberdrola, ministral-14b-2512), la tarea de costes falló dos
veces con «componentes / resumen_ejecutivo / supuestos: Field required»: el modelo devolvió un JSON, pero sin
los campos en la raíz, y el log no enseñaba la respuesta. Las causas posibles eran tres: el JSON venía envuelto
en otra clave, venía como llamada a función o llegó cortado (y lo primero que se podía leer era un componente).

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada que pasa cada respuesta por el
guardrail real de su tarea, con el bucle de reintentos de CrewAI. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_json_envuelto.py
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
INFORME = {
    "componentes": [{"componente": "Azure IoT Hub", "categoria": "infraestructura", "precio_unitario_eur": 21,
                     "unidades": 2, "unidad": "unidad S1", "periodicidad": "mensual",
                     "referencia": "Precio público de Azure"}],
    "resumen_ejecutivo": {"meses_proyecto": 6, "presupuesto_cliente_max_eur": 600_000,
                          "justificacion": "Precios públicos de Azure."},
    "supuestos": ["Se asume un piloto de 50.000 dispositivos."],
}
TEXTO = json.dumps(INFORME, ensure_ascii=False, indent=2)
ENVUELTO = json.dumps({"estimacion_costes": {"informe": INFORME}}, ensure_ascii=False)
PROPERTIES = json.dumps({"type": "object", "properties": INFORME}, ensure_ascii=False)
FUNCION = json.dumps({"name": "CostEstimationReport", "arguments": TEXTO}, ensure_ascii=False)
ESQUEMA = json.dumps(CostEstimationReport.model_json_schema(), ensure_ascii=False)
CORTADO = TEXTO[:TEXTO.index('"referencia"') + 30]  # se acaba a mitad del primer componente

# 1) Envuelto en una o dos claves, bajo «properties» o como llamada a función: se acepta
for nombre, respuesta in [("envuelto en dos claves", ENVUELTO), ("bajo properties", PROPERTIES),
                          ("como llamada a función", FUNCION), ("entre ```json", f"```json\n{ENVUELTO}\n```")]:
    informe = _leer_json(respuesta, CostEstimationReport)
    assert informe.componentes[0].componente == "Azure IoT Hub", nombre
print("OK 1: el JSON vale envuelto en otra clave, bajo «properties» o como llamada a función")


def motivo(respuesta: str) -> str:
    try:
        _leer_json(respuesta, CostEstimationReport)
    except ValueError as error:
        return str(error)
    raise AssertionError("debía fallar")


# 2) Los errores dicen qué pasó: cortado, esquema sin rellenar u otras claves
assert "llegó cortado" in motivo(CORTADO)
assert "componentes: Input should be a valid list" in motivo(ESQUEMA), motivo(ESQUEMA)
otras = motivo('{"costes": [1, 2], "total": 5}')
assert "ningún objeto JSON tiene los campos de la raíz" in otras and "['costes', 'total']" in otras
print("OK 2: el motivo distingue un JSON cortado, el esquema sin rellenar y un JSON con otras claves")

# 3) run_estimation_crew con el bucle de reintentos real
pendientes: list[str] = []
contextos: list[str] = []


def execute_task(self, task, context=None, tools=None):  # la repetición que pide el guardrail
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


html = estimar([ENVUELTO]).cost_estimation
assert "no se han podido estimar" not in html and "Azure IoT Hub" in html
print("OK 3: con el JSON envuelto, la sección de costes sale completa a la primera")

# 4) El esquema sin rellenar dos veces: se repite con el motivo y, a la segunda, el informe sale sin costes
contextos.clear()
registros.clear()
html = estimar([ESQUEMA, ESQUEMA]).cost_estimation
assert "Ojo: no se han podido estimar los costes." in html
assert len(contextos) == 1 and "La raíz del JSON tiene que tener estos campos: componentes, resumen_ejecutivo, supuestos" in contextos[0]
mensajes = [r.getMessage() for r in registros]
assert any("se repite. Respuesta:" in m and "caracteres; empieza" in m and "acaba" in m for m in mensajes), mensajes
print("OK 4: con el esquema sin rellenar se repite una vez y el informe sale sin costes; el log enseña la respuesta")

# 5) Un JSON cortado pide una respuesta más breve
contextos.clear()
html = estimar([CORTADO, TEXTO]).cost_estimation
assert "Azure IoT Hub" in html and "Sé más breve" in contextos[0]
print("OK 5: si el JSON llega cortado, la repetición pide textos más breves")

print("\nTodo bien: el fallo 47 está arreglado (en la parte que se puede comprobar sin el LLM real).")