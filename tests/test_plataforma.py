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
os.environ["AIDM_SIN_POSICIONAMIENTO"] = "1"  # fallo 135: sin el hilo del cuadrante (llamaría a Groq de verdad)
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
# Y en la tabla real del repo, las filas de AWS (19 desde el fallo 90, con la de EC2 Windows; 21 desde el 101 (parte 3),
# con Glue Data Quality y la detección de datos sensibles)
filas_aws = [f for f in ec.PRECIOS_CLOUD.values() if f["proveedor"] == "AWS" and f["moneda"] == "USD"]
assert len(filas_aws) == 21 and abs(ec.PRECIOS_CLOUD["aws-athena"]["precio_eur"] - 5.65 / tipo) < 1e-9
assert "aws-athena" in ec._tabla_precios(caso(cloud_provider="AWS"), "Consultas con Amazon Athena sobre S3")
print("OK 12: los precios de AWS salen de la Price List Bulk API, en dólares, y se pasan a euros al leerlos")

# 13) Fallo 90: la VM Windows de la puerta de enlace sale del CSV de EC2 (265 MB en JSON), leído línea a línea y
#     saltando los metadatos de antes de la cabecera. El mismo usagetype es Linux, Windows, Windows con SQL Server,
#     bajo demanda o reservado: los filtros tienen que dejar una sola fila, y si dejan más, la fila no se actualiza
CSV_EC2 = (
    '"FormatVersion","v1.0"\n"Disclaimer","Informativo"\n"Publication Date","2026-11-20T03:48:50Z"\n'
    '"Version","20261120034850"\n"OfferCode","AmazonEC2"\n'
    '"SKU","OfferTermCode","RateCode","TermType","PriceDescription","EffectiveDate","StartingRange","EndingRange",'
    '"Unit","PricePerUnit","Currency","usageType","operation","Tenancy","CapacityStatus","Pre Installed S/W",'
    '"Operating System"\n')
filas_ec2 = [  # (TermType, precio, operación, capacidad, software)
    ("OnDemand", "0.0640000000", "RunInstances:0002", "Used", "NA"),  # la buena
    ("Reserved", "0.0410000000", "RunInstances:0002", "Used", "NA"),
    ("OnDemand", "0.0416000000", "RunInstances", "Used", "NA"),  # Linux
    ("OnDemand", "0.0640000000", "RunInstances:0002", "UnusedCapacityReservation", "NA"),
    ("OnDemand", "0.5440000000", "RunInstances:0006", "Used", "SQL Std"),
    ("OnDemand", "0.1280000000", "RunInstances:0002", "Used", "SQL Web")]
for n, (termino, precio, operacion, capacidad, software) in enumerate(filas_ec2):
    CSV_EC2 += (f'"SKU{n}","T","R","{termino}","x","2026-11-01","0","Inf","Hrs","{precio}","USD",'
                f'"EUS2-BoxUsage:t3.medium","{operacion}","Shared","{capacidad}","{software}","Windows"\n')
CSV_EC2 += '"SKU9","T","R","OnDemand","x","2026-11-01","0","Inf","Hrs","0.0832","USD","EUS2-BoxUsage:t3.large",' \
           '"RunInstances:0002","Shared","Used","NA","Windows"\n'


def abrir_simulado(url, timeout=None):
    assert url.endswith("/eu-south-2/index.csv"), url
    return io.BytesIO(CSV_EC2.encode("utf-8"))


datos_ec2 = ap.leer_csv_aws("https://x/eu-south-2/index.csv", {"EUS2-BoxUsage:t3.medium"}, abrir=abrir_simulado)
assert len(datos_ec2["products"]) == 6  # solo las del usagetype pedido; t3.large no
FILTROS = {"operation": "RunInstances:0002", "tenancy": "Shared", "termType": "OnDemand", "capacityStatus": "Used",
           "preInstalledSw": "NA"}
assert ap.precio_aws(datos_ec2, "EUS2-BoxUsage:t3.medium", 0, FILTROS) == (0.064, "Hrs")
try:  # sin el filtro de software, también la de SQL Web: no se elige ninguna
    ap.precio_aws(datos_ec2, "EUS2-BoxUsage:t3.medium", 0, {k: v for k, v in FILTROS.items() if k != "preInstalledSw"})
    raise AssertionError("con dos filas tenía que parar")
except ap.VariasFilas as error:
    assert "2 precios" in str(error)
fila_ec2 = {"id": "aws-ec2-win", "servicio": "EC2 Windows", "buscar": ["ec2 windows"], "concepto": "c",
            "oferta": "AmazonEC2", "formato": "csv", "usagetype": "EUS2-BoxUsage:t3.medium", "filtros": FILTROS,
            "unidad": "hora", "por_cada": 1, "cobro": "hora"}
pedidas.clear()
leidos = []
filas_prueba = []
informe = ap.actualizar_aws(filas_prueba, {"region": "eu-south-2", "filas": [fila_ec2]},
                            pedir=lambda url: pedidas.append(url) or {"regions": {"eu-south-2": {
                                "currentVersionUrl": "/offers/v1.0/aws/AmazonEC2/1/eu-south-2/index.json"}}},
                            hoy="2026-11-20", leer_csv=lambda url, tipos: leidos.append((url, tipos)) or datos_ec2)
assert informe["nueva"] == ["aws-ec2-win: 0.064 $ por hora"] and filas_prueba[0]["precio_origen"] == "0.064"
assert leidos == [(ap.API_AWS + "/offers/v1.0/aws/AmazonEC2/1/eu-south-2/index.csv", {"EUS2-BoxUsage:t3.medium"})]
assert not any(u.endswith("eu-south-2/index.json") for u in pedidas)  # el JSON de EC2 no se baja
sin_filtro = {**fila_ec2, "filtros": {"operation": "RunInstances:0002"}}
informe = ap.actualizar_aws([], {"region": "eu-south-2", "filas": [sin_filtro]},
                            pedir=lambda url: {"regions": {"eu-south-2": {"currentVersionUrl": "/a/eu-south-2/index.json"}}},
                            leer_csv=lambda url, tipos: datos_ec2)
assert informe["sin_fila"][0].startswith("EC2 Windows: 4 precios para EUS2-BoxUsage:t3.medium"), informe
# En la tabla real: la fila de EC2 Windows, de la API, y la puerta de enlace la encuentra con AWS
ec2 = ec.PRECIOS_CLOUD["aws-ec2-windows-t3-medium"]
assert ec2["precio_origen"] == "0.064" and "AmazonEC2" in ec2["fuente"] and ec2["fecha"] >= "2026-11-20"  # se actualiza
assert "aws-ec2-windows-t3-medium" in ec._tabla_precios(caso(cloud_provider="AWS"),
                                                        "On-premises Data Gateway (Power BI) – VM Windows EC2 t3.medium")
print("OK 13: la VM Windows de la puerta de enlace sale del CSV de EC2, con una sola fila tras los filtros")

# 14) Fallo 135: el cuadrante de posicionamiento. Las guardias, el prompt (cabe en Groq), el JSON ilegible o la llamada
#     que falla (no se guarda nada y el informe sigue) y que plataforma_node, en los tests, no lanza el hilo
import logging
import threading
import gateway
from agents import posicionamiento as pos

avisos_pos = []
recoger_pos = logging.Handler()
recoger_pos.emit = lambda r: avisos_pos.append(r.getMessage())
logging.getLogger("aidm").addHandler(recoger_pos)
carpeta_pos = Path(tempfile.mkdtemp())
pos.RUTA = carpeta_pos / "posicionamiento.json"  # nunca el de data_structures
LANA = caso(nombre_del_cliente="Lanas del Cierzo, S.L.", sector="Textil y moda", tamaño_empresa="Mediana, 40 tiendas",
            objetivo_del_proyecto="Previsión de demanda por tienda", madurez_analitica_actual="Excel semanal",
            plazo_objetivo="9 meses", evidencias=["Tiendas en Aragón y Navarra"])
BUENO = {"area": "Previsión de demanda y stock",
         "cliente": {"hoy": {"x": -2, "y": "6,5", "motivo": "Informes en Excel desde 2019"},
                     "con_proyecto": {"x": 14, "y": 8, "motivo": "Lanas del Cierzo tendrá previsión por tienda"}},
         "competidores": [{"nombre": "Tejidos Alfa", "x": 8, "y": 9, "motivo": "Analítica propia", "confianza": "ALTA"},
                          {"nombre": "Lanas del Cierzo", "x": 3, "y": 3, "motivo": "El cliente", "confianza": "alta"},
                          {"nombre": "Sin posición", "x": "mucho", "y": 2},
                          {"nombre": "Moda Beta", "x": 5, "y": 4, "motivo": "Crece un 20 % al año", "confianza": "rara"},
                          {"nombre": "Gamma", "x": 2, "y": 2}, {"nombre": "Delta", "x": 4, "y": 6},
                          {"nombre": "Épsilon", "x": 6, "y": 1}, {"nombre": "tejidos alfa", "x": 1, "y": 1}]}
limpio = pos.limpiar(BUENO, LANA)
assert limpio["cliente"]["hoy"] == {"x": 0.0, "y": 6.5, "motivo": "Informes en Excel desde 2019"}, limpio  # el año vale
assert limpio["cliente"]["con_proyecto"] == {"x": 10.0, "y": 8.0, "motivo": ""}, "el nombre del cliente no sale"
assert [c["nombre"] for c in limpio["competidores"]] == ["Tejidos Alfa", "Moda Beta", "Gamma", "Delta"], limpio
assert [c["confianza"] for c in limpio["competidores"][:2]] == ["alta", "baja"] and limpio["competidores"][1]["motivo"] == ""
assert limpio["ejes"] == {"x": "Madurez actual en previsión de demanda y stock",
                          "y": "Ambición e inversión en previsión de demanda y stock"} and limpio["aviso"] == pos.AVISO
assert any("con cifras" in a for a in avisos_pos) and any("salía como competidor" in a for a in avisos_pos), avisos_pos
assert pos.limpiar({**BUENO, "competidores": [BUENO["competidores"][1]]}, LANA) is None, "sin competidores, nada"
assert pos.limpiar({**BUENO, "cliente": {"hoy": BUENO["cliente"]["hoy"]}}, LANA) is None
assert pos.limpiar({**BUENO, "area": ""}, LANA) is None and pos.limpiar(["no"], LANA) is None
# El prompt: con datos enormes, no pasa de PROMPT_MAX y, con el max_tokens, cabe en los 8.000 por minuto de Groq
ENORME = caso(**{c: "dato largo " * 400 for c in pos.CAMPOS}, nombre_del_cliente="Lanas del Cierzo, S.L.",
              evidencias=["evidencia larga " * 100] * 30)
for prueba in (LANA, ENORME):
    m = pos.mensajes(prueba)
    tokens = pos.tokens_aproximados(m[0]["content"] + m[1]["content"])
    assert tokens <= pos.PROMPT_MAX and tokens + pos.MAX_TOKENS < 8000, tokens
datos_lana = pos.mensajes(LANA)[1]["content"]  # la forma real de problem_case.json: {"description", "value"}
for linea in ("- sector: Textil y moda", "- objetivo_del_proyecto: Previsión de demanda por tienda",
              "- madurez_analitica_actual: Excel semanal", "Tiendas en Aragón y Navarra"):
    assert linea in datos_lana, (linea, datos_lana)
assert "Nunca inventes una empresa" in pos.INSTRUCCIONES and "sin cifras" in pos.INSTRUCCIONES
# El perfil: Groq y Cloudflare de reserva, sin ningún Gemini
assert gateway.modelos_de("posicionamiento", con_saltados=True) == (
    "posicionamiento", ["groq/openai/gpt-oss-120b", "cloudflare/@cf/openai/gpt-oss-120b"])
# El hilo: JSON ilegible o llamada que falla → no se guarda nada y sale [posicionamiento]; si va bien, se guarda
pedir_real = pos.pedir
avisos_pos.clear()
pos.pedir = lambda c, modelo=None: "Lo siento, no puedo darte un JSON"
assert pos.trabajar(LANA, pos._actual) is None and not pos.RUTA.exists()
assert any(a.startswith("[posicionamiento]") for a in avisos_pos), avisos_pos


def pedir_que_falla(c, modelo=None):
    raise TimeoutError("Groq no contesta")


avisos_pos.clear()
pos.pedir = pedir_que_falla
assert pos.trabajar(LANA, pos._actual) is None and not pos.RUTA.exists()
assert any(a.startswith("[posicionamiento] Sin cuadrante (TimeoutError") for a in avisos_pos), avisos_pos
pos.pedir = lambda c, modelo=None: json.dumps(BUENO)
assert pos.trabajar(LANA, pos._actual) == limpio and json.loads(pos.RUTA.read_text(encoding="utf-8")) == limpio
# Una ejecución vieja (se pulsó «Nueva sesión» o empezó otra) no escribe
mio = pos._actual
pos.olvidar()
assert not pos.RUTA.exists()
avisos_pos.clear()
assert pos.trabajar(LANA, mio) is None and not pos.RUTA.exists()
assert any("ya no es la actual" in a for a in avisos_pos), avisos_pos
pos.pedir = pedir_real
# plataforma_node en los tests (AIDM_SIN_POSICIONAMIENTO=1): ni hilo ni gateway.completar
completados, hilos = [], []
completar_real, hilo_real = gateway.completar, pos.threading.Thread
gateway.completar = lambda *a, **k: completados.append(k) or None
elegir_real = graph.elegir_plataforma
graph.elegir_plataforma = lambda c: {**c, utils.PLATAFORMA_OBJETIVO: {"value": "Microsoft Azure"}}
pos.threading.Thread = lambda *a, **k: hilos.append(k) or hilo_real(target=lambda: None)


def hilo_que_falla(*a, **k):
    raise RuntimeError("sin hilos")


try:
    assert os.environ.get("AIDM_SIN_POSICIONAMIENTO") == "1"
    with contextlib.redirect_stdout(io.StringIO()):
        salida = graph.plataforma_node({"problem_case": LANA})
    assert salida["problem_case"][utils.PLATAFORMA_OBJETIVO]["value"] == "Microsoft Azure"
    assert not hilos and not completados, (hilos, completados)
    assert not [h for h in threading.enumerate() if h.name == "posicionamiento"]
    # Sin la variable, lanzar() solo crea el hilo (aquí, uno vacío), sin esperarlo, y borra el cuadrante anterior
    del os.environ["AIDM_SIN_POSICIONAMIENTO"]
    pos.RUTA.write_text("{}", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        graph.plataforma_node({"problem_case": LANA})
    assert len(hilos) == 1 and hilos[0]["target"] is pos.trabajar and hilos[0]["daemon"] is True, hilos
    assert hilos[0]["args"][2] == pos.RUTA and not pos.RUTA.exists() and not completados
    # Con la forma real del caso pero sin sector ni objetivo (el caso vacío del 26 nov): ni hilo ni llamada
    hilos.clear()
    avisos_pos.clear()
    pos.RUTA.write_text("{}", encoding="utf-8")
    vacio = caso(madurez_analitica_actual="Excel semanal")
    assert pos.faltan(vacio) == ["sector", "objetivo_del_proyecto"] and pos.faltan(LANA) == []
    with contextlib.redirect_stdout(io.StringIO()):
        graph.plataforma_node({"problem_case": vacio})
    assert not hilos and not completados and not pos.RUTA.exists(), hilos
    assert "[posicionamiento] Caso sin datos (falta sector, objetivo_del_proyecto): no se llama al modelo" in avisos_pos
    # Si no se puede lanzar, el informe sigue
    pos.threading.Thread = hilo_que_falla
    avisos_pos.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        salida = graph.plataforma_node({"problem_case": LANA})
    assert salida["problem_case"][utils.PLATAFORMA_OBJETIVO]["value"] == "Microsoft Azure"
    assert any(a.startswith("[posicionamiento] No se ha podido lanzar") for a in avisos_pos), avisos_pos
finally:
    os.environ["AIDM_SIN_POSICIONAMIENTO"] = "1"
    gateway.completar, pos.threading.Thread, graph.elegir_plataforma = completar_real, hilo_real, elegir_real
    logging.getLogger("aidm").removeHandler(recoger_pos)
print("OK 14: el cuadrante pasa por las guardias, su prompt cabe en Groq, sin JSON no se guarda nada, una ejecución "
      "vieja no escribe y, en los tests, plataforma_node no lanza el hilo")

print("\nTodo bien: el fallo 67 funciona (en la parte que se puede comprobar sin el LLM real).")
