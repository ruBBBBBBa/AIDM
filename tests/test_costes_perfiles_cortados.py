"""Comprueba el arreglo del fallo 40 (C14): la descripción de los perfiles no puede llegar cortada sin avisar.

En la prueba del 22 de octubre, la sección de perfiles terminaba en la cabecera de una tabla
(«| **Perfil**                     | **Responsabilidades Clave**»), sin nombrar ningún perfil. La tarea del
coste presupuestó 4 perfiles que la descripción nunca llegó a dar.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada, y para el guardrail se usa el
bucle de reintentos real de CrewAI con la respuesta del agente simulada.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_perfiles_cortados.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

from types import SimpleNamespace

from crewai import Agent, Crew, Task
from crewai.tasks.task_output import TaskOutput

import agents.estimation_crew as ec
from agents.estimation_crew import (
    CostEstimationReport, EquipoEstimado, PerfilEquipo, _guardrail_perfiles, _perfiles_nombrados,
)

CORTADA_22OCT = (
    "A continuación, se detallan los **perfiles profesionales imprescindibles** para implementar el piloto en "
    "**4 meses**, considerando el alcance propuesto.\n\n---\n\n#### **Perfiles Necesarios para el Piloto**\n\n"
    "| **Perfil**                     | **Responsabilidades Clave**"
)
COMPLETA = (
    "### Data Engineer\n- Responsabilidades: pipelines de ADF desde el TMS y Navision.\n- Dedicación: completa.\n\n"
    "### Data Analyst/BI\n- Responsabilidades: informe semanal en Power BI.\n- Dedicación: completa."
)

# 1) Qué perfiles nombra cada texto
assert _perfiles_nombrados(CORTADA_22OCT, ec.PERFILES_CON_TARIFA) == []
assert _perfiles_nombrados(COMPLETA, ec.PERFILES_CON_TARIFA) == ["Data Engineer", "Data Analyst/BI"]
assert _perfiles_nombrados("**data analyst / BI** a media jornada", ["Data Analyst/BI"]) == ["Data Analyst/BI"]
print("OK 1: la salida del 22 oct no nombra ningún perfil de la tabla; una completa, sí")

# 2) El guardrail pide repetir una salida cortada una vez, y después acepta para no tumbar el informe
guardrail = _guardrail_perfiles()
ok, motivo = guardrail(SimpleNamespace(raw=CORTADA_22OCT))
assert ok is False and "sin tablas" in motivo
ok, texto = guardrail(SimpleNamespace(raw=CORTADA_22OCT))
assert ok is True and texto == CORTADA_22OCT, "la segunda vez se acepta: CrewAI lanzaría una excepción"
assert _guardrail_perfiles()(SimpleNamespace(raw=COMPLETA)) == (True, COMPLETA)
print("OK 2: el guardrail repite una vez la salida cortada y acepta una completa a la primera")

# 3) Con el bucle real de CrewAI: la repetición sustituye a la salida cortada
agente = Agent(role="Asesor", goal="Perfiles", backstory="Prueba", llm="groq/openai/gpt-oss-20b")
tarea = Task(description="Perfiles", expected_output="Perfiles", agent=agente,
             guardrail=_guardrail_perfiles(), guardrail_max_retries=1)
salida = TaskOutput(description="Perfiles", raw=CORTADA_22OCT, agent="Asesor")
contextos: list[str] = []
original = Agent.execute_task

def responde(textos):
    def execute_task(self, task, context=None, tools=None):
        contextos.append(context or "")
        return textos.pop(0)
    return execute_task

Agent.execute_task = responde([COMPLETA])
final = tarea._invoke_guardrail_function(salida, agente, [], tarea._guardrail)
assert final.raw == COMPLETA and len(contextos) == 1 and "sin tablas" in contextos[0]

# …y si la repetición también llega cortada, se acepta en vez de lanzar una excepción
tarea = Task(description="Perfiles", expected_output="Perfiles", agent=agente,
             guardrail=_guardrail_perfiles(), guardrail_max_retries=1)
Agent.execute_task = responde([CORTADA_22OCT])
final = tarea._invoke_guardrail_function(salida, agente, [], tarea._guardrail)
assert final.raw == CORTADA_22OCT
Agent.execute_task = original
print("OK 3: con el bucle de CrewAI, la tarea se repite una vez con el motivo y nunca tumba el informe")

# 4) Si la descripción no nombra perfiles presupuestados, la sección lo avisa antes de la tabla de coste
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
EQUIPO_22OCT = EquipoEstimado(
    perfiles=[PerfilEquipo(perfil="Data Engineer", nivel="Middle", horas_mes=160),
              PerfilEquipo(perfil="Data Analyst/BI", nivel="Middle", horas_mes=160),
              PerfilEquipo(perfil="AI Engineer", nivel="Junior", horas_mes=120),
              PerfilEquipo(perfil="Platform Engineer", nivel="Middle", horas_mes=120)],
    meses_min=4, meses_max=6,
)
prompts: list[str] = []
tareas: list[Task] = []
texto_perfiles = CORTADA_22OCT

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        tareas.append(task)
        if task.output_pydantic is CostEstimationReport:
            task.output = SimpleNamespace(pydantic=None, raw="COSTES")
        elif task.output_pydantic is EquipoEstimado:
            task.output = SimpleNamespace(pydantic=EQUIPO_22OCT, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw=texto_perfiles)
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
caso = {"restriccion_presupuestaria": {"description": "", "value": "90.000 €"}}
perfiles = ec.run_estimation_crew("PROPUESTA", "groq/openai/gpt-oss-20b", problem_case=caso).team_profiles
aviso = ("**Ojo:** la descripción de arriba no menciona a Data Engineer, Data Analyst/BI, AI Engineer, "
         "Platform Engineer; puede haber llegado cortada")
assert aviso in perfiles, "falta el aviso de la descripción cortada"
assert perfiles.index(aviso) < perfiles.index("## Estimación de Coste del Equipo")

texto_perfiles = COMPLETA
perfiles = ec.run_estimation_crew("PROPUESTA", "groq/openai/gpt-oss-20b", problem_case=caso).team_profiles
assert "no menciona a AI Engineer, Platform Engineer;" in perfiles and "Data Engineer, Data Analyst/BI, AI" not in perfiles
print("OK 4: la sección avisa de los perfiles presupuestados que la descripción no nombra")

# 5) La tarea pide apartados sin tablas y lleva el guardrail
tarea_perfiles = tareas[1]
assert "Escribe un apartado por perfil, con su nombre como título y una lista debajo; no uses tablas" in prompts[1]
assert tarea_perfiles.guardrail is not None and tarea_perfiles.guardrail_max_retries == 1
print("OK 5: la tarea de perfiles pide un apartado por perfil sin tablas y tiene el guardrail")

print("\nTodo bien: el fallo 40 (C14) está arreglado (en la parte que se puede comprobar sin el LLM real).")