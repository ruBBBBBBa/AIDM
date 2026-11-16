"""Comprueba el arreglo del fallo 4 (el roadmap recibe el caso) y el 73: las 7 fases van a la vez, ninguna lee a
otra y todas llevan la arquitectura de la propuesta como referencia común.

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
rb.HILOS = 1  # fallo 73: las fases en orden, para saber qué prompt es de cuál

# Crew.kickoff simulado: guarda el prompt de cada tarea y devuelve "SALIDA n"
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    if self.task_callback is None:  # la crew del resumen
        prompts.append(self.tasks[0].description)
        return SimpleNamespace(raw="## 🧭 RESUMEN SIMULADO 🚀")  # fallo 65: con emojis, como lo escribió Gemini
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

# 1) contexto_cliente (desde el fallo 25 sustituye a caso_compacto): datos reales, sin descripciones ni rellenos
compacto = rb.contexto_cliente(caso)
assert "- volumen_de_datos: 2 TB de histórico" in compacto
assert "no mencionado" not in compacto and "Volumen" not in compacto
assert "- regulacion_y_compliance" in compacto.split("Sin dato")[1], "el relleno debe salir como «Sin dato»"
print("OK 1: el contexto del cliente lleva solo los datos reales; el relleno sale como «Sin dato»")

# 2) Las 7 tareas del roadmap reciben el caso
rb.roadmap_builder("PROPUESTA SIMULADA", problem_case=caso)
assert len(prompts) == 8, f"se esperaban 8 prompts (resumen + 7 tareas) y hay {len(prompts)}"
tareas = prompts[1:]
for n, prompt in enumerate(tareas, start=1):
    assert "DATOS DEL CLIENTE" in prompt and "2 TB de histórico" in prompt, f"la tarea {n} no lleva el caso"
    assert "🧭" not in prompt and "🚀" not in prompt, f"la tarea {n} recibió el resumen con emojis (fallo 65)"
print("OK 2: las 7 tareas del roadmap reciben el resumen, sin emojis, y los datos del cliente")

# 3) Fallo 73: ninguna fase lee lo que escribe otra; todas llevan la arquitectura de la propuesta como referencia
prompts.clear()
rb.roadmap_builder({"architecture": "ARQUITECTURA SIMULADA: lago en tres zonas"}, problem_case=caso)
tareas = prompts[1:]
assert len(tareas) == 7
for n, prompt in enumerate(tareas, start=1):
    assert "ARQUITECTURA SIMULADA: lago en tres zonas" in prompt, f"la fase {n} no lleva la arquitectura"
    assert "CÓMO ENCAJA TU FASE" in prompt, f"la fase {n} no lleva la regla de seguir la propuesta"
    assert "SALIDA" not in prompt and "PRIMERA CREW" not in prompt, f"la fase {n} lee lo que escribió otra"
print("OK 3: las 7 fases son independientes y siguen la misma arquitectura")

# 4) graph.py le pasa el caso al roadmap
recibido = {}
graph.roadmap_builder = lambda propuesta, problem_case=None: recibido.update(caso=problem_case) or ""
graph.roadmap_node({"problem_solution": "x", "problem_case": caso})
assert recibido["caso"] is caso, "roadmap_node no pasa el problem_case"
print("OK 4: graph.py pasa el caso del estado al roadmap")

# 5) Fallo 94: la barra de progreso avanza con el resumen y con cada fase al terminar. Las fases van en hilos y solo
#    anotan su avance: la barra la pinta siempre el hilo que la empezó (el de la sesión de Streamlit)
import random
import threading
import time

import progreso

rb.HILOS = 7
pintadas = []


def kickoff_lento(self, *args, **kwargs):
    if self.task_callback is not None:
        time.sleep(random.uniform(0.6, 1.8))  # las fases acaban a destiempo, como de verdad
    return kickoff_simulado(self, *args, **kwargs)


Crew.kickoff = kickoff_lento
progreso.empezar(lambda fraccion, texto: pintadas.append((threading.get_ident(), fraccion, texto)), total=8)
try:
    rb.roadmap_builder({"architecture": "ARQUITECTURA SIMULADA"}, problem_case=caso)
finally:
    progreso.terminar()
    Crew.kickoff = kickoff_simulado
hilos = {hilo for hilo, _, _ in pintadas}
assert hilos == {threading.get_ident()}, "la barra solo se pinta desde el hilo de la sesión"
fracciones = [f for _, f, _ in pintadas]
assert fracciones == sorted(fracciones) and fracciones[-1] == 1.0, fracciones
assert len(pintadas) >= 3, pintadas  # se ha pintado mientras las fases iban acabando, no solo al final
assert pintadas[0][2] == "Resumen de la propuesta · 1 de 8 pasos" and pintadas[-1][2].endswith("· 8 de 8 pasos")
assert any(texto.startswith("Roadmap: ") for _, _, texto in pintadas)
progreso.avanzar("Sin barra")  # sin empezar (tests, scripts), no hace nada
print(f"OK 5: la barra avanza con el resumen y las 7 fases ({len(pintadas)} veces), pintada siempre por la sesión")

# 6) Fallo 88: con el inventario de la arquitectura, el resumen no se llama; las 7 fases reciben el inventario (con la
#    orquestación) y la sección de la propuesta que desarrolla cada una, que es lo que antes les llegaba en el resumen
rb.HILOS = 1
prompts.clear()
propuesta = {"architecture": "ARQUITECTURA SIMULADA", "governance": "GOBERNANZA SIMULADA: retención de 7 años",
             "data_ai_strategy": "ESTRATEGIA SIMULADA: previsión de demanda", "bi_visualization": "BI SIMULADA: 4 cuadros",
             "inventario": [{"servicio": "AWS Step Functions", "capa": "orquestación", "fase": "mvp", "alternativa": ""},
                            {"servicio": "Amazon SageMaker AI", "capa": "IA", "fase": "proyecto_completo",
                             "alternativa": ""}]}
rb.roadmap_builder(propuesta, problem_case=caso)
assert len(prompts) == 7, "sin la llamada al resumen"  # antes, 8: el resumen y las 7 fases
for n, prompt in enumerate(prompts):
    assert "- AWS Step Functions · orquestación · desde el MVP" in prompt and "La orquestación es la del inventario" in prompt
    assert "RESUMEN SIMULADO" not in prompt
fuentes, ingesta, almacenamiento, etl, modelado, ml, bi = prompts
assert "ESTRATEGIA SIMULADA" in fuentes and "ESTRATEGIA SIMULADA" in ml and "BI SIMULADA" in bi and "BI SIMULADA" in modelado
assert "GOBERNANZA SIMULADA" in almacenamiento and "GOBERNANZA SIMULADA" in etl
assert not any(s in ingesta for s in ("ESTRATEGIA SIMULADA", "GOBERNANZA SIMULADA", "BI SIMULADA"))
print("OK 6: con el inventario no se llama al resumen; cada fase recibe el inventario y su sección de la propuesta")

assert hashlib.md5(INFORME.read_bytes()).hexdigest() == huella_antes, "el informe real no debía cambiar"
print("\nTodo bien: los fallos 4, 73, 88 y 94 están arreglados.")