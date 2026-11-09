"""Comprueba el refuerzo del fallo 44: la propuesta de estructura llega sin importes a los agentes de la propuesta.

En la ejecución del 27 de octubre (Iberdrola), la estructura traía «~20K€/año», «~120K€/año» y una tabla de
presupuesto. No sale en el informe, pero es el contexto de arquitectura, gobernanza, estrategia y BI.

No llama a ningún LLM: se sustituyen las crews de la propuesta y de costes, y la escritura del informe.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_estructura_sin_importes.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

os.environ.setdefault("BIG_MODEL", "groq/openai/gpt-oss-20b")

import agents.solution_builder_crew as sbc
from agents.estimation_crew import EstimationResult
from agents.proposal_crew import ProposalResult

# Filas de la estructura del 27 oct (Iberdrola, 600.000 € de presupuesto)
ESTRUCTURA = """| Capa | Componente | Coste |
|---|---|---|
| Almacenamiento | Azure Data Lake Storage Gen2 | Coste ADLS: ~20K€/año (20 TB históricos + 20 GB/día). |
| Procesamiento | Azure Databricks (Serverless + Jobs) | Coste Databricks: ~120K€/año (cluster serverless + jobs). |

- **Presupuesto ajustado:** 600K€ primer año (incluye desarrollo, integración y despliegue)."""
CASO = {"restriccion_presupuestaria": {"description": "", "value": "600.000 € el primer año"}}

recibida = {}
sbc.run_proposal_crew = lambda estructura, *a, **k: recibida.update(estructura=estructura) or ProposalResult(
    architecture="Arquitectura.", governance="Gobernanza.", data_ai_strategy="Estrategia.", bi_visualization="BI.")
sbc.run_estimation_crew = lambda *a, **k: EstimationResult(cost_estimation="<div>Costes</div>", team_profiles="Perfiles")
sbc._inject_into_html = lambda *args: None

sbc.propose_solution(ESTRUCTURA, CASO, [])
estructura = recibida["estructura"]
assert "20K€" not in estructura and "120K€" not in estructura, estructura
assert "Azure Data Lake Storage Gen2" in estructura and "Azure Databricks (Serverless + Jobs)" in estructura
assert "600K€ primer año" in estructura, "el presupuesto del cliente se puede citar"
print("OK 1: la estructura llega a la propuesta sin «~20K€/año» ni «~120K€/año», con sus componentes y el presupuesto del cliente")

print("\nTodo bien: el refuerzo del fallo 44 está hecho.")