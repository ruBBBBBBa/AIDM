"""Comprueba el arreglo del fallo 44 (C2): los importes solo salen en la sección de costes.

En la prueba del 22 de octubre, arquitectura daba «~500 €/mes» para Synapse y «~8.000–10.000 €» el
primer año, BI ponía «$0» y «~500 €/mes» en su tabla de licencias, y el roadmap de almacenamiento,
«6.000 €/año». Ninguna cuadraba con la sección de costes, que es la que suma en Python.

No llama a ningún LLM: se sustituye Crew.kickoff (y las crews de la propuesta) por versiones simuladas.
Tampoco toca tu informe.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_secciones_sin_importes.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

from types import SimpleNamespace

# Modelos de relleno solo para poder crear los agentes; no se llega a llamar a ninguno.
for clave in ("BIG_MODEL", "REASONING_MODEL", "REASONING_RIGOROUS_MODEL", "SMALL_REASONING_MODEL"):
    os.environ.setdefault(clave, "groq/openai/gpt-oss-20b")

from crewai import Crew

import agents.roadmap_builder as rb
import agents.solution_builder_crew as sbc
from agents.estimation_crew import EstimationResult
from agents.proposal_crew import ProposalResult
from utils import NOTA_IMPORTES, _IMPORTE, sin_importes

CASO = {
    "status": "completed",
    "nombre_del_cliente": {"description": "Cliente", "value": "Transportes Cantábrico"},
    "restriccion_presupuestaria": {"description": "Presupuesto", "value": "Confirmado por el usuario: 90.000 €"},
}

# Textos de la prueba del 22 de octubre (Transportes Cantábrico)
ARQUITECTURA = """#### 6. Coste Estimado y Optimizaciones
- **Azure Synapse**: ~500 €/mes (100 DWU para piloto).
- **ADF**: ~50 €/mes (orquestación básica).
- **Total primer año**: ~8.000–10.000 € (sin contar mano de obra de Inetum).
- **Optimizaciones**:
  - **Synapse Serverless**: Pago por consulta (ideal para piloto).

- **Coste controlado** (90.000 €/año) y **plazo de 4 meses** para el piloto.
- Volúmenes manejables (500 GB históricos + 2 GB/día de telemetría)."""

BI = """| Herramienta | Uso en el Proyecto | Licencias | Coste Adicional |
|---|---|---|---|
| **Power BI Pro** | Dashboard operativo. | 15 licencias (ya adquiridas). | $0 |
| **Azure Synapse Analytics** | Almacenamiento y procesamiento de datos. | ~500 €/mes (100 DWU). | Incluido en presupuesto. |

- **Coste estimado de retrasos**: "€X ahorrados vs. línea base" (si se tiene histórico financiero).
- "Coste estimado de retrasos: €5K" (vs. €6K la semana pasada).
- "Invertir €20K/mes en mantenimiento preventivo podría reducir retrasos en un 12% y ahorrar €50K/año"."""

ALMACENAMIENTO = """| Opción | Por qué | Riesgo |
|---|---|---|
| Azure Synapse | Combina data warehouse y data lake. Coste predecible (6.000 €/año en escenario bajo). | Superar el presupuesto de 90.000 €. |

- **Coste**: 6.600 €/año (Synapse + ADF) + formación, dentro del presupuesto de 90.000 €.
- Power BI Premium: No es necesario en esta fase (evita costes de 4.995 €/mes por capacidad)."""


# 1) Qué cuenta como importe
for importe in ["~500 €/mes", "~8.000–10.000 €", "$0", "€20K/mes", "4.995 €/mes", "9,40 €/usuario/mes",
                "1,5 millones de euros", "de 8.000 a 10.000 euros", "EUR 500"]:
    assert _IMPORTE.search(importe), f"«{importe}» debería contar como importe"
for otro in ["100K operaciones/mes", "500 GB", "€X/mes", "20 %", "4 meses", "(€/semana)"]:
    assert not _IMPORTE.search(otro), f"«{otro}» no es un importe"
print("OK 1: «~500 €/mes», «$0», «€20K/mes»… son importes; «500 GB», «€X/mes» o «20 %», no")

# 2) Arquitectura: fuera los importes; se quedan las cifras del cliente y lo que no es dinero
limpio = sin_importes(ARQUITECTURA, CASO, "architecture")
for importe in ["~500 €/mes", "~50 €/mes", "8.000–10.000 €"]:
    assert importe not in limpio, f"sigue «{importe}»"
assert "**Coste controlado** (90.000 €/año)" in limpio, "los 90.000 € son del cliente y se pueden citar"
assert "500 GB históricos" in limpio and "Pago por consulta (ideal para piloto)" in limpio
assert "#### 6. Coste Estimado y Optimizaciones" in limpio and "- **Optimizaciones**:" in limpio
assert limpio.endswith(NOTA_IMPORTES)
print("OK 2: arquitectura pierde «~500 €/mes» y «~8.000–10.000 €» y mantiene los 90.000 € del cliente")

# 3) BI: en la tabla, «—»; en el texto, fuera el paréntesis o la frase; «€X» no es un importe
limpio = sin_importes(BI, CASO, "bi_visualization")
assert "| 15 licencias (ya adquiridas). | — |" in limpio, "«$0» debe pasar a «—»"
assert "| — (100 DWU). | Incluido en presupuesto. |" in limpio
assert '"€X ahorrados vs. línea base" (si se tiene histórico financiero)' in limpio
for importe in ["€5K", "€6K", "€20K/mes", "€50K/año"]:
    assert importe not in limpio, f"sigue «{importe}»"
print("OK 3: la tabla de licencias de BI queda con «—» y los ejemplos con importes inventados se quitan")

# 4) Roadmap: se quita el paréntesis y la frase sigue; el riesgo de superar el presupuesto del cliente se queda
limpio = sin_importes(ALMACENAMIENTO, CASO, "roadmap 3")
assert "Coste predecible. |" in limpio and "Superar el presupuesto de 90.000 €." in limpio
assert "Power BI Premium: No es necesario en esta fase." in limpio
assert "6.600" not in limpio and "6.000" not in limpio and "4.995" not in limpio
print("OK 4: el roadmap de almacenamiento pierde «6.000 €/año» y «6.600 €/año»; «Coste predecible» sigue")

# 5) Sin importes, el texto no cambia (ni se añade la nota); el código no se toca
sin_nada = "## Fuentes\n\n- TMS en SQL Server.\n- Reducir retrasos un 20 %."
assert sin_importes(sin_nada, CASO) == sin_nada
codigo = '```hcl\nsku = "DW100c" # 1.200 €/mes\n```'
assert sin_importes(codigo, CASO) == codigo
assert sin_importes("", CASO) == ""
print("OK 5: un texto sin importes queda igual, y los bloques de código no se tocan")

# 6) Las cifras del cliente se pueden citar, escritas de otra forma; las demás no
caso_rango = {
    "restriccion_presupuestaria": {"description": "", "value": "150.000 a 200.000 euros"},
    "evidencias": {"description": "", "value": ["Nos cuesta 5.000 € al mes en multas."]},
}
texto = "Cabe en los 200K € del cliente. Las multas son de 5.000 €/mes. La opción B cuesta 175.000 €."
assert sin_importes(texto, caso_rango) == (
    "Cabe en los 200K € del cliente. Las multas son de 5.000 €/mes.\n\n" + NOTA_IMPORTES)
print("OK 6: «200K €» y los 5.000 € de multas son del cliente y se quedan; «175.000 €» se quita")

# 7) La propuesta: las 4 secciones se filtran antes de ir al informe; la de costes, no
inyectado = {}
sbc.run_proposal_crew = lambda *a, **k: ProposalResult(
    architecture=ARQUITECTURA, governance="Sin importes.", data_ai_strategy="Sin importes.", bi_visualization=BI)
sbc.run_estimation_crew = lambda *a, **k: EstimationResult(
    cost_estimation="<div>84.820 €</div>", team_profiles="| Data Engineer | 30 €/h |")
sbc._inject_into_html = lambda *args: inyectado.update(zip(
    ["architecture", "governance", "data_ai", "bi", "cost_estimation", "team_profiles"], args))
resultado = sbc.propose_solution("ESTRUCTURA", CASO, [])
assert "~500 €/mes" not in inyectado["architecture"] and "$0" not in inyectado["bi"]
assert inyectado["governance"] == "Sin importes."
assert inyectado["cost_estimation"] == "<div>84.820 €</div>" and "30 €/h" in inyectado["team_profiles"]
assert "~500 €/mes" not in resultado["architecture"], "el roadmap debe recibir la propuesta ya filtrada"
print("OK 7: arquitectura, gobernanza, estrategia y BI se filtran; costes y perfiles quedan igual")

# 8) El roadmap: las 7 salidas se filtran antes de inyectarlas
salidas = ["Fuentes: TMS.", "Ingesta con ADF.", ALMACENAMIENTO, "ETL.", "Modelado.", "ML.", BI]

def kickoff_simulado(self, *args, **kwargs):
    if self.task_callback is None:  # la crew del resumen
        return SimpleNamespace(raw="RESUMEN SIMULADO")
    for task in self.tasks:
        self.task_callback(SimpleNamespace(raw=salidas.pop(0)))
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
roadmap = []
rb.inject_roadmap_into_html = lambda *args: roadmap.extend(args)
combinado = rb.roadmap_builder("PROPUESTA SIMULADA", problem_case=CASO)
assert len(roadmap) == 7 and roadmap[0] == "Fuentes: TMS."
assert "6.000 €/año" not in roadmap[2] and "Superar el presupuesto de 90.000 €." in roadmap[2]
assert "$0" not in roadmap[6] and "6.600 €/año" not in combinado
print("OK 8: las 7 secciones del roadmap se filtran antes de ir al informe")

print("\nTodo bien: el fallo 44 está arreglado.")