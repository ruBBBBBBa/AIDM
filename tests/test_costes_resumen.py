"""Comprueba el arreglo del fallo 23: el resumen de costes no se contradice.

Usa las cifras de la prueba del 15 de octubre con Transportes Cantábrico: infraestructura
75.000–90.000 €, equipo 214.400 € (4 meses) y presupuesto de 90.000 €. El LLM escribió que
el presupuesto «cubre completamente este escenario» con el encaje en «Bajo».

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_resumen.py
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
from agents.estimation_crew import CostEstimationReport, EquipoEstimado, ResumenEjecutivo, _format_cost_report, _rango

TEXTO_REAL = ("El presupuesto del cliente de 90.000 € cubre completamente este escenario, "
              "incluyendo una contingencia del 15% para imprevistos.")


def informe(minimo: int, maximo: int, justificacion: str = TEXTO_REAL) -> CostEstimationReport:
    return CostEstimationReport(
        resumen_ejecutivo={
            "coste_sin_equipo_min_eur": minimo, "coste_sin_equipo_max_eur": maximo,
            "presupuesto_cliente_max_eur": 90_000, "horizonte_temporal": "4 meses",
            "justificacion": justificacion,
        },
        supuestos=["Se asume Power BI Pro."],
        desglose_por_categoria={
            "licencias_y_herramientas": "10.000 €", "formacion_y_onboarding": "12.000 €",
            "mantenimiento_y_soporte": "15.000 €", "desarrollo_e_implantacion": "calculado aparte",
        },
        precio_por_componentes=[],
    )


# 1) Un rango con el mismo mínimo y máximo se muestra como un solo valor
assert _rango(214_400, 214_400) == "214.400 €"
assert _rango(75_000, 90_000) == "75.000 € – 90.000 €"
assert _rango(100.4, 99.6) == "100 €", "si redondeados son iguales, un solo valor"
print("OK 1: «214.400 € – 214.400 €» pasa a «214.400 €»; un rango de verdad se queda igual")

# 2) El caso del 15 de octubre
equipo = {"total_min": 214_400, "total_max": 214_400}
html = _format_cost_report(informe(75_000, 90_000), equipo=equipo)
assert "214.400 € – 214.400 €" not in html, "sigue saliendo el rango con mínimo = máximo"
assert "Equipo: 214.400 €" in html
assert "289.400 € – 304.400 €" in html and ">Bajo<" in html
print("OK 2: total 289.400 € – 304.400 €, encaje Bajo y la barra del equipo con un solo valor")

# 3) El texto del LLM dice a qué cifras se refiere, así que ya no contradice la etiqueta
etiqueta = "Infraestructura, licencias y formación (75.000 € – 90.000 €, sin el equipo): "
assert etiqueta + TEXTO_REAL in html, "la justificación debe ir precedida de las cifras a las que se refiere"
assert html.index("Incluso el escenario bajo") < html.index(etiqueta), "el motivo del encaje va antes que el texto del LLM"
print("OK 3: «cubre completamente este escenario» se lee como lo que es: solo infraestructura, licencias y formación")

# 4) Sin equipo, el coste estimado con mínimo = máximo también sale como un valor
html = _format_cost_report(informe(50_000, 50_000, "Precios de referencia de Azure."))
assert "50.000 € – 50.000 €" not in html and ">50.000 €</div>" in html
print("OK 4: sin equipo, «50.000 € – 50.000 €» sale como «50.000 €»")

# 5) El prompt ya no le pide al LLM que valore el presupuesto
assert "Sin valorar si caben en el presupuesto" in ResumenEjecutivo.model_fields["justificacion"].description

os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.output_pydantic is CostEstimationReport:
            task.output = SimpleNamespace(pydantic=informe(75_000, 90_000), raw="")
        elif task.output_pydantic is EquipoEstimado:
            task.output = SimpleNamespace(pydantic=None, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b",
                       problem_case={"restriccion_presupuestaria": {"description": "", "value": "90.000 € máximo"}})
assert "no digas si caben en el presupuesto del cliente" in prompts[0], "la tarea de costes debe pedirlo"
print("OK 5: la tarea de costes pide explicar las cifras sin valorar el presupuesto")

print("\nTodo bien: el fallo 23 está arreglado.")