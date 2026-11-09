"""Comprueba el arreglo del fallo 51: justificación en markdown, precios de céntimos y el umbral de «revisar unidad».

En la ejecución sin discovery del 28 de octubre (Iberdrola, ministral-14b-2512):
- La justificación salía con los «**» y los guiones a la vista (C12), y con la sangría del JSON.
- Azure Monitor salía a «0, €/log»: un precio de 0,00001 € se escribía mal.
- Event Hubs salía a 45.000 €/mes (0,0001 €/mensaje × 450 millones; el precio de Azure es por millón), el 7,5 % del
  presupuesto cada mes, y no se marcaba «revisar unidad» porque el umbral era el 10 %.

No llama a ningún LLM.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_justificacion.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

from agents.estimation_crew import CostEstimationReport, _cifra, _format_cost_report


def componente(nombre, precio, unidades, unidad):
    return {"componente": nombre, "categoria": "infraestructura", "precio_unitario_eur": precio, "unidades": unidades,
            "unidad": unidad, "periodicidad": "mensual", "referencia": "Precio público de Azure"}


# La justificación del 28 oct, con la sangría con la que llegó dentro del JSON
JUSTIFICACION = """
      **Estimación de costes para el piloto (6 meses) en Azure**:
      - **Infraestructura principal**: Event Hubs, IoT Hub, ADLS, Databricks Serverless y Azure ML.
      - **Procesamiento y ETL**: ADF con 3 pipelines mensuales.

      **Variaciones clave**:
      - **Volumen de datos**: si el piloto supera los 20 GB/día, el almacenamiento cambia."""

INFORME = CostEstimationReport(
    componentes=[componente("Azure Event Hubs (15M mensajes/día en piloto)", 0.0001, 450_000_000, "mensaje"),
                 componente("Azure Databricks Serverless (procesamiento streaming + batch)", 0.0001, 150_000_000, "ejecución"),
                 componente("Azure Monitor (logs y alertas)", 0.00001, 100_000_000, "log")],
    resumen_ejecutivo={"meses_proyecto": 6, "presupuesto_cliente_max_eur": 600_000, "justificacion": JUSTIFICACION},
    supuestos=["Se asume un piloto de 50.000 dispositivos."],
)
html = _format_cost_report(INFORME)

# 1) La justificación se ve con formato: negritas y lista, sin «**» ni sangría de código
assert "**" not in html, "siguen los ** a la vista"
assert "<strong>Estimación de costes para el piloto (6 meses) en Azure</strong>" in html
assert "<li><strong>Infraestructura principal</strong>" in html and "<pre>" not in html
print("OK 1: la justificación sale con negritas y lista, sin los «**» ni la sangría")

# 2) Los precios de céntimos se escriben enteros
assert [_cifra(v) for v in (0.00001, 0.000016, 0.025, 32.5, 1250)] == ["0,00001", "0,000016", "0,025", "32,50", "1.250"]
assert "0,00001 €/log" in html and "0, €" not in html
print("OK 2: 0,00001 € se escribe «0,00001», no «0,»")

# 3) Event Hubs (45.000 €/mes, un 7,5 % de 600.000 € al mes) se marca; Databricks (15.000 €/mes, 2,5 %) no
assert "Ojo: revisa la unidad de Azure Event Hubs (15M mensajes/día en piloto) (45.000 €/mes): cuesta al mes más de un 5 %" in html
assert html.count("(revisar unidad)") == 1
print("OK 3: Event Hubs a 45.000 €/mes sale «revisar unidad» con 600.000 € de presupuesto; Databricks, no")

print("\nTodo bien: el fallo 51 está arreglado.")