"""Comprueba el arreglo del C9 (parte del fallo 39): la sección de costes se escribe con tildes.

Las opciones de categoría y periodicidad van sin tilde en el esquema («formacion», «unico») y el modelo
copiaba esa forma en el resto de textos. Ahora el prompt pide escribir en el idioma del cliente con su
ortografía completa (sin imponer el español, porque hay clientes que hablan inglés) y dice que las opciones
son claves fijas. Los euros sí son fijos.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_tildes.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

from types import SimpleNamespace

from crewai import Crew

import agents.estimation_crew as ec
from agents.estimation_crew import ComponenteCoste, CostEstimationReport, EquipoEstimado

# 1) Si el modelo escribe la opción con tilde, se acepta igual
c = ComponenteCoste(componente="Formación en Power BI", categoria="Formación", precio_unitario_eur=2_500,
                    unidades=1, unidad="taller", periodicidad="Único", referencia="Tarifa de mercado.")
assert (c.categoria, c.periodicidad) == ("formacion", "unico")
assert c.componente == "Formación en Power BI", "los textos se guardan tal cual, con sus tildes"
print("OK 1: «Formación» y «Único» valen como opción; el nombre conserva la tilde")

# 1b) Con un cliente que habla inglés, las opciones traducidas también valen
en = ComponenteCoste(componente="Power BI training", categoria="Training", precio_unitario_eur=2_500,
                     unidades=1, unidad="workshop", periodicidad="One-off", referencia="Market rate.")
assert (en.categoria, en.periodicidad) == ("formacion", "unico")
for texto, esperada in [("Infrastructure", "infraestructura"), ("Licenses", "licencias"),
                        ("Support and maintenance", "mantenimiento"), ("Soporte", "mantenimiento")]:
    assert ComponenteCoste(**{**en.model_dump(), "categoria": texto}).categoria == esperada, texto
for texto, esperada in [("Monthly", "mensual"), ("Yearly", "anual"), ("Annual", "anual")]:
    assert ComponenteCoste(**{**en.model_dump(), "periodicidad": texto}).periodicidad == esperada, texto
print("OK 1b: «Training», «One-off», «Monthly»… también valen, por si el cliente habla inglés")

# 2) Los prompts piden español con tildes
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.name == "costes":
            task.output = SimpleNamespace(pydantic=CostEstimationReport(
                componentes=[c], resumen_ejecutivo={"meses_proyecto": 4, "presupuesto_cliente_max_eur": 0,
                                   "justificacion": "Tarifas públicas."},
                supuestos=[]), raw="")
        elif task.name == "equipo":
            task.output = SimpleNamespace(pydantic=None, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case={})
costes, perfiles, _ = prompts
assert "Escribe los textos en el idioma de los datos del cliente, con su ortografía completa" in costes
assert "claves fijas: van tal cual, sin tilde (formacion, unico), sea cual sea el idioma" in costes
assert "Escribe en el idioma de los datos del cliente, con su ortografía completa" in perfiles
assert "en euros" in costes, "la moneda sí es fija"
for prompt in (costes, perfiles):
    assert "Escribe en español" not in prompt and "Escribe los textos en español" not in prompt
print("OK 2: costes y perfiles escriben en el idioma del cliente, con su ortografía; los euros siguen fijos")

print("\nTodo bien: el C9 está arreglado (en la parte que se puede comprobar sin el LLM real).")