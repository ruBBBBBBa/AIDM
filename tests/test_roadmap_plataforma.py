"""Comprueba el arreglo del fallo 6: el roadmap usa la plataforma del cliente, no Azure y Power BI por defecto.

Antes, 6 de las 7 tareas del roadmap y sus agentes daban por hecho Azure (Data Factory, ADLS, Synapse,
Azure ML) y Power BI, aunque el cliente fuera de AWS o usara Looker.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt de cada tarea.
No toca tu informe ni tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_roadmap_plataforma.py
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

from crewai import Crew

import agents.roadmap_builder as rb
from utils import contexto_cliente, load_agents_config

AGENTES = ["data_sources_agent", "ingestion_agent", "storage_agent", "etl_processing_agent",
           "modeling_serving_agent", "ml_mlops_agent", "bi_visualization_agent"]
SOLO_AZURE = ["Data Factory", "ADLS", "Synapse", "Azure ML", "Azure Machine Learning", "Fabric", "Analysis Services"]

CASO_AWS = {
    "cloud_provider": {"description": "", "value": "AWS"},
    "herramienta_de_visualizacion": {"description": "", "value": "Looker"},
    "status": "completed",
}

prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    if self.task_callback is None:  # la crew del resumen
        return SimpleNamespace(raw="RESUMEN")
    for task in self.tasks:
        prompts.append(task.description + "\n" + task.expected_output)
        self.task_callback(SimpleNamespace(raw="texto"))
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
copia = Path(tempfile.mkdtemp()) / "informe.html"
shutil.copy("data_structures/solution_builder_report.html", copia)
rb.REPORT_PATH = copia  # el roadmap se inyecta en la copia, no en tu informe
rb.roadmap_builder("PROPUESTA", problem_case=CASO_AWS)
assert len(prompts) == 7, f"se esperaban 7 tareas y hay {len(prompts)}"

# 1) Las 7 tareas reciben la regla de la plataforma, después de los datos del cliente
for n, prompt in enumerate(prompts, start=1):
    assert "PLATAFORMA: si los datos del cliente traen una PLATAFORMA OBJETIVO" in prompt, f"la tarea {n} no lleva la regla"
    assert "- cloud_provider: AWS" in prompt
print("OK 1: las 7 tareas del roadmap reciben la regla de usar la plataforma del cliente")

# 2) Fuera de los datos del cliente, un servicio de Azure solo sale como ejemplo junto a los de AWS y Google Cloud
instrucciones = [p.replace(contexto_cliente(CASO_AWS), "") for p in prompts]
for n, texto in enumerate(instrucciones, start=1):
    for frase in texto.split(". "):
        if any(servicio in frase for servicio in SOLO_AZURE) or "Azure" in frase:
            assert "AWS" in frase and "Google Cloud" in frase, f"la tarea {n} da por hecho Azure: «{frase.strip()[:90]}…»"
        if "Power BI" in frase:
            assert "equivalente" in frase or "otras herramientas" in frase, f"la tarea {n} da por hecho Power BI: «{frase.strip()[:90]}…»"
print("OK 2: ninguna tarea da por hecho Azure o Power BI; salen solo como ejemplo, con sus equivalentes")

# 3) Los agentes del roadmap: el rol y el objetivo no nombran ninguna marca, y la experiencia cubre varias nubes
config = load_agents_config()
for agente in AGENTES:
    rol_y_objetivo = config[agente]["role"] + config[agente]["goal"]
    for marca in ("Azure", "Power BI", "Databricks", "Synapse", "AWS"):
        assert marca not in rol_y_objetivo, f"el rol u objetivo de {agente} fija «{marca}»"
    historia = config[agente]["backstory"]
    if "Azure" in historia:
        assert "AWS" in historia, f"la experiencia de {agente} solo menciona Azure"
    if "Power BI" in historia:
        assert "Tableau" in historia or "Looker" in historia, f"la experiencia de {agente} solo menciona Power BI"
print("OK 3: el rol y el objetivo de los 7 agentes no fijan marcas, y su experiencia cubre varias nubes y herramientas")

# 4) Fallo 102: las etiquetas del flujo del roadmap y los chips de las tarjetas salen del inventario, no fijas de la
#    plantilla («Azure Data Factory», «ADLS Gen2»…); la fase sin servicios lleva la plataforma
from bs4 import BeautifulSoup

INVENTARIO_AWS = [{"servicio": s, "capa": c, "fase": "mvp", "alternativa": ""} for s, c in [
    ("Amazon Kinesis Data Streams", "ingesta"), ("AWS Database Migration Service (DMS)", "ingesta"),
    ("AWS Glue Data Catalog", "ingesta"), ("Amazon S3 (capas Bronze, Silver y Gold)", "almacenamiento"),
    ("AWS Glue", "procesamiento"), ("Amazon Athena", "analítica"), ("Amazon SageMaker AI", "analítica / IA"),
    ("Power BI (Microsoft 365)", "visualización"), ("AWS KMS", "seguridad")]]
EN_AWS = {**CASO_AWS, "plataforma_objetivo": {"value": "Amazon Web Services (AWS)"}}
etiquetas = rb.etiquetas_del_flujo(INVENTARIO_AWS, EN_AWS)
assert etiquetas == {"roadmap-sources": "Sistemas del cliente",
                     "roadmap-ingestion": "Kinesis Data Streams · Database Migration Service",
                     "roadmap-storage": "S3", "roadmap-etl": "Glue", "roadmap-modeling": "Athena",
                     "roadmap-ml": "SageMaker AI", "roadmap-bi": "Power BI"}, etiquetas
sin_bi = [f for f in INVENTARIO_AWS if f["capa"] != "visualización"]
assert rb.etiquetas_del_flujo(sin_bi, EN_AWS)["roadmap-bi"] == "Amazon Web Services (AWS)"
shutil.copy("data_structures/solution_builder_report.html", copia)  # fallo 116: cada ejecución parte de la plantilla
rb.inject_roadmap_into_html(*["Texto de la fase."] * 7, etiquetas=etiquetas)
sopa = BeautifulSoup(copia.read_text(encoding="utf-8"), "html.parser")
flujo = [s.get_text(strip=True) for s in sopa.select(".rdm-pipe-tech")]
chips = [s.get_text(strip=True) for s in sopa.select("#rdm-stages-container .rdm-tech-chip")]
assert flujo == list(etiquetas.values()) and chips == list(etiquetas.values()), (flujo, chips)
assert not any(a in " ".join(flujo + chips) for a in SOLO_AZURE)
subtitulo = sopa.select_one("#card-roadmap-ingestion .section-header-text > span").get_text()
assert subtitulo == "Estrategia y pipelines con Kinesis Data Streams · Database Migration Service", subtitulo
assert not sopa.select("#rdm-stages-container .rdm-tech-chip[hidden]")
# Fallo 116: una fase sin etiqueta oculta su chip y deja el subtítulo sin herramienta
shutil.copy("data_structures/solution_builder_report.html", copia)
rb.inject_roadmap_into_html(*["Texto de la fase."] * 7, etiquetas={**etiquetas, "roadmap-ingestion": ""})
sopa = BeautifulSoup(copia.read_text(encoding="utf-8"), "html.parser")
assert sopa.select("#card-roadmap-ingestion .rdm-tech-chip[hidden]")
assert sopa.select_one("#card-roadmap-ingestion .section-header-text > span").get_text() == "Estrategia y pipelines"
print("OK 4: las etiquetas del flujo y los chips del roadmap salen del inventario, sin los de Azure de la plantilla")

# 5) Fallo 130: «Modelado» pasa a «Modelado / Análisis» y «ML & MLOps» a «Ops» en el flujo, las tarjetas y los logs;
#    el chip de Ops recoge también la monitorización, y sus prompts son más generales
nombres = [s.get_text(strip=True) for s in sopa.select(".rdm-pipe-name")]
assert nombres[4:6] == ["Modelado / Análisis", "Ops"], nombres
assert sopa.select_one("#card-roadmap-modeling h2").get_text(" ", strip=True) == "05 Modelado / Análisis"
assert sopa.select_one("#card-roadmap-ml h2").get_text(" ", strip=True) == "06 Ops"
assert not any(v in sopa.get_text() for v in ("Modelado & Serving", "Machine Learning & MLOps", "ML & MLOps"))
assert rb.TITULOS[4:6] == ["Modelado / Análisis", "Ops"], rb.TITULOS
assert rb.etiquetas_del_flujo([{"servicio": "Amazon CloudWatch", "capa": "monitorización", "fase": "mvp",
                                "alternativa": ""}], EN_AWS)["roadmap-ml"] == "CloudWatch"
print("OK 5: las fases se llaman «Modelado / Análisis» y «Ops» en el flujo, las tarjetas y los logs")

print("\nTodo bien: el fallo 6 está arreglado (en la parte que se puede comprobar sin el LLM real).")