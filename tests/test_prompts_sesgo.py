"""Comprueba el arreglo de los fallos 34 y 10: los prompts no empujan hacia soluciones grandes
ni piden campos que no existen.

Antes, los prompts pedían «al menos 3 funcionalidades» de IA y «al menos 2 dashboards», el estratega
tenía experiencia en «empresas Fortune 500», el equipo partía de una lista fija de roles, la arquitectura
se pedía «extensa» y los prompts de estrategia y BI pedían 'success_metrics', 'data_consumers' o
'technology_constraints', campos que el caso no tiene.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt de cada tarea.
No toca tu informe ni tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_prompts_sesgo.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import json
import re
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
from utils import contexto_cliente, load_agents_config

PROHIBIDAS = ["al menos 3", "al menos 2", "Fortune 500", "extensa", "success_metrics", "data_consumers",
              "technology_constraints", "Client Context", "'problem case'", "Considera roles como"]
CAMPOS = set(json.loads(Path("data_structures/problem_case_recovery.json").read_text(encoding="utf-8")))


def caso(nube: str, bi: str, empleados: str) -> dict:
    return {"cloud_provider": {"description": "", "value": nube},
            "herramienta_de_visualizacion": {"description": "", "value": bi},
            "tamaño_empresa": {"description": "", "value": empleados},
            "status": "completed"}


def generar(problem_case: dict) -> dict[str, list[str]]:
    """Los prompts de cada fase, con el LLM simulado."""
    prompts: dict[str, list[str]] = {}
    fase = ""

    def kickoff_simulado(self, *args, **kwargs):
        if getattr(self, "task_callback", None) is None and "Propuesta a resumir" in self.tasks[0].description:
            return SimpleNamespace(raw="RESUMEN")
        for task in self.tasks:
            prompts.setdefault(fase, []).append(task.description + "\n" + task.expected_output)
            task.output = SimpleNamespace(raw="texto", pydantic=None)
            if self.task_callback:
                self.task_callback(SimpleNamespace(raw="texto"))
        return SimpleNamespace(raw="texto")

    Crew.kickoff = kickoff_simulado
    ec._resolve_infracost_binary = lambda: None
    copia = Path(tempfile.mkdtemp()) / "informe.html"
    shutil.copy("data_structures/solution_builder_report.html", copia)
    rb.REPORT_PATH = copia
    fase = "estructura"; sp.build_structure_proposal(problem_case, chat_history=[])
    fase = "propuesta"; pc.run_proposal_crew("ESTRUCTURA", problem_case, chat_history=[], model_name="groq/openai/gpt-oss-20b")
    fase = "costes"; ec.run_estimation_crew("PROPUESTA", "groq/openai/gpt-oss-20b", problem_case=problem_case)
    fase = "roadmap"; rb.roadmap_builder("PROPUESTA", problem_case=problem_case)
    return prompts


A = generar(caso("Azure", "Power BI", "600 empleados"))

# 1) Ningún prompt ni agente lleva las frases que empujaban
usados = ["structure_proposal_agent", "data_governance_agent", "data_ai_strategist", "bi_visualization_specialist",
          "cost_estimation_agent", "team_profiles_advisor", "data_sources_agent", "storage_agent", "ingestion_agent",
          "etl_processing_agent", "modeling_serving_agent", "ml_mlops_agent", "bi_visualization_agent", "resume_agent"]
config = load_agents_config()
textos = [p for lista in A.values() for p in lista] + [" ".join(config[a].values()) for a in usados]
for frase in PROHIBIDAS:
    assert not any(frase in t for t in textos), f"«{frase}» sigue en algún prompt o agente"
print("OK 1: ningún prompt ni agente pide mínimos fijos, «Fortune 500», respuestas «extensas» o roles fijos")

# 2) Los campos que citan los prompts de estrategia y BI existen en el caso (fallo 10)
citados = set(re.findall(r"\b[a-zñ]+(?:_[a-zñ]+)+\b", " ".join(A["propuesta"])))
assert citados and citados <= CAMPOS, f"campos que no existen: {sorted(citados - CAMPOS)}"
print(f"OK 2: los {len(citados)} campos que citan los prompts existen en el caso: {', '.join(sorted(citados))}")

# 3) Fuera del bloque de datos del cliente, estructura, propuesta y costes no fijan ninguna tecnología
B = generar(caso("AWS", "Looker", "50 empleados"))
for fase in ("estructura", "propuesta", "costes"):
    for prompt_a, prompt_b in zip(A[fase], B[fase]):
        sin_datos_a = prompt_a.replace(contexto_cliente(caso("Azure", "Power BI", "600 empleados")), "")
        sin_datos_a = sin_datos_a.replace(contexto_cliente(caso("Azure", "Power BI", "600 empleados"), con_costes=True), "")
        for marca in ("Azure", "Power BI", "Databricks", "Synapse", "AWS"):
            assert marca not in sin_datos_a, f"un prompt de {fase} fija «{marca}» fuera de los datos del cliente"
        assert "AWS" in prompt_b and "Azure" not in prompt_b, f"un prompt de {fase} no sigue al caso"
print("OK 3: estructura, propuesta y costes no fijan tecnología; con un caso de AWS los prompts cambian con él")

print("\nTodo bien: los fallos 34 y 10 están arreglados (en la parte que se puede comprobar sin el LLM real).")