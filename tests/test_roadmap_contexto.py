"""Comprueba el arreglo del fallo 4: el roadmap recibe el caso y ETL ve lo que hizo la primera crew.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt
de cada tarea y devuelve una salida simulada. Tampoco toca tu informe: el roadmap se
inyecta en una copia temporal.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_roadmap_contexto.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import hashlib
import shutil
import tempfile
from types import SimpleNamespace

# Modelos de relleno solo para poder crear los agentes; no se llega a llamar a ninguno.
for clave in ("BIG_MODEL", "REASONING_MODEL", "REASONING_RIGOROUS_MODEL", "SMALL_REASONING_MODEL"):
    os.environ.setdefault(clave, "groq/openai/gpt-oss-20b")

from crewai import Crew

import agents.roadmap_builder as rb
import graph

INFORME = Path("data_structures/solution_builder_report.html")
huella_antes = hashlib.md5(INFORME.read_bytes()).hexdigest()

copia = Path(tempfile.mkdtemp()) / "informe.html"
shutil.copy(INFORME, copia)
rb.REPORT_PATH = copia  # el roadmap se inyecta en la copia, no en tu informe

# Crew.kickoff simulado: guarda el prompt de cada tarea y devuelve "SALIDA n"
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    if self.task_callback is None:  # la crew del resumen
        prompts.append(self.tasks[0].description)
        return SimpleNamespace(raw="RESUMEN SIMULADO")
    for task in self.tasks:
        prompts.append(task.description)
        self.task_callback(SimpleNamespace(raw=f"SALIDA {len(prompts) - 1} ({task.agent.role})"))
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado

caso = {
    "status": "completed",
    "nombre_del_cliente": {"description": "Cliente", "value": "FarmaNorte"},
    "volumen_de_datos": {"description": "Volumen", "value": "2 TB de histórico"},
    "restriccion_presupuestaria": {"description": "Presupuesto", "value": "150.000 a 200.000 euros"},
    "regulacion_y_compliance": {"description": "Regulación", "value": "no mencionado"},
    "evidencias": {"description": "Evidencias", "value": ["Frase larga de la conversación"]},
}

# 1) caso_compacto: solo valores reales, sin descripciones, rellenos ni evidencias
compacto = rb.caso_compacto(caso)
assert "- volumen_de_datos: 2 TB de histórico" in compacto
assert "no mencionado" not in compacto and "Frase larga" not in compacto and "Volumen" not in compacto
print("OK 1: el caso compacto lleva solo los datos reales")

# 2) Las 7 tareas del roadmap reciben el caso
rb.roadmap_builder("PROPUESTA SIMULADA", problem_case=caso)
assert len(prompts) == 8, f"se esperaban 8 prompts (resumen + 7 tareas) y hay {len(prompts)}"
tareas = prompts[1:]
for n, prompt in enumerate(tareas, start=1):
    assert "DATOS DEL CLIENTE" in prompt and "2 TB de histórico" in prompt, f"la tarea {n} no lleva el caso"
    assert "RESUMEN SIMULADO" in prompt, f"la tarea {n} perdió el resumen"
print("OK 2: las 7 tareas del roadmap reciben el resumen y los datos del cliente")

# 3) ETL (la primera de la segunda crew) ve las 3 salidas de la primera crew
etl = tareas[3]
for n in (1, 2, 3):
    assert f"SALIDA {n} " in etl, f"ETL no ve la salida {n} de la primera crew"
assert "ANÁLISIS DE LA PRIMERA CREW" in etl
print("OK 3: ETL recibe fuentes, ingesta y almacenamiento")

# 4) graph.py le pasa el caso al roadmap
recibido = {}
graph.roadmap_builder = lambda propuesta, problem_case=None: recibido.update(caso=problem_case) or ""
graph.roadmap_node({"problem_solution": "x", "problem_case": caso})
assert recibido["caso"] is caso, "roadmap_node no pasa el problem_case"
print("OK 4: graph.py pasa el caso del estado al roadmap")

assert hashlib.md5(INFORME.read_bytes()).hexdigest() == huella_antes, "el informe real no debía cambiar"
print("\nTodo bien: el fallo 4 está arreglado.")