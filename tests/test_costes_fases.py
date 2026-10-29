"""Comprueba el fallo 43: el coste del equipo separa el desarrollo de la implantación.

En la prueba con el jefe (caso Iberdrola) se pidió separar «Desarrollo e implantación», que era una sola
barra con el coste entero del equipo. Ahora cada perfil da sus horas al mes en cada fase y el equipo dice
cuántos meses, al final del plazo, son de implantación; Python calcula cada coste.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_fases.py
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
    CostEstimationReport, EquipoEstimado, PerfilEquipo, _calcular_equipo, _cargar_tarifas, _format_cost_report,
)

tarifas = _cargar_tarifas()
INFORME = CostEstimationReport(
    componentes=[{"componente": "Azure Synapse", "categoria": "infraestructura", "precio_unitario_eur": 1_000,
                  "unidades": 1, "unidad": "mes", "periodicidad": "mensual", "referencia": "Precio público"}],
    resumen_ejecutivo={"meses_proyecto": 4, "presupuesto_cliente_max_eur": 90_000, "justificacion": "Azure."},
    supuestos=[],
)
CON_FASES = EquipoEstimado(  # el ejemplo de la pregunta: 3–4 meses, el último de implantación
    perfiles=[
        PerfilEquipo(perfil="Data Engineer", nivel="Middle", horas_mes=160, horas_mes_implantacion=80),      # 30 €/h
        PerfilEquipo(perfil="Data Analyst/BI", nivel="Middle", horas_mes=160),                               # 28 €/h
        PerfilEquipo(perfil="Platform Engineer", nivel="Middle", horas_mes=0, horas_mes_implantacion=120),   # 34 €/h
    ],
    meses_min=3, meses_max=4, meses_implantacion=1,
)

# 1) El esquema pide las horas de implantación de cada perfil y los meses de implantación
props = EquipoEstimado.model_json_schema()
perfil = props["$defs"]["PerfilEquipo"]["properties"]
assert "horas_mes_implantacion" in perfil and "0 si solo participa en la implantación" in perfil["horas_mes"]["description"]
assert "meses_implantacion" in props["properties"]
assert {"horas_mes_implantacion", "meses_implantacion"}.isdisjoint(props.get("required", []) + props["$defs"]["PerfilEquipo"].get("required", []))
print("OK 1: cada perfil tiene horas de desarrollo y de implantación, y el equipo, meses de implantación")

# 2) Con fases: cada coste por separado (desarrollo 9.280 €/mes durante 2–3 meses, implantación 6.480 € en 1 mes)
calculo = _calcular_equipo(CON_FASES, tarifas)
texto = calculo["texto"]
assert "| Perfil | Nivel | Tarifa | Desarrollo (h/mes) | Implantación (h/mes) |" in texto
assert "| Data Engineer | Middle | 30 €/h | 160 | 80 |" in texto and "| Platform Engineer | Middle | 34 €/h | 0 | 120 |" in texto
assert "| **Coste mensual** | | | **9.280 €** | **6.480 €** |" in texto
assert "**Desarrollo en 2–3 meses: 18.560 € – 27.840 €**" in texto
assert "**Implantación en 1 mes: 6.480 €**" in texto
assert "**Coste del equipo en 3–4 meses: 25.040 € – 34.320 €**" in texto
assert (calculo["total_min"], calculo["total_max"]) == (25_040, 34_320)
print("OK 2: desarrollo 18.560–27.840 € en 2–3 meses, implantación 6.480 € en 1 mes, total 25.040–34.320 €")

# 3) En el desglose salen dos barras; los componentes siguen contando los 3–4 meses
html = _format_cost_report(INFORME, equipo=calculo)
assert "Desarrollo · 2–3 meses" in html and "Equipo: 18.560 € – 27.840 €" in html
assert "Implantación · 1 mes" in html and "Equipo: 6.480 €" in html
assert "Desarrollo e implantación" not in html
assert ">28.040 € – 38.320 €</div>" in html, "total = 3.000–4.000 € de Synapse + 25.040–34.320 € del equipo"
print("OK 3: el desglose separa «Desarrollo · 2–3 meses» e «Implantación · 1 mes»")

# 4) Sin fase de implantación todo queda como antes, también si hay meses pero nadie tiene horas en ella
for equipo in (EquipoEstimado(perfiles=CON_FASES.perfiles[1:2], meses_min=3, meses_max=4),
               EquipoEstimado(perfiles=CON_FASES.perfiles[1:2], meses_min=3, meses_max=4, meses_implantacion=1)):
    calculo = _calcular_equipo(equipo, tarifas)
    assert "| Data Analyst/BI | Middle | 160 | 28 €/h | 4.480 € |" in calculo["texto"]
    assert "Coste del equipo en 3–4 meses: 13.440 € – 17.920 €" in calculo["texto"]
    assert calculo["meses_implantacion"] == 0
    assert "Desarrollo e implantación" in _format_cost_report(INFORME, equipo=calculo)
print("OK 4: sin horas de implantación, la tabla y la barra «Desarrollo e implantación» son las de antes")

# 5) La implantación no puede durar más que el plazo corto
largo = EquipoEstimado(perfiles=CON_FASES.perfiles, meses_min=2, meses_max=3, meses_implantacion=5)
# Fallo 49: y deja al menos un mes de desarrollo (antes se quedaban en 2, con 0 meses de desarrollo)
assert calculo["meses_implantacion"] == 1 and "**Implantación en 1 mes: 6.480 €**" in calculo["texto"]
assert "**Desarrollo en 1–2 meses: 9.280 € – 18.560 €**" in calculo["texto"]
print("OK 5: 5 meses de implantación en un plazo de 2–3 se quedan en 1, con 1–2 meses de desarrollo")

# 6) Un perfil sin tarifa con horas en las dos fases: se avisa de ambas y cuenta en el mínimo del encaje
sin_tarifa = EquipoEstimado(
    perfiles=[*CON_FASES.perfiles,
              PerfilEquipo(perfil="Project Manager", nivel="Middle", horas_mes=40, horas_mes_implantacion=20)],
    meses_min=3, meses_max=4, meses_implantacion=1,
)
calculo = _calcular_equipo(sin_tarifa, tarifas)
assert "Project Manager (Middle, 40 h/mes y 20 h/mes en implantación)" in calculo["no_sumados"]
assert "| Project Manager | Middle | sin tarifa | 40 | 20 |" in calculo["texto"]
assert (calculo["falta_min"], calculo["falta_max"]) == (20 * (40 * 2 + 20 * 1), 20 * (40 * 3 + 20 * 1))
print("OK 6: el Project Manager sin tarifa se avisa con sus horas de cada fase y suma 2.000–2.800 € de mínimo")

# 7) Los prompts piden las dos fases, y el informe completo las usa
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.name == "costes":
            task.output = SimpleNamespace(pydantic=INFORME, raw="")
        elif task.name == "equipo":
            task.output = SimpleNamespace(pydantic=CON_FASES, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="### Data Engineer\n### Data Analyst/BI\n### Platform Engineer")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
resultado = ec.run_estimation_crew("PROPUESTA", "groq/openai/gpt-oss-20b",
                                   problem_case={"restriccion_presupuestaria": {"description": "", "value": "90.000 €"}})
_, perfiles, equipo = prompts
assert "Distingue su dedicación en el desarrollo (construir la solución) y en la implantación" in perfiles
assert "horas_mes_implantacion" in equipo and "meses_implantacion" in equipo
assert "**Implantación en 1 mes: 6.480 €**" in resultado.team_profiles
assert "Implantación · 1 mes" in resultado.cost_estimation
print("OK 7: las tareas piden las dos fases y el informe completo las separa")

print("\nTodo bien: el fallo 43 está arreglado (en la parte que se puede comprobar sin el LLM real).")