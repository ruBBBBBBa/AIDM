"""Comprueba el arreglo del fallo 5: los costes conocen el presupuesto y el total cuadra con el equipo.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt
de cada tarea y devuelve respuestas simuladas. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_equipo.py
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
from agents.estimation_crew import (
    CostEstimationReport, EquipoEstimado, PerfilEquipo,
    _calcular_equipo, _cargar_tarifas, _encaje_presupuesto,
)

# 1) Las tarifas se leen bien del CSV
tarifas = _cargar_tarifas()
assert tarifas[("data engineer", "senior")] == 45
assert tarifas[("data analyst/bi", "middle")] == 28
assert len(tarifas) == 13 * 3, f"se esperaban 39 tarifas y hay {len(tarifas)}"
print("OK 1: tarifas leídas de bd/costes_perfil.csv (13 perfiles × 3 niveles)")

# 2) El coste del equipo se calcula en Python: horas × tarifa × meses
equipo = EquipoEstimado(
    perfiles=[
        PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160),   # 160 × 45 = 7.200
        PerfilEquipo(perfil="data architect", nivel="Senior", horas_mes=80),   # 80 × 75 = 6.000 (sin importar mayúsculas)
        PerfilEquipo(perfil="Experto en SAP", nivel="Senior", horas_mes=40),   # no está en la tabla: no se suma
    ],
    meses_min=4, meses_max=3,  # al revés a propósito: se ordenan
)
calculo = _calcular_equipo(equipo, tarifas)
assert calculo["mensual"] == 13_200
assert (calculo["total_min"], calculo["total_max"]) == (39_600, 52_800)
assert "| Data Engineer | Senior | 160 | 45 €/h | 7.200 € |" in calculo["texto"]
assert "| Experto en SAP | Senior | 40 | sin tarifa | no se suma |" in calculo["texto"]
assert "| **Total mensual** | | | | **13.200 €** |" in calculo["texto"]
assert "Coste del equipo en 3–4 meses: 39.600 € – 52.800 €" in calculo["texto"]
print("OK 2: equipo = 13.200 €/mes → 39.600 – 52.800 € en 3–4 meses; el perfil sin tarifa no se suma")

# 3) La regla del encaje con el presupuesto
assert _encaje_presupuesto(100_000, 150_000, 200_000)[0] == "alto"
assert _encaje_presupuesto(150_000, 250_000, 200_000)[0] == "medio"
assert _encaje_presupuesto(250_000, 300_000, 200_000)[0] == "bajo"
assert _encaje_presupuesto(250_000, 300_000, 0)[0] == "medio"  # sin presupuesto no se puede comprobar
print("OK 3: encaje alto / medio / bajo según el presupuesto, y 'medio' si no hay presupuesto")

# 4) run_estimation_crew completo con el LLM simulado
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None  # sin Infracost: no se llama a ningún LLM para el Terraform

# Sin equipo, en los 3–4 meses del equipo: 10.000 €/mes + 10.000 € únicos = 40.000–50.000 €,
# y la contingencia del 20 % sube el escenario alto a 60.000 € (fallo 32: las sumas las hace Python)
informe_simulado = CostEstimationReport(
    componentes=[
        {"componente": "Azure Synapse", "categoria": "infraestructura", "importe_eur": 10_000,
         "periodicidad": "mensual", "calculo": "Pool dedicado"},
        {"componente": "Formación", "categoria": "formacion", "importe_eur": 10_000,
         "periodicidad": "unico", "calculo": "2 talleres"},
    ],
    resumen_ejecutivo={
        "meses_proyecto": 4, "presupuesto_cliente_max_eur": 200_000, "contingencia_pct": 20,
        "justificacion": "Infraestructura Azure de tamaño medio.",
    },
    supuestos=["Se asume Power BI Pro."],
)
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.output_pydantic is CostEstimationReport:
            task.output = SimpleNamespace(pydantic=informe_simulado, raw="")
        elif task.output_pydantic is EquipoEstimado:
            task.output = SimpleNamespace(pydantic=equipo, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer y Data Architect.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado

caso = {
    "restriccion_presupuestaria": {"description": "Presupuesto", "value": "150.000 a 200.000 euros"},
    "plazo_objetivo": {"description": "Plazo", "value": "MVP en 3-4 meses"},
}
resultado = ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case=caso)

assert len(prompts) == 3, f"se esperaban 3 tareas y hay {len(prompts)}"
for n, prompt in enumerate(prompts, start=1):
    assert "DATOS DEL CLIENTE" in prompt and "150.000 a 200.000 euros" in prompt, f"la tarea {n} no lleva el caso"
print("OK 4: las 3 tareas de costes reciben el presupuesto y el plazo del cliente")

# Total = 40.000–60.000 (sin equipo) + 39.600–52.800 (equipo) = 79.600–112.800 → cabe en 200.000
html = resultado.cost_estimation
assert "79.600 € – 112.800 €" in html, "el total no suma infraestructura + equipo"
assert "Encaje con el presupuesto" in html and "Alto" in html
assert "Equipo: 39.600 € – 52.800 €" in html, "la barra de desarrollo debe mostrar el coste del equipo"
assert "| **Total mensual** | | | | **13.200 €** |" in resultado.team_profiles
print("OK 5: el informe suma infraestructura + equipo (79.600 – 112.800 €) y el encaje sale 'Alto'")

print("\nTodo bien: el fallo 5 está arreglado.")