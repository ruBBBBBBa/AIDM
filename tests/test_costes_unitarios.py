"""Comprueba el arreglo del fallo 41 (C15): precio unitario × unidades en Python y lo anual prorrateado.

En la prueba del 22 de octubre, Synapse salía como «6.000 €/año» aunque su detalle sumaba 600 + 500 €/mes
(unos 13.200 €/año), y debajo «En 4–6 meses: 6.000 €»: lo anual se contaba entero. El soporte, que el
propio detalle daba a 400 €/mes, salía como 4.800 €.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_unitarios.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

from types import SimpleNamespace

from crewai import Crew
from pydantic import ValidationError

import agents.estimation_crew as ec
from agents.estimation_crew import ComponenteCoste, CostEstimationReport, EquipoEstimado, _format_cost_report


def componente(nombre, categoria, precio, unidades, unidad, periodicidad) -> dict:
    return {"componente": nombre, "categoria": categoria, "precio_unitario_eur": precio, "unidades": unidades,
            "unidad": unidad, "periodicidad": periodicidad, "referencia": "Precio público de Azure"}


def informe(componentes: list[dict]) -> CostEstimationReport:
    return CostEstimationReport(
        componentes=componentes,
        resumen_ejecutivo={"meses_proyecto": 4, "presupuesto_cliente_max_eur": 90_000,
                           "justificacion": "Precios públicos de Azure."},
        supuestos=[],
    )


EQUIPO_22OCT = {"total_min": 65_920, "total_max": 98_880, "meses_min": 4, "meses_max": 6}

# 1) El modelo da precio y unidades; el importe no está en el esquema porque lo calcula Python
props = CostEstimationReport.model_json_schema()["$defs"]["ComponenteCoste"]["properties"]
assert {"precio_unitario_eur", "unidades", "unidad", "periodicidad", "referencia"} <= set(props)
assert "importe_eur" not in props, "el importe no lo debe dar el modelo"
c = ComponenteCoste(**componente("Soporte", "mantenimiento", "50 €", "8", "hora", "mensual"))
assert c.importe_eur == 400
print("OK 1: el modelo da precio unitario y unidades; Python multiplica (50 € × 8 horas = 400 €/mes)")

# 2) Lo que no es un número se rechaza, en el precio y en las unidades
for campo, malo in (("precio_unitario_eur", "estimated"), ("unidades", "unas 730")):
    datos = componente("Synapse", "infraestructura", 1.2, 730, "hora", "mensual") | {campo: malo}
    try:
        ComponenteCoste(**datos)
    except ValidationError:
        continue
    raise AssertionError(f"{campo}={malo!r} no debería aceptarse")
print("OK 2: «estimated» como precio o «unas 730» como unidades se rechazan, y el modelo lo repite")

# 3) El caso del 22 oct: Synapse en dos partes mensuales, soporte por horas y ADF anual
html = _format_cost_report(informe([
    componente("Synapse: pool dedicado", "infraestructura", 600, 1, "mes", "mensual"),
    componente("Synapse: almacenamiento", "infraestructura", 0.025, 20_000, "GB", "mensual"),
    componente("Soporte de Inetum", "mantenimiento", 50, 8, "hora", "mensual"),
    componente("Azure Data Factory", "infraestructura", 600, 1, "año", "anual"),
]), equipo=EQUIPO_22OCT)
assert "600 €/mes" in html and "500 €/mes" in html and "0,025 €/GB × 20.000" in html
assert "En 4–6 meses: 2.400 € – 3.600 €" in html and "En 4–6 meses: 2.000 € – 3.000 €" in html, \
    "Synapse: 600 + 500 €/mes durante 4–6 meses (antes, 6.000 € en total)"
assert "400 €/mes" in html and "50 €/hora × 8" in html and "En 4–6 meses: 1.600 € – 2.400 €" in html, \
    "soporte: 400 €/mes (antes, 4.800 €)"
assert "600 €/año" in html and "En 4–6 meses: 200 € – 300 € (parte proporcional)" in html, \
    "lo anual se prorratea (antes, 600 € enteros)"
assert "Infraestructura cloud" in html and "4.600 € – 6.900 €" in html, "infra = 2.400 + 2.000 + 200 (… 3.600 + 3.000 + 300)"
print("OK 3: Synapse suma 1.100 €/mes, el soporte 400 €/mes y ADF 600 €/año sale prorrateado en 4–6 meses")

# 4) El prompt pide precio y unidades, sin multiplicar, y lo que se paga por uso como mensual
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.name == "costes":
            task.output = SimpleNamespace(pydantic=informe([]), raw="")
        elif task.name == "equipo":
            task.output = SimpleNamespace(pydantic=None, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b",
                       problem_case={"restriccion_presupuestaria": {"description": "", "value": "90.000 €"}})
costes = prompts[0]
assert "No multipliques: el importe lo calcula el programa" in costes
assert "Lo que se paga por uso (horas, GB, ejecuciones) va como mensual" in costes
assert "pon un elemento por parte" in costes
print("OK 4: la tarea pide precio y unidades sin multiplicar, y lo que se paga por uso como mensual")

print("\nTodo bien: el fallo 41 (C15) está arreglado (en la parte que se puede comprobar sin el LLM real).")
