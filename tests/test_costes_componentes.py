"""Comprueba el arreglo del fallo 32: un importe numérico por componente y las cuentas en Python.

En la prueba del 15 de octubre, las tarjetas no decían si el importe era mensual y las partidas únicas
se dividían entre 12 (la contingencia de 15.000 € salía como 1.250 €). En la del 19 de octubre, sin
Infracost, Llama escribió «estimated» en vez de importes y solo valoró Synapse.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_componentes.py
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
from agents.estimation_crew import (
    ComponenteCoste, CostEstimationReport, EquipoEstimado, _a_numero, _format_cost_report,
)


def componente(nombre: str, categoria: str, importe, periodicidad: str) -> dict:
    return {"componente": nombre, "categoria": categoria, "importe_eur": importe,
            "periodicidad": periodicidad, "calculo": "Referencia pública"}


def informe(componentes: list[dict], contingencia: int = 0) -> CostEstimationReport:
    return CostEstimationReport(
        componentes=componentes,
        resumen_ejecutivo={"meses_proyecto": 4, "presupuesto_cliente_max_eur": 90_000,
                           "contingencia_pct": contingencia, "justificacion": "Precios públicos de Azure."},
        supuestos=["Se asume Power BI Pro."],
    )


# 1) El esquema que ve el modelo pide números y una periodicidad cerrada
esquema = CostEstimationReport.model_json_schema()["$defs"]["ComponenteCoste"]["properties"]
assert esquema["importe_eur"]["type"] == "number"
assert esquema["periodicidad"]["enum"] == ["mensual", "anual", "unico"]
assert esquema["categoria"]["enum"] == ["infraestructura", "licencias", "formacion", "mantenimiento"]
print("OK 1: cada componente lleva importe numérico, periodicidad (mensual/anual/unico) y categoría")

# 2) Los importes escritos como texto se leen bien; lo que no es un número se rechaza
assert _a_numero("1.250 €") == 1250 and _a_numero("15.000") == 15000
assert _a_numero("32,50 €/mes") == 32.5 and _a_numero("32.5") == 32.5
assert _a_numero("1,250.50 EUR") == 1250.5 and _a_numero("1.250,50") == 1250.5
for malo in ("estimated", "22,50 € + 10 € = 32,50 €", "-50"):
    try:
        ComponenteCoste(**componente("Synapse", "infraestructura", malo, "mensual"))
    except ValidationError:
        continue
    raise AssertionError(f"«{malo}» no debería aceptarse como importe")
print("OK 2: «1.250 €», «32,50 €/mes» o «1,250.50 EUR» son números; «estimated» o una cuenta en texto se rechazan")

# 3) Tildes y mayúsculas en las opciones no tumban la respuesta
c = ComponenteCoste(**componente("Talleres", "Formación y onboarding", 5000, "Único"))
assert (c.categoria, c.periodicidad) == ("formacion", "unico")
print("OK 3: «Formación y onboarding» y «Único» se aceptan como formacion y unico")

# 4) El caso del 15 de octubre: lo único no se divide entre 12 y cada tarjeta dice su unidad
html = _format_cost_report(informe([
    componente("Contingencia de integración", "mantenimiento", 15_000, "unico"),
    componente("Azure Data Factory", "infraestructura", 1_250, "mensual"),
    componente("Power BI Premium", "licencias", 1_200, "anual"),
]))
assert "15.000 €, pago único" in html and "En 4 meses: 15.000 €" in html
assert "1.250 €/mes" in html and "En 4 meses: 5.000 €" in html
assert "1.200 €/año" in html and "En 4 meses: 1.200 €" in html  # una licencia anual se paga entera
assert ">21.200 €</div>" in html, "el total debe ser 15.000 + 5.000 + 1.200"
print("OK 4: «15.000 €, pago único», «1.250 €/mes» y «1.200 €/año»; total sin equipo 21.200 €")

# 5) El desglose suma las tarjetas de cada categoría, con los meses del equipo y la contingencia aparte
equipo = {"total_min": 60_000, "total_max": 80_000, "meses_min": 3, "meses_max": 4}
html = _format_cost_report(informe([
    componente("Azure Synapse", "infraestructura", 32.5, "mensual"),
    componente("Azure Blob Storage", "infraestructura", 10, "mensual"),
    componente("Formación ADF", "formacion", 5_000, "unico"),
    componente("Formación Power BI", "formacion", 5_000, "unico"),
], contingencia=10), equipo=equipo)
assert "32,50 €/mes" in html and "En 3–4 meses: 98 € – 130 €" in html
assert "Infraestructura cloud" in html and "128 € – 170 €" in html, "infraestructura = (32,50 + 10) × 3–4 meses"
assert "Formación y onboarding" in html and ">10.000 €</div>" in html
assert "Contingencia (10 %, solo escenario alto)" in html and "1.017 €" in html  # 10 % de 10.170 €
assert "Equipo: 60.000 € – 80.000 €" in html
assert "70.128 € – 91.187 €" in html, "total = 10.128–11.187 (sin equipo) + 60.000–80.000 (equipo)"
assert ">3–4 meses</div>" in html, "el horizonte sale de los meses del equipo"
print("OK 5: el desglose suma las tarjetas (C11), usa los 3–4 meses del equipo y separa la contingencia")

# 6) Sin componentes: se dice, en vez de mostrar tarjetas vacías, y se avisa junto al total
html = _format_cost_report(informe([]), equipo=equipo)
assert "La propuesta no trae componentes valorados" in html
assert "Ojo: el total no incluye infraestructura, licencias, formación ni soporte" in html
assert html.index("Encaje con el presupuesto") < html.index("Ojo: el total no incluye infraestructura")
print("OK 6: sin componentes, el informe lo dice y avisa de que el total solo es el equipo")

# 7) Se dice si los precios son estimados por el modelo, y el texto del LLM no se ejecuta como HTML
html = _format_cost_report(informe([componente("<b>Synapse</b>", "infraestructura", 100, "mensual")]))
assert "sin contrastar con Infracost" in html and "&lt;b&gt;Synapse&lt;/b&gt;" in html
assert "sin contrastar con Infracost" not in _format_cost_report(informe([]), infracost_raw="OVERALL TOTAL 100")
print("OK 7: sin Infracost se avisa de que son estimaciones, y los nombres del LLM salen escapados")

# 8) El prompt pide un importe por componente y deja fuera el equipo y la contingencia
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.output_pydantic is CostEstimationReport:
            task.output = SimpleNamespace(pydantic=informe([componente("Synapse", "infraestructura", 100, "mensual")]), raw="")
        elif task.output_pydantic is EquipoEstimado:
            task.output = SimpleNamespace(pydantic=None, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b",
                       problem_case={"restriccion_presupuestaria": {"description": "", "value": "90.000 €"}})
costes = prompts[0]
assert "un elemento por cada servicio cloud, licencia, formación o soporte" in costes and "sin dejarte ninguno" in costes
assert "su importe en euros (solo el número) y su periodicidad: mensual, anual o unico" in costes
assert "NO lo incluyas como componente" in costes and "no lo añadas como componente" in costes
assert "sku" not in costes and "coste_sin_equipo" not in costes
print("OK 8: la tarea pide un importe por componente, sin el equipo ni la contingencia como componentes")

print("\nTodo bien: el fallo 32 está arreglado (en la parte que se puede comprobar sin el LLM real).")
