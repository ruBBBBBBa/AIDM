"""Comprueba el arreglo del fallo 42 (C16 y C17): lo que no tiene tarifa cuenta en el encaje y el aviso es claro.

En la prueba del 22 de octubre, el escenario bajo (84.820 €) cabía por 5.180 € en los 90.000 € y el encaje
salía «Medio», aunque el aviso decía que faltaban la gestión del proyecto y la formación (C16). Además, el
aviso daba la formación por no incluida cuando el desglose tenía 7.500 € de formación (C17).

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_sin_tarifa.py
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
PERFILES = [  # 16.000 €/mes: 64.000 € en 4 meses y 96.000 € en 6
    PerfilEquipo(perfil="Data Engineer", nivel="Middle", horas_mes=160),     # 4.800
    PerfilEquipo(perfil="Data Analyst/BI", nivel="Middle", horas_mes=160),   # 4.480
    PerfilEquipo(perfil="Data Architect", nivel="Junior", horas_mes=120),    # 3.840
    PerfilEquipo(perfil="Data Scientist", nivel="Junior", horas_mes=120),    # 2.880
]
INFORME = CostEstimationReport(  # 18.900 € sin el equipo, como el 22 oct
    componentes=[{"componente": "Infraestructura y formación", "categoria": "infraestructura",
                  "precio_unitario_eur": 18_900, "unidades": 1, "unidad": "proyecto", "periodicidad": "unico",
                  "referencia": "Precios públicos"}],
    resumen_ejecutivo={"meses_proyecto": 4, "presupuesto_cliente_max_eur": 90_000, "justificacion": "Azure."},
    supuestos=[],
)


def informe_con(funciones) -> str:
    equipo = EquipoEstimado(perfiles=PERFILES, meses_min=4, meses_max=6, funciones_sin_tarifa=funciones)
    return _format_cost_report(INFORME, equipo=_calcular_equipo(equipo, tarifas))


# 1) Las funciones sin tarifa llevan horas; un modelo que mande solo el nombre sigue valiendo
esquema = EquipoEstimado.model_json_schema()["$defs"]["FuncionSinTarifa"]["properties"]
assert set(esquema) == {"funcion", "horas_mes"}
viejo = EquipoEstimado(perfiles=[], meses_min=4, meses_max=4, funciones_sin_tarifa=["Gestión del proyecto"])
assert viejo.funciones_sin_tarifa[0].funcion == "Gestión del proyecto" and viejo.funciones_sin_tarifa[0].horas_mes == 0
assert min(tarifas.values()) == 20, "la tarifa más baja de la tabla es la de Data Analyst/BI Junior"
print("OK 1: cada función sin tarifa lleva sus horas al mes; una lista de nombres sigue aceptándose")

# 2) Sin nada fuera de la tabla, el encaje es el de siempre
html = informe_con([])
assert ">Medio<" in html and "provisional" not in html and "Contando al menos" not in html
assert "Solo el escenario bajo (82.900 €) cabe en el presupuesto del cliente (90.000 €)" in html
print("OK 2: sin funciones sin tarifa, encaje «Medio» como antes (82.900 € en 90.000 €)")

# 3) Con la gestión del proyecto a 32 h/mes, el encaje cuenta un mínimo y se marca como provisional
html = informe_con([{"funcion": "Gestión del proyecto", "horas_mes": 32}])
assert ">Medio (provisional)<" in html
assert ("Contando al menos 2.560 € – 3.840 € por lo que no tiene tarifa (32 h/mes a 20 €/h, la tarifa más baja "
        "de la tabla), solo el escenario bajo (85.460 €) cabe") in html
assert ">82.900 € – 114.900 €</div>" in html, "el total que se muestra sigue siendo solo lo que tiene tarifa"
print("OK 3: con 32 h/mes de gestión, «Medio (provisional)» contando al menos 2.560 € (85.460 € en 90.000 €)")

# 4) Si el mínimo de lo que falta ya no cabe en el margen, el encaje baja (C16)
html = informe_con([{"funcion": "Gestión del proyecto", "horas_mes": 64},
                    {"funcion": "Soporte de primer nivel", "horas_mes": 40}])
assert ">Bajo (provisional)<" in html, "82.900 € + 104 h × 20 €/h × 4 meses = 91.220 € no cabe en 90.000 €"
assert "incluso el escenario bajo (91.220 €) supera el presupuesto del cliente (90.000 €)" in html
print("OK 4: con 104 h/mes sin tarifa, el margen de 7.100 € no alcanza y el encaje pasa a «Bajo (provisional)»")

# 5) Sin horas no hay mínimo que contar, pero el encaje sigue siendo provisional
html = informe_con(["Gestión del proyecto"])
assert ">Medio (provisional)<" in html and "Contando al menos" not in html
print("OK 5: una función sin horas no suma mínimo, pero el encaje queda como provisional")

# 6) C17: el aviso habla de horas de personas, y la formación queda fuera de las funciones sin tarifa
html = informe_con([{"funcion": "Gestión del proyecto", "horas_mes": 32}])
assert "Ojo: el total no incluye las horas de Gestión del proyecto (32 h/mes)" in html
assert "así que el encaje es provisional" in html

os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b",
                       problem_case={"restriccion_presupuestaria": {"description": "", "value": "90.000 €"}})
_, perfiles, equipo = prompts
assert "con sus horas al mes" in perfiles
assert "La formación, las licencias y los servicios no son funciones del equipo: se valoran en los costes" in perfiles
assert "no pongas ahí formación, licencias ni servicios, que se valoran aparte" in equipo
desc = EquipoEstimado.model_json_schema()["properties"]["funciones_sin_tarifa"]["description"]
assert "No van aquí la formación, las licencias ni los servicios" in desc
print("OK 6: el aviso habla de horas, y la formación, las licencias y los servicios quedan fuera de las funciones sin tarifa")

print("\nTodo bien: el fallo 42 (C16 y C17) está arreglado (en la parte que se puede comprobar sin el LLM real).")
