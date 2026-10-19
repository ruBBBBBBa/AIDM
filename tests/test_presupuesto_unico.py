"""Comprueba el arreglo del fallo 24: un solo presupuesto y un equipo a la medida del piloto.

En la prueba del 15 de octubre, arquitectura repartió los 90.000 € sin mano de obra, perfiles sumó
60.000 € y costes dio 289.400–304.400 €, con 9 perfiles a jornada completa durante 4 meses (214.400 €).
Ahora solo la crew de costes da cifras, y el equipo se dimensiona al alcance pedido, con dedicación
parcial donde toque.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt de cada tarea.
No toca tu informe ni tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_presupuesto_unico.py
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

for clave in ("BIG_MODEL", "REASONING_MODEL", "REASONING_RIGOROUS_MODEL", "SMALL_REASONING_MODEL"):
    os.environ.setdefault(clave, "groq/openai/gpt-oss-20b")
os.environ.pop("INFRACOST_API_KEY", None)

from crewai import Crew

import agents.estimation_crew as ec
import agents.proposal_crew as pc
import agents.roadmap_builder as rb
import agents.structure_proposal_agent_logic as sp
from agents.estimation_crew import CostEstimationReport, EquipoEstimado
from utils import contexto_cliente

CASO = {
    "restriccion_presupuestaria": {"description": "", "value": "90.000 € máximo el primer año"},
    "plazo_objetivo": {"description": "", "value": "4 meses para piloto con informe semanal automatizado"},
    "tipo_de_servicio": {"description": "", "value": "Que Inetum lo implemente y después forme al equipo interno"},
    "status": "completed",
}
REGLA = "Las cifras de presupuesto y coste salen solo de la sección de costes"

# 1) La regla del presupuesto va en todos los bloques menos en el de la crew de costes
assert REGLA in contexto_cliente(CASO)
assert REGLA not in contexto_cliente(CASO, con_costes=True)
print("OK 1: la regla de no hacer presupuestos va en el contexto general y no en el de costes")

# 2) La generación completa con el LLM simulado
prompts: dict[str, list[str]] = {"estructura": [], "propuesta": [], "costes": [], "roadmap": []}
fase = ""

def kickoff_simulado(self, *args, **kwargs):
    if getattr(self, "task_callback", None) is None and "Propuesta a resumir" in self.tasks[0].description:
        return SimpleNamespace(raw="RESUMEN SIMULADO")
    for task in self.tasks:
        prompts[fase].append(task.description)
        task.output = SimpleNamespace(raw="texto simulado", pydantic=None)
        if self.task_callback:
            self.task_callback(SimpleNamespace(raw="texto simulado"))
    return SimpleNamespace(raw="texto simulado")

Crew.kickoff = kickoff_simulado
ec._resolve_infracost_binary = lambda: None
copia = Path(tempfile.mkdtemp()) / "informe.html"
shutil.copy("data_structures/solution_builder_report.html", copia)
rb.REPORT_PATH = copia

fase = "estructura"; sp.build_structure_proposal(CASO, chat_history=[])
fase = "propuesta"; pc.run_proposal_crew("ESTRUCTURA", CASO, chat_history=[], model_name="groq/openai/gpt-oss-20b")
fase = "costes"; ec.run_estimation_crew("PROPUESTA", "groq/openai/gpt-oss-20b", problem_case=CASO)
fase = "roadmap"; rb.roadmap_builder("PROPUESTA", problem_case=CASO)

for nombre in ("estructura", "propuesta", "roadmap"):
    for n, prompt in enumerate(prompts[nombre], start=1):
        assert REGLA in prompt, f"el prompt {n} de {nombre} no lleva la regla del presupuesto"
assert all(REGLA not in p for p in prompts["costes"]), "la crew de costes sí debe poder dar cifras"
print("OK 2: los 12 prompts de estructura, propuesta y roadmap prohíben presupuestos propios; los 3 de costes no")

# 3) Perfiles: el alcance pedido, el tipo de servicio y sin cifras ni lista fija de roles
costes, perfiles, equipo = prompts["costes"]
assert "no la solución completa a largo plazo" in perfiles
assert "si su dedicación es completa o parcial" in perfiles
assert "si quiere que Inetum lo implemente" in perfiles
assert "No des cifras de coste" in perfiles
assert "Considera roles como" not in perfiles, "la lista fija de roles empuja a equipos grandes"
print("OK 3: la tarea de perfiles se ciñe al alcance pedido, al tipo de servicio y no da cifras")

# 4) Equipo presupuestado: los mismos perfiles y jornada completa solo para quien la tenga
assert "los mismos perfiles, sin añadir otros" in equipo
assert "160 horas al mes es jornada completa: úsala solo para" in equipo
assert "menos si es parcial" in EquipoEstimado.model_json_schema()["$defs"]["PerfilEquipo"]["properties"]["horas_mes"]["description"]
print("OK 4: el equipo que se presupuesta usa los mismos perfiles y dedicación parcial donde toque")

# 5) Costes: un único porcentaje de contingencia
assert "usa un único porcentaje, el mismo en todo el informe" in costes
print("OK 5: la tarea de costes pide un único porcentaje de contingencia")

print("\nTodo bien: el fallo 24 está arreglado (en la parte que se puede comprobar sin el LLM real).")