"""Comprueba el arreglo del fallo 45 (C6): los componentes a 0 € y los precios de Infracost en euros.

En la prueba del 22 de octubre, Azure Machine Learning y Azure Logic Apps salieron a 0 € («gratis para el
piloto»), aunque el roadmap de ML proponía un clúster de 4 nodos y un endpoint con autoescalado. Power BI Pro
también iba a 0 €, pero con razón: el cliente ya tiene las licencias.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión simulada. Tampoco ejecuta Infracost: se
simula subprocess.run para ver con qué variables se llamaría.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_ceros.py
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
from agents.estimation_crew import CostEstimationReport, ComponenteCoste, EquipoEstimado, _format_cost_report, _ya_lo_tiene


def componente(nombre, precio, unidades, unidad, periodicidad, referencia, categoria="infraestructura"):
    return {"componente": nombre, "categoria": categoria, "precio_unitario_eur": precio, "unidades": unidades,
            "unidad": unidad, "periodicidad": periodicidad, "referencia": referencia}


# Los componentes del 22 de octubre, con las referencias que escribió el modelo
SYNAPSE = componente("Azure Synapse Analytics", 600, 1, "pool de 100 DWU", "mensual", "Tarifas públicas de Azure.")
AZURE_ML = componente("Azure Machine Learning (servicios básicos para piloto)", 0, 1, "workspace", "anual",
                      "Gratis para el piloto, solo se paga por computación adicional si se escala.")
LOGIC_APPS = componente("Azure Logic Apps (para envío de alertas)", 0, 1, "flujo", "anual",
                        "Gratis para el piloto (hasta 100K operaciones/mes).")
POWER_BI = componente("Power BI Pro (licencias existentes)", 0, 15, "licencia", "anual",
                      "Licencias ya adquiridas por el cliente. No hay coste adicional.", "licencias")


def informe(*componentes) -> CostEstimationReport:
    return CostEstimationReport(
        componentes=list(componentes),
        resumen_ejecutivo={"meses_proyecto": 4, "presupuesto_cliente_max_eur": 90_000,
                           "justificacion": "Tarifas públicas de Azure."},
        supuestos=["Se asume acceso a los datos del TMS."],
    )


# 1) Cuándo un 0 € está justificado: solo si el cliente ya lo tiene
assert _ya_lo_tiene(ComponenteCoste(**POWER_BI))
assert _ya_lo_tiene(ComponenteCoste(**{**AZURE_ML, "referencia": "Ya lo tiene el cliente."}))
assert not _ya_lo_tiene(ComponenteCoste(**AZURE_ML)) and not _ya_lo_tiene(ComponenteCoste(**LOGIC_APPS))
print("OK 1: «licencias ya adquiridas» justifica el 0 €; «gratis para el piloto», no")

# 2) El caso del 22 de octubre: se avisa de Azure ML y Logic Apps, no de Power BI Pro
html = _format_cost_report(informe(SYNAPSE, AZURE_ML, LOGIC_APPS, POWER_BI))
aviso = ("Ojo: Azure Machine Learning (servicios básicos para piloto) y Azure Logic Apps (para envío de alertas) "
         "van a 0 €, así que no suman nada al total.")
assert aviso in html, "falta el aviso de los componentes a 0 €"
assert "Power BI Pro (licencias existentes) y" not in html and "y Power BI Pro" not in html
assert html.index("Encaje con el presupuesto") < html.index(aviso), "el aviso va junto al total y al encaje"
assert html.count("0 € (por revisar)") == 2 and html.count("0 € (ya lo tiene el cliente)") == 1
assert ">2.400 €</div>" in html, "los ceros no cambian el total: 600 €/mes × 4 meses"
print("OK 2: Azure ML y Logic Apps salen «por revisar» y con aviso; Power BI Pro, «ya lo tiene el cliente»")

# 3) Con un solo componente a 0 €, la frase va en singular; sin ninguno, no hay aviso
html = _format_cost_report(informe(SYNAPSE, AZURE_ML))
assert "Ojo: Azure Machine Learning (servicios básicos para piloto) va a 0 €" in html
html = _format_cost_report(informe(SYNAPSE, POWER_BI))
assert "a 0 €, así que" not in html and "(por revisar)" not in html
print("OK 3: «va a 0 €» con uno; sin ceros por revisar, ningún aviso")

# 4) El prompt de costes pone la regla del 0 €
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.name == "costes":
            task.output = SimpleNamespace(pydantic=informe(SYNAPSE, AZURE_ML), raw="")
        elif task.name == "equipo":
            task.output = SimpleNamespace(pydantic=None, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado
resultado = ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case={})
assert "Pon precio 0 solo si el cliente ya lo tiene pagado" in prompts[0]
assert "«Ya lo tiene el cliente»" in prompts[0] and "el cómputo, las ejecuciones o un endpoint" in prompts[0]
assert "Azure Machine Learning (servicios básicos para piloto) va a 0 €" in resultado.cost_estimation
print("OK 4: la tarea de costes solo admite 0 € si el cliente ya lo tiene o el uso cabe en el nivel gratuito")

# 5) Infracost se llama con la moneda en euros (variable del CLI 0.10 y del nuevo)
llamada = {}

def run_simulado(cmd, **kwargs):
    llamada.update(cmd=cmd, env=kwargs["env"])
    return SimpleNamespace(returncode=0, stdout="OVERALL TOTAL  €123.45".encode(), stderr=b"")

ec.subprocess.run = run_simulado
assert ec._run_infracost_breakdown('resource "azurerm_resource_group" "rg" {}', "infracost", "clave") == "OVERALL TOTAL  €123.45"
assert llamada["env"]["INFRACOST_CURRENCY"] == "EUR" and llamada["env"]["INFRACOST_CLI_CURRENCY"] == "EUR"
assert llamada["env"]["INFRACOST_API_KEY"] == "clave" and llamada["cmd"][1] == "breakdown"
print("OK 5: Infracost se ejecuta con INFRACOST_CURRENCY=EUR e INFRACOST_CLI_CURRENCY=EUR")

# 6) Fallo 101: una licencia de lo que el cliente ya tiene según sus datos no se suma, salvo que el texto diga que
#    hacen falta más. El 19 nov, con Power BI en Microsoft 365, costes sumaba 10 licencias de Power BI Pro
con_power_bi = {"stack_tecnologico_actual": {"description": "Stack", "value": "ERP antiguo, Microsoft 365 con Power BI"}}
PRO = componente("Power BI Pro - licencias para usuarios de negocio", 9.9, 10, "usuario", "mensual",
                 "Precio público de Power BI Pro.", "licencias")
html = _format_cost_report(informe(SYNAPSE, PRO), problem_case=con_power_bi)
assert "0 € (ya lo tiene el cliente)" in html and ">2.400 €</div>" in html, html  # solo Synapse: 600 €/mes × 4
assert "(por revisar)" not in html
mas = {**PRO, "referencia": "10 licencias adicionales para los usuarios de negocio nuevos, que hoy no tienen Power BI."}
html = _format_cost_report(informe(SYNAPSE, mas), problem_case=con_power_bi)
assert "0 € (ya lo tiene el cliente)" not in html and ">2.796 €</div>" in html, html  # 2.400 + 9,9 × 10 × 4
html = _format_cost_report(informe(SYNAPSE, PRO), problem_case={})  # sin datos del cliente, como antes
assert ">2.796 €</div>" in html
tableau = {**PRO, "componente": "Tableau Creator"}  # una licencia de algo que no tiene se suma
html = _format_cost_report(informe(SYNAPSE, tableau), problem_case=con_power_bi)
assert ">2.796 €</div>" in html
assert "No sumes licencias de lo que el cliente ya tiene" in prompts[0] and "licencias adicionales" in prompts[0]
print("OK 6: una licencia de lo que el cliente ya tiene no se suma, salvo que haga falta más")

# 7) Fallo 101 (cont.): las filas que faltaban, de las fuentes oficiales. Lake Formation no tiene coste propio (los
#    permisos no se cobran): sale «sin coste propio» y no «por revisar». Fabric se paga por CU-hora con la capacidad
#    encendida todo el mes: una F2 son 2 × 730 CU-hora (el 17 nov, el Eventhouse salía a 109.500 €/mes). Document
#    Intelligence se cobra por 1.000 páginas, con la cantidad en páginas
precios = ec.PRECIOS_CLOUD
assert precios["aws-lake-formation-permisos"]["precio_eur"] == 0
assert "lake-formation/pricing" in precios["aws-lake-formation-permisos"]["fuente"]
assert precios["azure-fabric-capacidad-cu"]["fuente"] == "Azure Retail Prices API (spaincentral)"
assert all("westeurope; en spaincentral solo están los medidores Batch" in precios[i]["fuente"] for i in
           ("azure-document-intelligence-read", "azure-document-intelligence-precompilado",
            "azure-document-intelligence-personalizado"))
assert all(precios[i]["fecha"] == "2026-11-20" for i in ("aws-lake-formation-permisos", "azure-fabric-capacidad-cu"))
en_aws = {"plataforma_objetivo": {"value": "Amazon Web Services (AWS)"},
          "stack_tecnologico_actual": {"description": "Stack", "value": "ERP antiguo"}}
LAKE = componente("AWS Lake Formation - gobierno y catalogación de datos", 0, 0, "N/A", "mensual",
                  "Lake Formation sin coste adicional (AWS)")
html = _format_cost_report(informe(SYNAPSE, LAKE), problem_case=en_aws)
assert "0 € (sin coste propio)" in html and "(por revisar)" not in html and "Ojo: AWS Lake Formation" not in html, html
assert "Sin coste propio: Permisos sobre el catálogo" in html and "precio de la tabla" in html
con_tarifa = {**LAKE, "tarifa": "aws-lake-formation-permisos", "unidades": 1}
assert "0 € (sin coste propio)" in _format_cost_report(informe(SYNAPSE, con_tarifa), problem_case=en_aws)
precio_cu = precios["azure-fabric-capacidad-cu"]["precio_eur"]
F2 = componente("Microsoft Fabric F2", 0, 2 * 730, "CU-hora", "mensual", "Capacidad F2 encendida todo el mes")
f2 = ec._con_precio_de_tabla(ComponenteCoste(**{**F2, "tarifa": "azure-fabric-capacidad-cu"}))
assert abs(f2.importe_eur - 2 * precio_cu * 730) < 1e-6 and 240 < f2.importe_eur < 300, f2.importe_eur  # unos 249 €/mes
assert "Ojo:" not in f2.referencia
pocas = ec._con_precio_de_tabla(ComponenteCoste(**{**F2, "tarifa": "azure-fabric-capacidad-cu", "unidades": 40}))
assert "Ojo: una capacidad F2 encendida todo el mes son 1.460 CU-hora al mes" in pocas.referencia, pocas.referencia
paginas = ec._con_precio_de_tabla(ComponenteCoste(**componente(
    "Azure AI Document Intelligence", 0, 5_000, "página", "mensual", "5.000 facturas al mes"),
    tarifa="azure-document-intelligence-precompilado"))
assert abs(paginas.importe_eur - 5 * 8.9771) < 1e-9  # 5.000 páginas son 5 paquetes de 1.000
assert "1.460 CU-hora" in prompts[0] and "Eventhouse" in prompts[0]
print("OK 7: Lake Formation sin coste propio, Fabric por CU-hora encendida todo el mes y Document Intelligence "
      "por 1.000 páginas")

print("\nTodo bien: el fallo 45 está arreglado (en la parte que se puede comprobar sin el LLM real).")