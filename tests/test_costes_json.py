"""Comprueba el arreglo del fallo 46: los costes no dependen de las llamadas a herramienta.

En la ejecución sin discovery del 26 de octubre (Iberdrola, ministral-14b-2512), la tarea de costes tenía
output_pydantic, así que CrewAI pedía la respuesta como llamada a herramienta. El modelo devolvió el JSON como
texto, falló tres veces con «No tool calls or function call found in response (mode: TOOLS)» y el informe se
quedó colgado. Además, los importes venían con la unidad mal: 0,10 €/mensaje × 10 millones = 1 M€/mes.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada que pasa cada respuesta por el
guardrail real de su tarea, con el bucle de reintentos de CrewAI. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_json.py
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

from crewai import Agent, Crew, Task
from crewai.tasks.task_output import TaskOutput

import agents.estimation_crew as ec
from agents.estimation_crew import (
    SIN_JSON, CostEstimationReport, EquipoEstimado, _format_cost_report, _guardrail_json, _leer_json,
)

MODELO = "mistral/ministral-14b-2512"

# Los primeros componentes que devolvió ministral el 26 oct, con el presupuesto de Iberdrola
COMPONENTES_26OCT = [
    {"componente": "Azure IoT Hub para ingestión de telemetría MQTT", "categoria": "infraestructura",
     "precio_unitario_eur": 0.008, "unidades": 10_000_000, "unidad": "mensaje", "periodicidad": "mensual",
     "referencia": "Precio público Azure (tarifa por mensaje IoT Hub)"},
    {"componente": "Azure Event Hubs para buffer de mensajes", "categoria": "infraestructura",
     "precio_unitario_eur": 0.10, "unidades": 10_000_000, "unidad": "mensaje", "periodicidad": "mensual",
     "referencia": "Precio público Azure (tarifa por mensaje Event Hubs)"},
    {"componente": "Azure Data Lake Storage Gen2 (almacenamiento)", "categoria": "infraestructura",
     "precio_unitario_eur": 0.02, "unidades": 20_000, "unidad": "GB", "periodicidad": "mensual",
     "referencia": "Precio público Azure (tarifa de almacenamiento ADLS Gen2)"},
]
JSON_26OCT = json.dumps({
    "componentes": COMPONENTES_26OCT,
    "resumen_ejecutivo": {"meses_proyecto": 6, "presupuesto_cliente_max_eur": 600_000, "contingencia_pct": 5,
                          "justificacion": "Precios públicos de Azure."},
    "supuestos": ["Se asume un piloto de 50.000 dispositivos."],
}, ensure_ascii=False, indent=2)
EQUIPO_JSON = json.dumps({"perfiles": [{"perfil": "Data Engineer", "nivel": "Senior", "horas_mes": 160}],
                          "meses_min": 6, "meses_max": 6})
EN_BLOQUE = f"```json\n{JSON_26OCT}\n```"
CON_TEXTO_ANTES = f"Thought: ya tengo la estimación.\nAquí tienes el JSON pedido:\n{JSON_26OCT}\nEspero que sirva."
SIN_NADA = "Los costes dependen de la configuración final; no puedo darlos todavía."

# 1) El lector de JSON
assert len(_leer_json(EN_BLOQUE, CostEstimationReport).componentes) == 3
assert len(_leer_json(CON_TEXTO_ANTES, CostEstimationReport).componentes) == 3
envuelto = '{"CostEstimationReport": ' + JSON_26OCT + "}"
assert _leer_json(envuelto, CostEstimationReport).resumen_ejecutivo.presupuesto_cliente_max_eur == 600_000
for malo, motivo in [(SIN_NADA, "no hay ningún objeto JSON"), ('{"componentes": "ninguno"}', "componentes")]:
    try:
        _leer_json(malo, CostEstimationReport)
        raise AssertionError("debía fallar")
    except ValueError as error:
        assert motivo in str(error), error
print("OK 1: el JSON se lee entre ```json, con texto antes o envuelto; sin JSON, el motivo es claro")

# 2) El guardrail con el bucle real de CrewAI: repite una vez y nunca lanza una excepción
contextos: list[str] = []
pendientes: list[str] = []

def execute_task(self, task, context=None, tools=None):  # la repetición que pide el guardrail
    contextos.append(context or "")
    return pendientes.pop(0)

Agent.execute_task = execute_task
agente = Agent(role="Costes", goal="Costes", backstory="Prueba", llm="groq/openai/gpt-oss-20b")

def pasar_por_guardrail(task, respuestas):
    """Lo que hace CrewAI al terminar una tarea: la primera respuesta y, si el guardrail la rechaza, la repetición."""
    pendientes[:] = respuestas[1:]
    salida = TaskOutput(description=task.description, raw=respuestas[0], agent=task.agent.role)
    return task._invoke_guardrail_function(salida, task.agent, [], task._guardrail)

tarea = Task(description="Costes", expected_output="JSON", agent=agente,
             guardrail=_guardrail_json(CostEstimationReport, "costes"), guardrail_max_retries=1)
final = pasar_por_guardrail(tarea, [SIN_NADA, EN_BLOQUE])
assert CostEstimationReport.model_validate_json(final.raw).componentes[0].unidad == "mensaje"
assert len(contextos) == 1 and "Responde solo con el objeto JSON" in contextos[0]
assert '"periodicidad": "mensual|anual|unico"' in contextos[0], "la repetición lleva la forma del JSON"

tarea = Task(description="Costes", expected_output="JSON", agent=agente,
             guardrail=_guardrail_json(CostEstimationReport, "costes"), guardrail_max_retries=1)
assert pasar_por_guardrail(tarea, [SIN_NADA, SIN_NADA]).raw == SIN_JSON
print("OK 2: sin JSON, la tarea se repite una vez con el motivo; a la segunda, un marcador en vez de una excepción")

# 3) run_estimation_crew entero, con las respuestas pasando por los guardrails de verdad
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
respuestas: dict[str, list[str]] = {}
prompts: dict[str, str] = {}
tareas: dict[str, Task] = {}
fallo_crew: Exception | None = None

def kickoff_simulado(self, *args, **kwargs):
    if fallo_crew:
        raise fallo_crew
    for task in self.tasks:
        prompts[task.name], tareas[task.name] = task.description, task
        task.output = pasar_por_guardrail(task, respuestas[task.name])
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
PERFILES = "## Data Engineer\n\n- Construye los pipelines."
caso = {"restriccion_presupuestaria": {"description": "", "value": "600.000 € el primer año"}}

def estimar(costes: list[str]):
    respuestas.update(costes=costes, perfiles=[PERFILES], equipo=[EQUIPO_JSON])
    return ec.run_estimation_crew("PROPUESTA ENTERA (con gobernanza y BI)", MODELO, problem_case=caso,
                                  componentes="ARQUITECTURA: IoT Hub, Event Hubs y ADLS Gen2")

for costes, como in [([EN_BLOQUE], "entre ```json"), ([CON_TEXTO_ANTES], "con texto antes")]:
    html = estimar(costes).cost_estimation
    assert "no se han podido estimar" not in html and "Azure Event Hubs para buffer de mensajes" in html, como
print("OK 3: con el JSON entre ```json o con texto antes, la sección de costes sale completa")

# 4) Sin JSON dos veces: el informe sale con el aviso, y el equipo se calcula igual
registros: list[logging.LogRecord] = []
captura = logging.Handler()
captura.emit = registros.append
logging.getLogger("aidm").addHandler(captura)

resultado = estimar([SIN_NADA, SIN_NADA])
assert "Ojo: no se han podido estimar los costes." in resultado.cost_estimation
assert MODELO in resultado.cost_estimation
assert "## Estimación de Coste del Equipo" in resultado.team_profiles
assert any(MODELO in r.getMessage() and "Sin costes" in r.getMessage() for r in registros)
print("OK 4: sin JSON dos veces, la sección de costes avisa y el resto del informe sigue")

# 5) Si la crew lanza una excepción, el log dice el modelo y el error, y el informe sale igual
fallo_crew = RuntimeError("No tool calls or function call found in response (mode: TOOLS)")
for t in tareas.values():
    t.output = None
resultado = estimar([EN_BLOQUE])
fallo_crew = None
assert "Ojo: no se han podido estimar los costes." in resultado.cost_estimation
assert "no se han podido describir los perfiles" in resultado.team_profiles
assert any(MODELO in r.getMessage() and "mode: TOOLS" in r.getMessage() for r in registros), "falta la línea del log"
print("OK 5: si la crew falla, el log dice el modelo y el error, y el informe no se queda colgado")

# 6) Las tareas: sin output_pydantic, con guardrail, y la de costes solo con la arquitectura
estimar([EN_BLOQUE])
for nombre in ("costes", "equipo"):
    assert tareas[nombre].output_pydantic is None and tareas[nombre].guardrail_max_retries == 1
    assert "Responde solo con un objeto JSON" in prompts[nombre]
assert "ARQUITECTURA: IoT Hub" in prompts["costes"] and "PROPUESTA ENTERA" not in prompts["costes"]
assert "DATOS DEL CLIENTE" in prompts["costes"] and "600.000 €" in prompts["costes"]
assert "PROPUESTA ENTERA" in prompts["perfiles"], "los perfiles siguen viendo la propuesta entera"
print("OK 6: costes y equipo ya no usan output_pydantic; costes recibe solo la arquitectura y los datos del cliente")

# 7) Un componente que cada mes se lleva más del 5 % del presupuesto (fallo 51; antes, 10 %) se marca «revisar unidad»
html = _format_cost_report(CostEstimationReport.model_validate_json(JSON_26OCT))
aviso = ("Ojo: revisa la unidad de Azure IoT Hub para ingestión de telemetría MQTT (80.000 €/mes) y "
         "Azure Event Hubs para buffer de mensajes (1.000.000 €/mes): cada uno cuesta al mes más de un 5 % del "
         "presupuesto del cliente (600.000 €).")
assert aviso in html, "falta el aviso de «revisar unidad»"
assert html.count("(revisar unidad)") == 2, "ADLS Gen2 (400 €/mes) no debe marcarse"
assert "Van sumados en el total, que puede estar inflado." in html
sin_presupuesto = json.loads(JSON_26OCT)
sin_presupuesto["resumen_ejecutivo"]["presupuesto_cliente_max_eur"] = 0
assert "revisar unidad" not in _format_cost_report(CostEstimationReport.model_validate(sin_presupuesto))
print("OK 7: IoT Hub (80.000 €/mes) y Event Hubs (1 M€/mes) salen «revisar unidad» con 600.000 € de presupuesto")

print("\nTodo bien: el fallo 46 está arreglado (en la parte que se puede comprobar sin el LLM real).")