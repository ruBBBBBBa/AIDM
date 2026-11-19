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
rb.HILOS = 1  # fallo 73: las fases en orden, para saber qué salida es de cuál
roadmap = []
rb.inject_roadmap_into_html = lambda *args: roadmap.extend(args)
combinado = rb.roadmap_builder("PROPUESTA SIMULADA", problem_case=CASO)
assert len(roadmap) == 7 and roadmap[0] == "Fuentes: TMS."
assert "6.000 €/año" not in roadmap[2] and "Superar el presupuesto de 90.000 €." in roadmap[2]
assert "$0" not in roadmap[6] and "6.600 €/año" not in combinado
print("OK 8: las 7 secciones del roadmap se filtran antes de ir al informe")

# 9) Fallo 83: los agentes reciben lo que no hay que proponer en la nube elegida y, si una sección lo nombra igualmente,
#    lleva una nota visible y el log lo dice. El 12 nov el informe ponía en Aragón Rekognition Custom Labels, SageMaker
#    Serverless Inference y Macie (no están allí); el 14 nov, Lookout for Vision (retirado el 31 oct 2025)
import logging
import utils

EN_AWS = {**CASO, "plataforma_objetivo": {"value": "Amazon Web Services (AWS)", "region": "Europe (Spain), eu-south-2 (Aragón)"}}
EN_AZURE = {**CASO, "cloud_provider": {"description": "", "value": "Microsoft Azure"}}
contexto = utils.contexto_cliente(EN_AWS)
assert "SERVICIOS QUE NO DEBES PROPONER" in contexto
assert "- Amazon Lookout for Vision: retirado el 31 oct 2025" in contexto and "SageMaker" in contexto
assert "- Amazon Rekognition Custom Labels: no está en Europe (Spain), eu-south-2 (Aragón)" in contexto
assert "Time Series Insights" not in contexto  # solo la nube elegida
assert "Time Series Insights" in utils.contexto_cliente(EN_AZURE)  # sin plataforma objetivo, vale cloud_provider
assert "SERVICIOS QUE NO DEBES PROPONER" not in utils.contexto_cliente(CASO)  # sin nube, nada
nombres = lambda texto, caso: [s["servicio"] for s in utils.revisar_servicios(texto, caso, "prueba")]
assert nombres("La inspección va con Amazon Lookout for Vision y alertas.", EN_AWS) == ["Amazon Lookout for Vision"]
assert nombres("Inferencia en SageMaker Serverless Inference; PII con Amazon Macie.", EN_AWS) == [
    "Amazon SageMaker Serverless Inference", "Amazon Macie"]
# Nombres parecidos que sí valen: las clases S3 Glacier, el Glue Data Catalog, un tal Luis del cliente
assert nombres("Logs a S3 Glacier Deep Archive; esquemas en AWS Glue Data Catalog; IoT Core.", EN_AWS) == []
assert nombres("Luis, el responsable de IT, valida los informes de Power BI.", EN_AZURE) == []
assert nombres("Bots con Language Understanding (LUIS).", EN_AZURE) == ["Language Understanding (LUIS)"]

avisos = []
manejador = logging.Handler()
manejador.emit = avisos.append
logging.getLogger("aidm").addHandler(manejador)
sbc.run_proposal_crew = lambda *a, **k: ProposalResult(
    architecture="Visión con Amazon Lookout for Vision.", governance="Sin servicios raros.",
    data_ai_strategy="Sin servicios raros.", bi_visualization="Power BI.")
sbc.run_estimation_crew = lambda *a, **k: EstimationResult(
    cost_estimation="<div>Amazon Rekognition Custom Labels · 80 €/mes</div>", team_profiles="Perfiles.")
sbc.propose_solution("ESTRUCTURA", EN_AWS, [])
assert inyectado["architecture"].startswith("> **Ojo: esta sección nombra servicios")
assert "**Amazon Lookout for Vision** retirado el 31 oct 2025" in inyectado["architecture"]
assert "en su lugar, un modelo de visión en Amazon SageMaker AI" in inyectado["architecture"]
assert inyectado["governance"] == "Sin servicios raros."
assert inyectado["cost_estimation"].startswith("<blockquote>") and "Rekognition Custom Labels" in inyectado["cost_estimation"]
salidas = ["Fuentes.", "Ingesta.", "Almacenamiento.", "ETL.", "Modelado.", "Detección de PII con Amazon Macie.", "BI."]
roadmap.clear()
rb.roadmap_builder("PROPUESTA SIMULADA", problem_case=EN_AWS)
assert roadmap[5].startswith("> **Ojo:") and "Amazon Macie" in roadmap[5] and roadmap[0] == "Fuentes."
mensajes = [r.getMessage() for r in avisos]
assert any(m.startswith("[servicios] architecture nombra Amazon Lookout for Vision") for m in mensajes), mensajes
assert any(m.startswith("[servicios] roadmap: ML y MLOps nombra Amazon Macie") for m in mensajes), mensajes
print("OK 9: los agentes reciben lo que no hay que proponer en su nube, y la sección que lo nombra lleva una nota")

# 10) Fallo 88: la arquitectura termina con el inventario de servicios; Python lo lee (lista o tabla), lo pone como tabla
#     en el informe y se lo pasa a costes y al roadmap. Sin inventario, todo sigue como antes
from agents import inventario

BLOQUE = """La plataforma va en S3 con Glue.

**INVENTARIO DE SERVICIOS**
- Amazon S3 (capas Bronze, Silver y Gold) | almacenamiento | MVP | Amazon EFS
- AWS Step Functions | orquestación | MVP | AWS Glue Workflows
- Amazon SageMaker AI | IA | completo | —"""
texto, filas = inventario.leer(BLOQUE)
assert texto == "La plataforma va en S3 con Glue." and [f["servicio"] for f in filas] == [
    "Amazon S3 (capas Bronze, Silver y Gold)", "AWS Step Functions", "Amazon SageMaker AI"]
assert [f["fase"] for f in filas] == ["mvp", "mvp", "proyecto_completo"] and filas[2]["alternativa"] == ""
tabla_md = "Texto.\n\n### Inventario de servicios\n| Servicio | Capa | Fase | Alternativa |\n|---|---|---|---|\n" \
           "| Amazon Athena | analítica | Fase 2 | Amazon Redshift |"
assert inventario.leer(tabla_md)[1] == [{"servicio": "Amazon Athena", "capa": "analítica", "fase": "proyecto_completo",
                                         "alternativa": "Amazon Redshift"}]
assert inventario.leer("Sin inventario.") == ("Sin inventario.", [])
assert "INVENTARIO DE SERVICIOS" in inventario.INSTRUCCION and "orquestación" in inventario.INSTRUCCION

recibido = {}
sbc.run_proposal_crew = lambda *a, **k: ProposalResult(architecture=BLOQUE, governance="G.", data_ai_strategy="D.",
                                                       bi_visualization="B.")
sbc.run_estimation_crew = lambda *a, **k: recibido.update(k) or EstimationResult(cost_estimation="<div>c</div>",
                                                                                 team_profiles="P.")
resultado = sbc.propose_solution("ESTRUCTURA", CASO, [])
assert "**INVENTARIO DE SERVICIOS**" not in inyectado["architecture"]  # el bloque crudo no va al informe
assert "#### Inventario de servicios" in inyectado["architecture"]
assert "| AWS Step Functions | orquestación | MVP | AWS Glue Workflows |" in inyectado["architecture"]
assert "| Amazon SageMaker AI | IA | Proyecto completo | — |" in inyectado["architecture"]
assert [f["servicio"] for f in recibido["inventario"]] == [f["servicio"] for f in filas]
assert resultado["inventario"] == filas  # lo lee el roadmap
sbc.run_proposal_crew = lambda *a, **k: ProposalResult(architecture="Sin bloque.", governance="G.",
                                                       data_ai_strategy="D.", bi_visualization="B.")
avisos.clear()
assert sbc.propose_solution("ESTRUCTURA", CASO, [])["inventario"] == [] and recibido["inventario"] == []
assert any("no trae el inventario de servicios" in r.getMessage() for r in avisos)
print("OK 10: el inventario de la arquitectura se lee, va como tabla al informe y llega a costes y al roadmap")

# 11) Fallo 89: la arquitectura elige una opción por papel y deja la otra como alternativa del inventario; si el texto
#     sigue con «X o Y» (el 12 nov: «Delta Lake (o Apache Iceberg)», «Kinesis Data Streams o Firehose»), lo avisa
assert "Elige una sola opción para cada papel" in inventario.REGLA_ELEGIR
filas_89 = [{"servicio": "Delta Lake", "capa": "almacenamiento", "fase": "mvp", "alternativa": "Apache Iceberg"},
            {"servicio": "Amazon Kinesis Data Streams", "capa": "ingesta", "fase": "mvp",
             "alternativa": "Amazon Kinesis Data Firehose"},
            {"servicio": "Amazon Athena", "capa": "analítica", "fase": "mvp", "alternativa": "Amazon Redshift"}]
abierto = ("Se usa el formato **Delta Lake** (o Apache Iceberg) sobre Parquet. Los sensores publican en Amazon Kinesis "
           "Data Streams o directamente en Kinesis Data Firehose. Si crece, se migraría a Amazon Redshift.")
assert inventario.sin_decidir(abierto, filas_89) == ["Delta Lake o Apache Iceberg",
                                                     "Amazon Kinesis Data Streams o Amazon Kinesis Data Firehose"]
assert inventario.sin_decidir("Formato Delta Lake por su time travel; se descarta Apache Iceberg.", filas_89) == []
bloque_89 = abierto + "\n\nINVENTARIO DE SERVICIOS\n" + "\n".join(
    f"- {f['servicio']} | {f['capa']} | MVP | {f['alternativa']}" for f in filas_89)
sbc.run_proposal_crew = lambda *a, **k: ProposalResult(architecture=bloque_89, governance="G.", data_ai_strategy="D.",
                                                       bi_visualization="B.")
sbc.propose_solution("ESTRUCTURA", CASO, [])
assert inyectado["architecture"].startswith("> **Ojo: la arquitectura deja elecciones sin decidir** (Delta Lake o "
                                            "Apache Iceberg; Amazon Kinesis Data Streams o Amazon Kinesis Data Firehose)")
print("OK 11: la arquitectura que deja «X o Y» entre un servicio y su alternativa lleva una nota")

# 12) Fallo 90: con Power BI y los datos fuera de Azure, la puerta de enlace de datos local va en la arquitectura, en el
#     inventario y en los costes. El 12 nov Power BI se conectaba con Athena sin ella
POWER_BI = {"herramienta_de_visualizacion": {"description": "", "value": "Power BI (preferencia por Microsoft 365)"}}
aws_pbi, azure_pbi = {**EN_AWS, **POWER_BI}, {**EN_AZURE, **POWER_BI}
aws_qs = {**EN_AWS, "herramienta_de_visualizacion": {"description": "", "value": "Amazon QuickSight"}}
en_casa = {**POWER_BI, "plataforma_objetivo": {"value": "Microsoft Azure", "despliegue": "on_premises"}}
assert utils.necesita_puerta_de_enlace(aws_pbi) and utils.necesita_puerta_de_enlace(en_casa)
assert not utils.necesita_puerta_de_enlace(azure_pbi) and not utils.necesita_puerta_de_enlace(aws_qs)
assert "on-premises data gateway" in utils.regla_puerta_de_enlace(aws_pbi) and utils.regla_puerta_de_enlace(aws_qs) == ""
sin_puerta = [{"servicio": "Amazon Athena", "capa": "analítica", "fase": "mvp", "alternativa": ""},
              {"servicio": "Power BI Pro", "capa": "visualización", "fase": "mvp", "alternativa": ""}]
filas_90, nota_90 = inventario.con_puerta_de_enlace(sin_puerta, aws_pbi)
assert filas_90[-1]["servicio"] == utils.PUERTA_DE_ENLACE and filas_90[-1]["fase"] == "mvp" and "Ojo: falta" in nota_90
con_puerta = sin_puerta + [{"servicio": "On-premises data gateway", "capa": "visualización", "fase": "mvp",
                            "alternativa": ""}]
assert inventario.con_puerta_de_enlace(con_puerta, aws_pbi) == (con_puerta, "")  # ya la trae: no se duplica
assert inventario.con_puerta_de_enlace(sin_puerta, azure_pbi) == (sin_puerta, "")
bloque_90 = "Power BI lee de Athena.\n\nINVENTARIO DE SERVICIOS\n- Amazon Athena | analítica | MVP | —\n" \
            "- Power BI Pro | visualización | MVP | —"
sbc.run_proposal_crew = lambda *a, **k: ProposalResult(architecture=bloque_90, governance="G.", data_ai_strategy="D.",
                                                       bi_visualization="B.")
sbc.propose_solution("ESTRUCTURA", aws_pbi, [])
assert "Ojo: falta la puerta de enlace de Power BI" in inyectado["architecture"]
assert f"| {utils.PUERTA_DE_ENLACE} | visualización | MVP | — |" in inyectado["architecture"]
assert recibido["inventario"][-1]["servicio"] == utils.PUERTA_DE_ENLACE  # la valoran los costes
# Y la regla va en el prompt de la arquitectura
import agents.proposal_crew as pc
vistos_90 = []


def kickoff_que_mira(self, *args, **kwargs):
    for t in self.tasks:
        vistos_90.append(t.description)
        t.output = SimpleNamespace(raw="texto")
    return SimpleNamespace(raw="texto")


Crew.kickoff = kickoff_que_mira
pc.run_proposal_crew("ESTRUCTURA", aws_pbi, [], model_name="groq/openai/gpt-oss-20b")
assert "PUERTA DE ENLACE DE POWER BI" in vistos_90[0]
print("OK 12: con Power BI y los datos fuera de Azure, la puerta de enlace entra en el prompt, el inventario y los costes")

# 13) Fallo 111: un servicio que la sección nombra solo para descartarlo no se marca. El 18 nov se marcaba
#     «… en sustitución de Macie»
assert nombres("Detección de PII con reglas propias, en sustitución de Amazon Macie.", EN_AWS) == []
assert nombres("Amazon Macie no está disponible en Aragón: la PII se detecta con reglas propias.", EN_AWS) == []
assert nombres("La PII se detecta con reglas propias (Amazon Macie, no disponible en la región).", EN_AWS) == []
assert nombres("Se descarta Amazon Lookout for Vision: está retirado.", EN_AWS) == []
# Se sigue marcando si se propone en otra frase, o si el descarte es de otra cosa
assert nombres("PII con Amazon Macie. El visor nuevo va en lugar del actual.", EN_AWS) == ["Amazon Macie"]
assert nombres("Clasificación de PII con Amazon Macie.\nMás adelante, reglas propias en sustitución de Amazon Macie.",
               EN_AWS) == ["Amazon Macie"]
assert nombres("- Amazon Macie\n- El portal antiguo no disponible", EN_AWS) == ["Amazon Macie"]
print("OK 13: un servicio nombrado solo para descartarlo no se marca; si también se propone, sí")

# 14) Fallo 108: el cruce del inventario con los costes reconoce el mismo servicio aunque costes añada la instancia o
#     las licencias, con guiones Unicode o con paréntesis; dos servicios distintos con nombre parecido no se juntan
mismos = [("AWS Database Migration Service (DMS)", "AWS Database Migration Service (DMS) - instancia t3.medium"),
          ("On-premises Data Gateway (Power BI)", "On‑premises Data Gateway (Power BI) – VM Windows EC2 t3.medium"),
          ("Power BI (Microsoft 365)", "Power BI Pro - licencias para usuarios de negocio"),
          ("Amazon S3 (capas Bronze, Silver y Gold)", "Amazon S3 Standard"),
          ("Amazon Athena", "Amazon Athena, consultas sobre el lago")]
distintos = [("Amazon Kinesis Data Streams", "Amazon Kinesis Data Firehose"),
             ("Amazon S3", "AWS Glue Data Catalog, tablas sobre S3"), ("Amazon Redshift", "Amazon RDS"),
             ("Power BI", "Power Automate"), ("Amazon API Gateway", "On-premises Data Gateway"),
             ("AWS Lambda", "Amazon Lambda-Edge"), ("Amazon SageMaker AI", "Amazon Rekognition")]
assert all(inventario.corresponde(a, b) and inventario.corresponde(b, a) for a, b in mismos), \
    [(a, b) for a, b in mismos if not inventario.corresponde(a, b)]
assert not any(inventario.corresponde(a, b) for a, b in distintos), \
    [(a, b) for a, b in distintos if inventario.corresponde(a, b)]
assert inventario._clave("Amazon S3 (capas Bronze, Silver y Gold)") == "s3"  # el «)» final ya no se queda
from agents import estimation_crew as ec
from agents.estimation_crew import ComponenteCoste
inv = [{"servicio": s, "capa": "x", "fase": "mvp", "alternativa": ""} for s, _ in mismos[:3]]
comps = [ComponenteCoste(componente=c, categoria="infraestructura", precio_unitario_eur=1, unidades=1, unidad="mes",
                         periodicidad="mensual", referencia="x") for _, c in mismos[:3]]
_, sin_valorar, fuera = ec._cruzar_con_inventario(comps, inv)
assert sin_valorar == [] and fuera == [], (sin_valorar, fuera)
print("OK 14: el cruce del inventario con los costes reconoce el mismo servicio y no junta servicios distintos")

print("\nTodo bien: los fallos 44, 83, 88, 89, 90 y 111 están arreglados.")