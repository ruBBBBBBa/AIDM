"""Comprueba el fallo 67: el agente de plataforma elige el hiperescalar y su nube manda en todo el informe.

No llama a ningún LLM: la explicación se simula. Tampoco toca tu informe ni tu caso: trabaja con copias temporales.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_plataforma.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.environ["AIDM_DECISION_URL"] = ""  # fallo 69: este test no usa el modelo de decisión, aunque esté en tu .env
import contextlib
import io
import json
import shutil
import tempfile
from types import SimpleNamespace

import litellm

import utils
from agents import plataforma
from agents import estimation_crew as ec

PLANTILLA = json.loads(Path("data_structures/problem_case_recovery.json").read_text(encoding="utf-8"))
CONFIG = plataforma._config()


def caso(**campos) -> dict:
    nuevo = json.loads(json.dumps(PLANTILLA))
    for campo, valor in campos.items():
        nuevo[campo]["value"] = valor
    nuevo["status"] = "completed"
    return nuevo


# El cliente de la ejecución del 9 nov (AgroLogística del Norte)
AGRO = caso(
    cloud_provider="Azure como prioridad, con un módulo actual en AWS (EC2 y S3 para sensores de temperatura de "
                   "cámaras frigoríficas) que no quieren migrar ni rehacer",
    stack_tecnologico_actual="Microsoft 365 y Excel en toda la empresa; ERP propio",
    tamano_equipo_tecnico="2 personas en IT (responsable de sistemas y técnico de soporte). Excel avanzado y SQL básico",
)

# 1) Un cliente que ya está en Azure y vive en Microsoft 365: se mantiene Azure, sin formación de plataforma
r = plataforma.puntuar(AGRO, CONFIG)
assert r["senales"]["actuales"] == ["azure", "aws"] and "microsoft" in r["senales"]["ecosistemas"], r["senales"]
assert (r["decision"], r["recomendado"], r["formacion"]) == ("mantener", "azure", None), r
assert r["orden"][0] == "azure" and len(r["orden"]) == 5
for clave, puntos in r["puntos"].items():
    assert abs(sum(puntos[c] for c in plataforma.CRITERIOS) - puntos["total"]) < 0.2, (clave, puntos)
tabla = plataforma.tabla(r, CONFIG)
assert tabla.count("\n") == 6 and "**Microsoft Azure (ya la usa o la prefiere)**" in tabla, tabla
print("OK 1: el cliente del 9 nov, en Azure y con Microsoft 365, se queda en Azure; las 5 nubes salen en la tabla")

# 2) Si otra nube le saca bastante a la que usa, se recomienda cambiar y se calcula la formación de su equipo
cambio = caso(cloud_provider="IBM Cloud", tamano_equipo_tecnico="dos técnicos de sistemas",
              stack_tecnologico_actual="Microsoft 365, Power BI y Databricks para los cuadros de mando")
r = plataforma.puntuar(cambio, CONFIG)
assert (r["decision"], r["actual"], r["recomendado"]) == ("cambiar", "ibm", "azure"), r
assert r["ventaja"] >= CONFIG["cambiar_si_ventaja"]
f = CONFIG["formacion"]
horas = 2 * (f["horas_fundamentos"] + f["horas_especialidad"])
assert r["formacion"]["personas"] == 2 and r["formacion"]["personas_del_caso"], r["formacion"]
assert (r["formacion"]["horas"], r["formacion"]["coste_eur"]) == (horas, horas * f["coste_hora_eur"])
texto = plataforma.resumen(r, CONFIG)
assert "**Cambiar de IBM Cloud a Microsoft Azure**" in texto and "Ley de Datos de la UE" in texto, texto
assert "**No admite lo que ya usa el cliente:** IBM Cloud (Databricks)" in texto, texto
assert f"2 personas, unas {horas} horas" in texto and "DP-700" in texto, texto
print(f"OK 2: de IBM Cloud, que no admite su Databricks, a Azure (le saca {r['ventaja']} puntos), con la "
      f"formación de 2 personas: {horas} horas")

# 3) Si la ventaja no llega al umbral, se mantiene la nube actual y el texto lo explica
r = plataforma.puntuar(caso(cloud_provider="IBM Cloud", stack_tecnologico_actual="Microsoft 365"), CONFIG)
assert r["decision"] == "mantener" and r["recomendado"] == "ibm" and 0 < r["ventaja"] < CONFIG["cambiar_si_ventaja"], r
assert "por debajo de los 15 que hacen falta" in plataforma.resumen(r, CONFIG)
print(f"OK 3: con una ventaja de {r['ventaja']} puntos (menos de 15), se mantiene la nube actual")

# 4) Sin nube todavía: se elige una, y su equipo (que el caso no cuenta) se forma con el número por defecto
r = plataforma.puntuar(caso(stack_tecnologico_actual="Google Workspace y Gmail"), CONFIG)
assert (r["decision"], r["recomendado"]) == ("elegir", "google"), r
assert r["formacion"]["personas"] == f["personas_si_no_se_sabe"] and not r["formacion"]["personas_del_caso"]
assert "supuesto: el caso no dice cuántas" in plataforma.resumen(r, CONFIG)
print("OK 4: sin nube, con Google Workspace, sale Google Cloud; la formación usa el equipo por defecto y lo avisa")

# 5) Si el cliente pide ENS y la nube no lo tiene confirmado, el informe lo dice
r = plataforma.puntuar(caso(cloud_provider="Oracle Cloud (OCI)", regulacion_y_compliance="ENS, es un ayuntamiento"),
                       CONFIG)
assert r["senales"]["pide_ens"] and r["recomendado"] == "oracle"
assert "**ENS categoría alta:** por confirmar" in plataforma.resumen(r, CONFIG)
assert "no hemos podido confirmar la certificación" in plataforma.resumen(r, CONFIG)
print("OK 5: un ayuntamiento en OCI: el ENS sale «por confirmar» con el aviso de comprobarlo")

# 6) elegir_plataforma(): escribe la sección en el informe y guarda la plataforma objetivo en el caso
carpeta = Path(tempfile.mkdtemp())
plataforma.REPORT_PATH = carpeta / "informe.html"
shutil.copy("data_structures/solution_builder_report_recovery.html", plataforma.REPORT_PATH)
utils.PROBLEM_CASE_PATH = carpeta / "problem_case.json"
pedidos = []


def completion_simulada(*args, **kwargs):
    pedidos.append(kwargs)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="Recomendamos mantener Azure porque toda la empresa trabaja con Microsoft 365."))])


completion_real, litellm.completion = litellm.completion, completion_simulada
log = io.StringIO()
try:
    with contextlib.redirect_stdout(log):
        nuevo = plataforma.elegir_plataforma(AGRO)
finally:
    litellm.completion = completion_real
assert pedidos and pedidos[0]["metadata"] == {"aidm_tarea": "plataforma"}, pedidos
prompt = pedidos[0]["messages"][-1]["content"]
assert "COMPARACIÓN DE INETUM" in prompt and "| **Microsoft Azure" in prompt
assert "[plataforma] Microsoft Azure (mantener); notas: Microsoft Azure 100" in log.getvalue(), log.getvalue()
guardado = json.loads(utils.PROBLEM_CASE_PATH.read_text(encoding="utf-8"))
assert guardado["plataforma_objetivo"]["value"] == "Microsoft Azure" == nuevo["plataforma_objetivo"]["value"]
assert guardado["plataforma_objetivo"]["decision"] == "mantener"
html = plataforma.REPORT_PATH.read_text(encoding="utf-8")
seccion = html.split('id="content-platform"')[1].split('<div class="section-card">')[0]
assert "Mantener Microsoft Azure" in seccion and "Recomendamos mantener Azure" in seccion and "<table>" in seccion
assert "Contenido pendiente" not in seccion
print("OK 6: la sección sale en el informe (decisión, explicación y tabla) y el caso guarda la plataforma objetivo")

# 7) Si el LLM falla, la sección sale igual, con la decisión y la tabla
shutil.copy("data_structures/solution_builder_report_recovery.html", plataforma.REPORT_PATH)
litellm.completion = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin cupo"))
try:
    with contextlib.redirect_stdout(io.StringIO()) as salida:
        plataforma.elegir_plataforma(AGRO)
finally:
    litellm.completion = completion_real
seccion = plataforma.REPORT_PATH.read_text(encoding="utf-8").split('id="content-platform"')[1]
assert "Mantener Microsoft Azure" in seccion and "<table>" in seccion
assert "No se pudo escribir la explicación: RuntimeError" in salida.getvalue()
print("OK 7: si el modelo falla, la sección lleva igualmente la decisión y la tabla")

# 8) Los demás agentes reciben la plataforma objetivo, y los costes usan sus precios aunque cloud_provider diga otra
contexto = utils.contexto_cliente(nuevo)
assert "PLATAFORMA OBJETIVO: Microsoft Azure (el cliente ya la usa o la prefiere, y se mantiene)" in contexto
assert "- plataforma_objetivo" not in contexto
en_aws = caso(cloud_provider="AWS")
en_aws["plataforma_objetivo"] = {"value": "Microsoft Azure", "motivo": "recomendada por Inetum"}
assert "azure-data-factory" in ec._tabla_precios(en_aws, "Azure Data Lake Storage Gen2 con Azure Data Factory")
assert ec._tabla_precios(caso(cloud_provider="AWS"), "Azure Data Lake Storage Gen2 con Azure Data Factory") == ""
print("OK 8: el contexto de todos los agentes lleva la PLATAFORMA OBJETIVO y la tabla de precios es la de esa nube")

# 9) El grafo pasa por la plataforma entre el discovery y la estructura; el discovery no la ve como un dato
import graph
from agents import discovery_agent_logic as discovery

assert graph.route_after_discovery({"completion_reached": True}) == "plataforma"
aristas = graph.aidm_graph.builder.edges
assert ("plataforma", "structure_proposal") in aristas, aristas
utils.PROBLEM_CASE_PATH.write_text(json.dumps({**AGRO, "status": "pending fields",
                                               "plataforma_objetivo": nuevo["plataforma_objetivo"]}), encoding="utf-8")
vistos = []
litellm.completion = lambda *a, **k: vistos.append(k) or SimpleNamespace(choices=[SimpleNamespace(
    message=SimpleNamespace(content='{"updates": [], "assistant_reply": "¿Algo más?", "update_status": null}'))])
try:
    with contextlib.redirect_stdout(io.StringIO()):
        discovery._propose_plan_with_agent("Hola", [])
finally:
    litellm.completion = completion_real
assert vistos and all("plataforma_objetivo" not in m["content"] for m in vistos[0]["messages"])
print("OK 9: el grafo va del discovery a la plataforma y de ahí a la estructura; el discovery no ve la plataforma")

# 10) Las dos plantillas del informe tienen la sección nueva antes de la arquitectura
for nombre in ("solution_builder_report.html", "solution_builder_report_recovery.html"):
    html = Path("data_structures", nombre).read_text(encoding="utf-8")
    assert html.count('id="content-platform"') == 1 and html.index('id="content-platform"') < html.index(
        'id="content-architecture"'), nombre
print("OK 10: las dos plantillas tienen la sección «Plataforma Recomendada» antes de la arquitectura")

# 11) Fallo 87: las licencias de Power BI están en la tabla y valen con cualquier nube; si la nube del cliente no tiene
#     filas (el 12 nov, AWS: 19 avisos de «Tarifa desconocida»), no se avisa de las tarifas desconocidas, y sin
#     ninguna fila que ofrecer el JSON de costes va sin el campo tarifa
import logging
pro = ec.PRECIOS_CLOUD["power-bi-pro"]
assert pro["precio_eur"] == 12.10 and ec.PRECIOS_CLOUD["power-bi-premium-por-usuario"]["precio_eur"] == 20.80
assert "microsoft.com/es-es" in pro["fuente"] and pro["fecha"]
# Google Cloud aún no tiene filas: solo las licencias (desde el fallo 87, parte 2, AWS sí tiene)
assert [f["id"] for f in ec._filas_precios(caso(cloud_provider="Google Cloud"), "BigQuery y Power BI")] == ["power-bi-pro"]
assert {"aws-s3-standard", "aws-athena", "power-bi-pro"} <= {
    f["id"] for f in ec._filas_precios(caso(cloud_provider="AWS"), "Amazon S3, Athena y Power BI")}
assert "power-bi-pro" in ec._tabla_precios(en_aws, "Azure Data Factory y Power BI")  # con Azure, junto a las suyas
en_sus_servidores = caso(cloud_provider="Azure")
en_sus_servidores["plataforma_objetivo"] = {"value": "Microsoft Azure", "despliegue": "on_premises"}
assert [f["id"] for f in ec._filas_precios(en_sus_servidores, "Azure Data Factory y Power BI")] == ["power-bi-pro"]
assert '"tarifa"' not in ec._plantilla_costes(con_tarifa=False) and '"tarifa"' in ec._plantilla_costes(con_tarifa=True)
componentes = [ec.ComponenteCoste(componente="Licencias Power BI Pro", tarifa="power-bi-pro", categoria="licencias",
                                  precio_unitario_eur=0, unidades=40, unidad="usuario", periodicidad="mensual",
                                  referencia="40 comerciales"),
               ec.ComponenteCoste(componente="Amazon S3", tarifa="Almacenamiento del lago", categoria="infraestructura",
                                  precio_unitario_eur=0.024, unidades=500, unidad="GB", periodicidad="mensual",
                                  referencia="Precio público")]
informe = ec.CostEstimationReport(componentes=componentes, supuestos=[], resumen_ejecutivo=ec.ResumenEjecutivo(
    meses_proyecto=4, presupuesto_cliente_max_eur=100_000, justificacion="Precios públicos."))
avisos = io.StringIO()
manejador = logging.StreamHandler(avisos)
logging.getLogger().addHandler(manejador)
try:
    html = ec._format_cost_report(informe, "", None, caso(cloud_provider="AWS"), avisar_tarifas=False)
    assert "Tarifa desconocida" not in avisos.getvalue(), avisos.getvalue()
    ec._format_cost_report(informe, "", None, caso(cloud_provider="AWS"))  # con filas de su nube, sí se avisa
    assert "Tarifa desconocida en costes: 'Almacenamiento del lago'" in avisos.getvalue()
finally:
    logging.getLogger().removeHandler(manejador)
assert "484 €/mes" in html and "12,10 €/usuario al mes × 40 · precio de la tabla" in html  # 40 × 12,10
assert "12 €/mes" in html  # S3 se queda con el precio del modelo
print("OK 11: Power BI Pro y PPU están en la tabla con cualquier nube, y sin filas de la nube no hay avisos de tarifa")

# 12) Fallo 87 (parte 2): los precios de AWS salen de la Price List Bulk API, con la SKU de cada fila en bd/precios_aws.yaml;
#     se guardan en dólares y se pasan a euros al leerlos. Sin red: la API se simula
import csv
import yaml

sys.path.insert(0, str(RAIZ / "bd"))
import actualizar_precios as ap

oferta = {"products": {"A": {"attributes": {"usagetype": "EUS2-Uso"}}, "B": {"attributes": {"usagetype": "EUS2-Otro"}}},
          "terms": {"OnDemand": {"A": {"t": {"priceDimensions": {
              "1": {"beginRange": "0", "unit": "GB-Mo", "pricePerUnit": {"USD": "0.023"}},
              "2": {"beginRange": "51200", "unit": "GB-Mo", "pricePerUnit": {"USD": "0.022"}}}}},
              "B": {"t": {"priceDimensions": {"1": {"beginRange": "0", "unit": "Messages",
                                                    "pricePerUnit": {"USD": "0.000001"}}}}}}}}
pedidas = []


def pedir_simulado(url):
    pedidas.append(url)
    if url.endswith("region_index.json"):
        return {"regions": {"eu-south-2": {"currentVersionUrl": "/archivo.json"}}} if "Buena" in url else {"regions": {}}
    return oferta


assert ap.precio_aws(oferta, "EUS2-Uso", 0) == (0.023, "GB-Mo") and ap.precio_aws(oferta, "EUS2-Uso", 51200)[0] == 0.022
assert ap.precio_aws(oferta, "EUS2-Nada") is None
config = {"region": "eu-south-2", "filas": [
    {"id": "aws-s3", "servicio": "S3", "buscar": ["amazon s3"], "concepto": "c", "oferta": "Buena", "usagetype": "EUS2-Uso",
     "unidad": "GB al mes", "por_cada": 1, "cobro": "GB al mes"},
    {"id": "aws-iot", "servicio": "IoT", "buscar": ["iot core"], "concepto": "c", "oferta": "Buena",
     "usagetype": "EUS2-Otro", "unidad": "mensaje", "por_cada": 1000000, "cobro": "millón de mensajes"},
    {"id": "aws-x", "servicio": "Sin región", "buscar": ["x"], "concepto": "c", "oferta": "Mala", "usagetype": "EUS2-Uso",
     "unidad": "u", "por_cada": 1, "cobro": "u"}],
    "sin_fila": [{"servicio": "Amazon EC2", "motivo": "archivo enorme"}],
    "licencias_sin_fila": [{"servicio": "Tableau", "motivo": "403"}]}
carpeta_precios = Path(tempfile.mkdtemp())
csv_prueba, config_prueba = carpeta_precios / "precios.csv", carpeta_precios / "precios_aws.yaml"
shutil.copy(RAIZ / "bd" / "precios_cloud.csv", csv_prueba)
config_prueba.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
original_csv = csv_prueba.read_bytes()
with contextlib.redirect_stdout(io.StringIO()):
    informe = ap.actualizar_todo(probar=True, solo="aws", pedir=pedir_simulado, hoy="2026-11-17",
                                 csv_precios=csv_prueba, config_aws=config_prueba)
assert csv_prueba.read_bytes() == original_csv, "--probar no escribe"
assert not any("region_index" in u and "EC2" in u for u in pedidas)  # solo las ofertas de la lista
assert informe["nueva"] == ["aws-s3: 0.023 $ por GB al mes", "aws-iot: 1 $ por millón de mensajes"], informe["nueva"]
assert informe["sin_fila"] == ["Sin región: Mala no tiene precios en eu-south-2", "Amazon EC2: archivo enorme"]
assert informe["licencias_sin_fila"] == ["Tableau: 403"]
log = io.StringIO()
with contextlib.redirect_stdout(log):
    ap.actualizar_todo(solo="aws", pedir=pedir_simulado, hoy="2027-03-01", csv_precios=csv_prueba,
                       config_aws=config_prueba)
assert "Informe de cobertura" in log.getvalue()
with open(csv_prueba, encoding="utf-8") as f:
    escritas = {fila["id"]: fila for fila in csv.DictReader(f, delimiter=";")}
assert escritas["aws-iot"]["moneda"] == "USD" and escritas["aws-iot"]["precio_origen"] == "1"
assert escritas["aws-iot"]["precio_eur"] == "" and escritas["power-bi-pro"]["precio_eur"] == "12.10"  # las otras, igual
assert any(l.startswith("power-bi-pro: precio del 2026-11-12") for l in ap.licencias_viejas(list(escritas.values()), "2027-03-01"))
with contextlib.redirect_stdout(io.StringIO()):  # la segunda vez, sin cambios
    otra = ap.actualizar_todo(solo="aws", pedir=pedir_simulado, hoy="2027-03-01", csv_precios=csv_prueba,
                              config_aws=config_prueba)
assert otra["igual"] == ["aws-s3: 0.023 $", "aws-iot: 1 $"] and not otra["cambia"]
# Al leer la tabla, los dólares pasan a euros con el tipo fijo de costes.yaml, y la referencia lo dice
ec_csv = ec.CSV_PRECIOS
ec.CSV_PRECIOS = csv_prueba
try:
    leidas = ec._cargar_precios()
finally:
    ec.CSV_PRECIOS = ec_csv
tipo = ec._cambio()["usd_por_eur"]
assert abs(leidas["aws-iot"]["precio_eur"] - 1 / tipo) < 1e-9 and leidas["power-bi-pro"]["precio_eur"] == 12.10
# Y en la tabla real del repo, las 18 filas de AWS
filas_aws = [f for f in ec.PRECIOS_CLOUD.values() if f["proveedor"] == "AWS"]
assert len(filas_aws) == 18 and abs(ec.PRECIOS_CLOUD["aws-athena"]["precio_eur"] - 5.65 / tipo) < 1e-9
assert "aws-athena" in ec._tabla_precios(caso(cloud_provider="AWS"), "Consultas con Amazon Athena sobre S3")
print("OK 12: los precios de AWS salen de la Price List Bulk API, en dólares, y se pasan a euros al leerlos")

print("\nTodo bien: el fallo 67 funciona (en la parte que se puede comprobar sin el LLM real).")
