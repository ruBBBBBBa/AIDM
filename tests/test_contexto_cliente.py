"""Comprueba el arreglo del fallo 25: todos los agentes reciben los mismos datos del cliente, con su estado.

En la prueba del 15 de octubre cada agente rellenó los huecos por su cuenta: retrasos del 10 % o del
20 % según la sección, un stack de Azure que el cliente no tiene, Power BI Premium con 15 licencias Pro…
Ahora los 15 prompts de la generación llevan el mismo bloque: lo confirmado, lo que el cliente no sabe,
lo que no aplica, lo que falta, las notas del discovery y la regla «Por confirmar / Se asume».

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt de cada tarea.
No toca tu informe ni tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_contexto_cliente.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import shutil
import tempfile
from types import SimpleNamespace

# Modelos de relleno solo para poder crear los agentes; no se llega a llamar a ninguno.
for clave in ("BIG_MODEL", "REASONING_MODEL", "REASONING_RIGOROUS_MODEL", "SMALL_REASONING_MODEL"):
    os.environ.setdefault(clave, "groq/openai/gpt-oss-20b")
os.environ.pop("INFRACOST_API_KEY", None)

from crewai import Crew

import agents.estimation_crew as ec
import agents.proposal_crew as pc
import agents.roadmap_builder as rb
import agents.structure_proposal_agent_logic as sp
from utils import contexto_cliente


def caso(**valores) -> dict:
    datos = {campo: {"description": f"Descripción larga de {campo}", "value": v} for campo, v in valores.items()}
    return {**datos, "status": "completed"}


# Resumen del caso guardado el 15 de octubre (Transportes Cantábrico)
CANTABRICO = caso(
    nombre_del_cliente="Transportes Cantábrico",
    cloud_provider="Azure",
    herramienta_de_visualizacion="Power BI",
    stack_tecnologico_actual="SQL Server 2019 (local), Navision (ERP), Microsoft 365, Excel",
    restriccion_presupuestaria="90.000 € máximo el primer año",
    tipo_de_arquitectura="Confirmado por el usuario: no lo sabe",
    tiene_equipo_datos="Confirmado por el usuario: no aplica",
    regulacion_y_compliance="no mencionado",
    evidencias=["Uso actual de Power BI con 15 licencias Pro", "Navision como ERP sin CRM implementado",
                "Uso actual de Power BI con 15 licencias Pro"],
)

# 1) Cada campo va a su grupo, sin descripciones
texto = contexto_cliente(CANTABRICO)
confirmados, resto = texto.split("El cliente no lo sabe")
assert "- cloud_provider: Azure" in confirmados and "- restriccion_presupuestaria: 90.000 € máximo" in confirmados
no_sabe, resto = resto.split("El cliente dice que no aplica")
assert "- tipo_de_arquitectura" in no_sabe
no_aplica, resto = resto.split("Sin dato")
assert "- tiene_equipo_datos" in no_aplica
assert "- regulacion_y_compliance" in resto.split("Notas del discovery")[0]
assert "Descripción larga" not in texto and "no mencionado" not in texto and "completed" not in texto
print("OK 1: confirmado, no lo sabe, no aplica y sin dato, cada uno en su grupo y sin descripciones")

# 2) Las notas del discovery van sin repetir, y las reglas al final
assert texto.count("15 licencias Pro") == 1, "las notas repetidas deben salir una vez"
assert "«Por confirmar: …»" in texto and "«Se asume: …»" in texto
assert "No escribas cifras del cliente que no estén aquí" in texto
print("OK 2: las notas van sin duplicados y las reglas «Por confirmar / Se asume» cierran el bloque")

# 3) Casos raros: «Confirmado por el usuario: X» es un dato, y un caso vacío no rompe nada
otro = contexto_cliente(caso(stack_tecnologico_actual="Confirmado por el usuario: sin CRM",
                              kpis_de_exito="Confirmado por el usuario: no lo sé"))
assert "- stack_tecnologico_actual: sin CRM" in otro
assert "- kpis_de_exito" in otro.split("El cliente no lo sabe")[1]
assert "(sin datos del cliente)" in contexto_cliente({})
print("OK 3: «Confirmado por el usuario: sin CRM» cuenta como dato y un caso vacío no falla")

# 4) Los 15 prompts de la generación llevan el mismo bloque, y ninguno el JSON en bruto
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    if getattr(self, "task_callback", None) is None and len(self.tasks) == 1 and "Propuesta a resumir" in self.tasks[0].description:
        return SimpleNamespace(raw="RESUMEN SIMULADO")  # la crew del resumen del roadmap
    for task in self.tasks:
        prompts.append(task.description)
        task.output = SimpleNamespace(raw="texto simulado", pydantic=None)
        if self.task_callback:
            self.task_callback(SimpleNamespace(raw="texto simulado"))
    return SimpleNamespace(raw="texto simulado")

Crew.kickoff = kickoff_simulado
ec._resolve_infracost_binary = lambda: None
copia = Path(tempfile.mkdtemp()) / "informe.html"
shutil.copy("data_structures/solution_builder_report.html", copia)
rb.REPORT_PATH = copia  # el roadmap se inyecta en la copia, no en tu informe

sp.build_structure_proposal(CANTABRICO, chat_history=[])
pc.run_proposal_crew("ESTRUCTURA SIMULADA", CANTABRICO, chat_history=[], model_name="groq/openai/gpt-oss-20b")
ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case=CANTABRICO)
rb.roadmap_builder("PROPUESTA SIMULADA", problem_case=CANTABRICO)

assert len(prompts) == 15, f"se esperaban 15 prompts (1 + 4 + 3 + 7) y hay {len(prompts)}"
for n, prompt in enumerate(prompts, start=1):
    assert "CÓMO USAR ESTOS DATOS" in prompt and "- cloud_provider: Azure" in prompt, f"el prompt {n} no lleva el bloque"
    assert "'description'" not in prompt and "Descripción larga" not in prompt, f"el prompt {n} lleva el JSON en bruto"
print("OK 4: estructura, propuesta (4), costes (3) y roadmap (7) reciben el mismo bloque, sin el JSON en bruto")

print("\nTodo bien: el fallo 25 está arreglado (en la parte que se puede comprobar sin el LLM real).")