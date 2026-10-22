"""Comprueba el arreglo del fallo 37: perfiles de la tabla de tarifas y aviso de lo que no se suma.

En la prueba del 19 de octubre (Llama 4 Scout), el agente usó «Business Intelligence Developer»,
«Project Manager» e «IT Support Specialist», que no están en costes_perfil.csv, y el total solo
contaba 2 de los 5 perfiles sin avisar de ello.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_perfiles.py
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
    PERFILES_CON_TARIFA, CostEstimationReport, EquipoEstimado, PerfilEquipo,
    _calcular_equipo, _cargar_tarifas, _format_cost_report,
)


def informe() -> CostEstimationReport:
    """Un informe de costes con 20.000 € sin el equipo y los 90.000 € de presupuesto del 19 de octubre."""
    return CostEstimationReport(
        componentes=[{"componente": "Azure Synapse Analytics", "categoria": "infraestructura",
                      "importe_eur": 20_000, "periodicidad": "unico", "calculo": "Referencia de Azure"}],
        resumen_ejecutivo={
            "meses_proyecto": 4, "presupuesto_cliente_max_eur": 90_000,
            "justificacion": "Precios de referencia de Azure.",
        },
        supuestos=["Se asume acceso a los datos del TMS."],
    )


tarifas = _cargar_tarifas()

# 1) La lista cerrada sale del CSV y va en el esquema que ve el modelo
assert len(PERFILES_CON_TARIFA) == 13 and PERFILES_CON_TARIFA[1] == "Data Analyst/BI"
esquema = EquipoEstimado.model_json_schema()
assert esquema["$defs"]["PerfilEquipo"]["properties"]["perfil"]["enum"] == PERFILES_CON_TARIFA
assert "funciones_sin_tarifa" in esquema["properties"]
assert "funciones_sin_tarifa" not in esquema.get("required", []), "debe poder quedar vacía"
print("OK 1: el esquema del equipo solo ofrece los 13 perfiles de bd/costes_perfil.csv")

# 2) Un perfil de la tabla escrito de otra forma sí se suma
variantes = EquipoEstimado(
    perfiles=[
        PerfilEquipo(perfil="data analyst / BI", nivel="Middle", horas_mes=100),  # 100 × 28 = 2.800
        PerfilEquipo(perfil="  MLOps/AIOps  engineer ", nivel="Junior", horas_mes=10),  # 10 × 26 = 260
    ],
    meses_min=1, meses_max=1,
)
calculo = _calcular_equipo(variantes, tarifas)
assert calculo["mensual"] == 3_060 and calculo["no_sumados"] == []
print("OK 2: «data analyst / BI» y «MLOps/AIOps  engineer» encuentran su tarifa")

# 3) El caso del 19 de octubre: los 3 perfiles sin tarifa quedan apuntados
equipo_llama = EquipoEstimado(
    perfiles=[
        PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160),                    # 7.200
        PerfilEquipo(perfil="Data Scientist", nivel="Senior", horas_mes=160),                   # 8.800
        PerfilEquipo(perfil="Business Intelligence Developer", nivel="Middle", horas_mes=80),
        PerfilEquipo(perfil="Project Manager", nivel="Middle", horas_mes=40),
        PerfilEquipo(perfil="IT Support Specialist", nivel="Junior", horas_mes=20),
    ],
    meses_min=4, meses_max=4,
)
calculo = _calcular_equipo(equipo_llama, tarifas)
assert calculo["mensual"] == 16_000 and calculo["total_min"] == 64_000
assert calculo["no_sumados"] == [
    "Business Intelligence Developer (Middle, 80 h/mes)",
    "Project Manager (Middle, 40 h/mes)",
    "IT Support Specialist (Junior, 20 h/mes)",
]
assert "Sin presupuestar, porque no tienen tarifa en la tabla: Business Intelligence Developer" in calculo["texto"]
print("OK 3: equipo del 19 oct = 16.000 €/mes y los 3 perfiles sin tarifa quedan apuntados")

# 4) El aviso sale junto al total y al encaje, antes del motivo. Sin él, 84.000 € daría un encaje
#    «Alto» con 90.000 € de presupuesto aunque falten tres perfiles
html = _format_cost_report(informe(), equipo=calculo)
aviso = "Ojo: el total y el encaje no incluyen Business Intelligence Developer (Middle, 80 h/mes); Project Manager"
assert aviso in html, "falta el aviso de los perfiles que no se suman"
assert ">84.000 €</div>" in html and ">Alto<" in html
assert html.index("Encaje con el presupuesto") < html.index(aviso) < html.index("Incluso el escenario alto")
assert "El coste real será mayor" in html
print("OK 4: «Ojo: el total y el encaje no incluyen…» aparece debajo del encaje")

# 5) Una función sin perfil en la tabla también se avisa
con_funcion = EquipoEstimado(
    perfiles=[PerfilEquipo(perfil="Data Engineer", nivel="Middle", horas_mes=160)],
    meses_min=4, meses_max=4,
    funciones_sin_tarifa=["Gestión del proyecto"],
)
calculo = _calcular_equipo(con_funcion, tarifas)
assert calculo["no_sumados"] == ["Gestión del proyecto"]
assert "no incluyen Gestión del proyecto" in _format_cost_report(informe(), equipo=calculo)
print("OK 5: las funciones sin perfil en la tabla también se avisan")

# 6) Si todo tiene tarifa, no hay aviso
calculo = _calcular_equipo(EquipoEstimado(perfiles=con_funcion.perfiles, meses_min=4, meses_max=4), tarifas)
assert "Ojo: el total y el encaje" not in _format_cost_report(informe(), equipo=calculo)
assert "Sin presupuestar" not in calculo["texto"]
print("OK 6: sin perfiles fuera de la tabla, no sale ningún aviso")

# 7) Los prompts: perfiles con los nombres de la tabla y funciones aparte
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.output_pydantic is CostEstimationReport:
            task.output = SimpleNamespace(pydantic=informe(), raw="")
        elif task.output_pydantic is EquipoEstimado:
            task.output = SimpleNamespace(pydantic=equipo_llama, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer y Data Scientist.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
resultado = ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b",
                                   problem_case={"restriccion_presupuestaria": {"description": "", "value": "90.000 €"}})
_, perfiles, equipo = prompts
assert ", ".join(PERFILES_CON_TARIFA) in perfiles, "la tarea de perfiles debe recibir los nombres de la tabla"
assert "no una lista que haya que cubrir" in perfiles and "no inventes un perfil" in perfiles
assert "Considera roles como" not in perfiles, "la lista no debe empujar a equipos grandes (fallo 24)"
assert "los mismos perfiles, sin añadir otros" in equipo and "funciones_sin_tarifa" in equipo
assert "Ojo: el total y el encaje no incluyen Business Intelligence Developer" in resultado.cost_estimation
assert "Sin presupuestar, porque no tienen tarifa" in resultado.team_profiles
print("OK 7: la tarea de perfiles usa el catálogo de la tabla y el informe completo avisa de lo que no se suma")

print("\nTodo bien: el fallo 37 está arreglado (en la parte que se puede comprobar sin el LLM real).")
