"""Comprueba el gateway de modelos (gateway.py y data_structures/modelos.yaml).

Decisión del 20 oct: cada agente o tarea tiene su modelo. Por defecto, la asignación es la de antes: BIG_MODEL en
casi todo y los REASONING_* en parte del roadmap. Con MODELO_<TAREA> en el .env se cambia el de una sola tarea.

No llama a ningún LLM: se sustituyen Crew.kickoff y litellm.completion por versiones simuladas.
No toca tu informe ni tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_gateway.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import contextlib
import io
import json
import re
import shutil
import tempfile
from types import SimpleNamespace

import yaml
from crewai import Crew

import gateway
import utils
import agents.discovery_agent_logic as discovery
import agents.estimation_crew as ec
import agents.proposal_crew as pc
import agents.roadmap_builder as rb
import agents.structure_proposal_agent_logic as sp

# El .env de quien ejecuta el test no cuenta: se quitan sus MODELO_* y se ponen unos modelos de prueba
for clave in [c for c in os.environ if c.startswith("MODELO_")]:
    del os.environ[clave]
os.environ.update(BIG_MODEL="prueba/grande", REASONING_MODEL="prueba/razona",
                  REASONING_RIGOROUS_MODEL="prueba/riguroso", SMALL_REASONING_MODEL="prueba/pequeno")

# La asignación de antes del gateway (roadmap_builder.py usaba FIRST, SECOND y THIRD_MODEL_NAME)
ANTES = {
    "discovery": "prueba/grande", "estructura": "prueba/grande",
    "arquitectura": "prueba/grande", "gobernanza": "prueba/grande", "estrategia": "prueba/grande",
    "visualizacion": "prueba/grande", "costes": "prueba/grande", "equipo": "prueba/grande",
    "terraform": "prueba/grande", "resumen": "prueba/grande",
    "roadmap_fuentes": "prueba/riguroso", "roadmap_ingesta": "prueba/riguroso", "roadmap_almacenamiento": "prueba/grande",
    "roadmap_etl": "prueba/riguroso", "roadmap_modelado": "prueba/razona", "roadmap_ml": "prueba/pequeno",
    "roadmap_bi": "prueba/grande",
}

# 1) Por defecto, cada tarea usa el modelo de antes, y el yaml tiene justo las tareas que piden los agentes
for tarea, esperado in ANTES.items():
    assert gateway.modelo(tarea) == esperado, (tarea, gateway.modelo(tarea))
config = yaml.safe_load(Path("data_structures/modelos.yaml").read_text(encoding="utf-8"))
codigo = "".join(p.read_text(encoding="utf-8") for p in Path("agents").glob("*.py"))
pedidas = set(re.findall(r'gateway\.modelo\("(\w+)"\)', codigo))
assert set(config["tareas"]) == set(ANTES) == pedidas, (set(config["tareas"]) ^ pedidas)
print("OK 1: sin tocar nada, cada una de las 17 tareas usa el modelo de antes")

# 2) MODELO_<TAREA> cambia solo esa tarea y el log lo dice; una variable vacía o una tarea que falta van al de por defecto
os.environ["MODELO_COSTES"] = "prueba/costes"
log = io.StringIO()
with contextlib.redirect_stdout(log):
    assert gateway.modelo("costes") == "prueba/costes"
assert "[gateway] costes: prueba/costes (MODELO_COSTES del .env)" in log.getvalue(), log.getvalue()
assert gateway.modelo("equipo") == "prueba/grande"
del os.environ["MODELO_COSTES"]
os.environ["SMALL_REASONING_MODEL"] = ""
assert gateway.modelo("roadmap_ml") == "prueba/grande"
os.environ["SMALL_REASONING_MODEL"] = "prueba/pequeno"
assert gateway.modelo("tarea_nueva") == "prueba/grande"
print("OK 2: MODELO_COSTES cambia solo los costes y sale en el log; sin modelo, se usa BIG_MODEL")

# 3) Las reglas de cada modelo son las que había en utils.MODEL_RULES
assert utils.build_llm is gateway.build_llm
llm = gateway.build_llm("groq/openai/gpt-oss-20b")
assert llm.max_tokens == 4096
assert llm.additional_params["fallbacks"] == [{"model": "cloudflare/@cf/openai/gpt-oss-20b", "max_tokens": 6000}]
assert gateway.parametros("groq/openai/gpt-oss-120b") == {
    "num_retries": 2, "max_tokens": 6000, "fallbacks": [{"model": "cloudflare/@cf/openai/gpt-oss-120b", "max_tokens": 6000}]}
assert gateway.parametros("cloudflare/@cf/meta/llama-3.1-8b-instruct") == {"num_retries": 2, "max_tokens": 4096}
assert gateway.parametros("mistral/ministral-14b-2512") == {"num_retries": 2}
assert gateway.parametros("mistral/ministral-14b-2512", num_retries=3) == {"num_retries": 3}
assert gateway.build_llm("gemini/gemini-2.5-flash") == "gemini/gemini-2.5-flash"
print("OK 3: gpt-oss 20b y 120b llevan su tope y su reserva en Cloudflare, como antes; los demás, solo reintentos")

# 4) Cada agente usa el modelo de su tarea: se le da uno distinto a cada una
for tarea in ANTES:
    os.environ[f"MODELO_{tarea.upper()}"] = f"prueba/{tarea}"
roles = {datos["role"].strip(): clave for clave, datos in utils.load_agents_config().items()}
usados: dict[str, dict[str, str]] = {}
fase = ""


def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        usados.setdefault(fase, {})[roles[task.agent.role.strip()]] = task.agent.llm.model
        task.output = SimpleNamespace(raw="texto", pydantic=None)
        if getattr(self, "task_callback", None):
            self.task_callback(SimpleNamespace(raw="texto"))
    return SimpleNamespace(raw="texto")


def completion_simulada(**kwargs):
    usados.setdefault(fase, {})["litellm"] = kwargs["model"]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({"updates": [], "assistant_reply": "¿Cuál es el sector?", "update_status": None})))])


Crew.kickoff = kickoff_simulado
discovery.litellm.completion = completion_simulada
os.environ["INFRACOST_API_KEY"] = "prueba"  # para que se pida el modelo de Terraform; no se llama a Infracost
ec._resolve_infracost_binary = lambda: "infracost"
ec._generate_terraform_from_proposal = lambda propuesta, modelo: usados.setdefault(fase, {}).update(terraform=modelo) or ""
copia = Path(tempfile.mkdtemp()) / "informe.html"
shutil.copy("data_structures/solution_builder_report.html", copia)
rb.REPORT_PATH = copia
CASO = {"cloud_provider": {"description": "", "value": "Azure"}, "status": "completed"}

with contextlib.redirect_stdout(io.StringIO()):
    fase = "discovery"; discovery._propose_plan_with_agent("Somos una distribuidora", [])
    fase = "estructura"; sp.build_structure_proposal(CASO, chat_history=[])
    fase = "propuesta"; pc.run_proposal_crew("ESTRUCTURA", CASO, chat_history=[])
    fase = "costes"; ec.run_estimation_crew("PROPUESTA", problem_case=CASO)
    fase = "roadmap"; rb.roadmap_builder("PROPUESTA", problem_case=CASO)

assert usados["discovery"] == {"litellm": "prueba/discovery"}, usados["discovery"]
assert usados["estructura"] == {"structure_proposal_agent": "prueba/estructura"}, usados["estructura"]
assert usados["propuesta"] == {"structure_proposal_agent": "prueba/arquitectura", "data_governance_agent": "prueba/gobernanza",
                               "data_ai_strategist": "prueba/estrategia",
                               "bi_visualization_specialist": "prueba/visualizacion"}, usados["propuesta"]
assert usados["costes"] == {"terraform": "prueba/terraform", "cost_estimation_agent": "prueba/costes",
                            "team_profiles_advisor": "prueba/equipo"}, usados["costes"]
assert usados["roadmap"] == {"resume_agent": "prueba/resumen", "data_sources_agent": "prueba/roadmap_fuentes",
                             "ingestion_agent": "prueba/roadmap_ingesta", "storage_agent": "prueba/roadmap_almacenamiento",
                             "etl_processing_agent": "prueba/roadmap_etl", "modeling_serving_agent": "prueba/roadmap_modelado",
                             "ml_mlops_agent": "prueba/roadmap_ml", "bi_visualization_agent": "prueba/roadmap_bi"}, usados["roadmap"]
print("OK 4: discovery, estructura, los 4 de la propuesta, Terraform, costes, equipo, el resumen y las 7 fases del "
      "roadmap usan cada uno el modelo de su tarea")

# 5) Si un test pasa el modelo a la crew, vale para todos sus agentes (como antes)
usados.clear()
with contextlib.redirect_stdout(io.StringIO()):
    fase = "propuesta"; pc.run_proposal_crew("ESTRUCTURA", CASO, chat_history=[], model_name="prueba/fijo")
    fase = "costes"; ec.run_estimation_crew("PROPUESTA", "prueba/fijo", problem_case=CASO)
assert set(usados["propuesta"].values()) == {"prueba/fijo"} and set(usados["costes"].values()) == {"prueba/fijo"}, usados
print("OK 5: con el modelo pasado a mano, toda la crew usa ese")

print("\nTodo bien: el gateway de modelos funciona (en la parte que se puede comprobar sin el LLM real).")
