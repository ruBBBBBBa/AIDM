"""Comprueba el fallo 49: el desarrollo nunca sale con 0 meses.

El 27 oct, con Iberdrola (piloto de 6 meses y despliegue completo en 18), el equipo salió con 6–18 meses y 6 de
implantación: mezcló el piloto y el despliegue completo, y la tabla decía «Desarrollo en 0–12 meses: 0 €». Ahora
_calcular_equipo() deja al menos un mes de desarrollo, y la tarea de equipo pide los meses del piloto.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_fases_minimo.py
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
from agents.estimation_crew import EquipoEstimado, PerfilEquipo, _calcular_equipo, _cargar_tarifas

tarifas = _cargar_tarifas()
PERFILES = [PerfilEquipo(perfil="Data Engineer", nivel="Middle", horas_mes=160, horas_mes_implantacion=80)]  # 30 €/h

# 1) El caso del 27 oct: 6–18 meses y 6 de implantación → 5 de implantación y 1–13 de desarrollo
calculo = _calcular_equipo(EquipoEstimado(perfiles=PERFILES, meses_min=6, meses_max=18, meses_implantacion=6), tarifas)
assert calculo["meses_implantacion"] == 5
assert "**Desarrollo en 1–13 meses: 4.800 € – 62.400 €**" in calculo["texto"], calculo["texto"]
assert "Desarrollo en 0" not in calculo["texto"]
assert "**Implantación en 5 meses: 12.000 €**" in calculo["texto"]
print("OK 1: con 6–18 meses y 6 de implantación, quedan 1–13 meses de desarrollo y 5 de implantación")

# 2) Un plazo de un mes no da para dos fases: todo cuenta como desarrollo
calculo = _calcular_equipo(EquipoEstimado(perfiles=PERFILES, meses_min=1, meses_max=1, meses_implantacion=1), tarifas)
assert calculo["meses_implantacion"] == 0 and calculo["total_min"] == 160 * 30
assert "Desarrollo en 0" not in calculo["texto"] and "Implantación en" not in calculo["texto"]
print("OK 2: con un plazo de 1 mes, todo es desarrollo (4.800 €)")

# 3) La tarea de equipo pide los meses del piloto y al menos un mes de desarrollo
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        task.output = SimpleNamespace(pydantic=None, raw="### Data Engineer")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
ec.run_estimation_crew("PROPUESTA", "groq/openai/gpt-oss-20b", problem_case={})
equipo = prompts[-1]
assert "si el cliente pide un piloto, los meses del piloto, no los del despliegue completo" in equipo
assert "al menos un mes de desarrollo" in equipo
print("OK 3: la tarea de equipo pide los meses del piloto y deja al menos un mes de desarrollo")

print("\nTodo bien: el fallo 49 está arreglado (en la parte que se puede comprobar sin el LLM real).")