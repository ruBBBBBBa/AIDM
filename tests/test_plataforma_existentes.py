"""Comprueba el fallo 71: lo que el cliente ya tiene en otra nube se mantiene, salvo que pida migrarlo o que compense.

La solución nueva va a la nube recomendada. Lo que el cliente ya tiene en otra (un módulo en AWS, un Redshift…) se
mantiene e integra, salvo que el cliente pida migrarlo o que migrar compense mucho: la nube recomendada le saca al
menos existente.ventaja_minima puntos y el ahorro del horizonte es al menos existente.ahorro_sobre_coste veces lo que
cuesta migrar. Lo que dice el cliente se busca por palabras en la frase donde nombra esa nube.

No llama a ningún LLM ni al modelo de decisión y no toca tu caso ni tu informe.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_plataforma_existentes.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto
os.environ["AIDM_DECISION_URL"] = ""  # sin el modelo de decisión, aunque esté en tu .env

import contextlib
import copy
import io
import json
import shutil
import tempfile

import litellm

import utils
from agents import plataforma

PLANTILLA = json.loads(Path("data_structures/problem_case_recovery.json").read_text(encoding="utf-8"))
CONFIG = plataforma._config()
E = CONFIG["existente"]


def caso(**campos) -> dict:
    nuevo = json.loads(json.dumps(PLANTILLA))
    for campo, valor in campos.items():
        nuevo[campo]["value"] = valor
    nuevo["status"] = "completed"
    return nuevo


def con(**cambios) -> dict:
    """La configuración con otros valores en existente."""
    config = copy.deepcopy(CONFIG)
    config["existente"].update(cambios)
    return config


MICROSOFT = "Microsoft 365 y Excel en toda la empresa; ERP propio"
AGRO = caso(cloud_provider="Azure como prioridad, con un módulo actual en AWS (EC2 y S3 para sensores de temperatura de "
                           "cámaras frigoríficas) que no quieren migrar ni rehacer", stack_tecnologico_actual=MICROSOFT)
REDSHIFT = caso(cloud_provider="Azure para casi todo y un Redshift en AWS para el histórico",
                stack_tecnologico_actual=MICROSOFT)
horas_servicio, tarifa = E["horas_migracion_por_servicio"], 30  # Data Engineer, Middle en bd/costes_perfil.csv
ahorro = E["horas_operar_otra_nube_mes"] * 12 * E["horizonte_anios"] * CONFIG["formacion"]["coste_hora_eur"]

# 1) El cliente del 9 nov: el módulo de AWS se mantiene porque «no quieren migrar», con sus dos servicios
r = plataforma.puntuar(AGRO, CONFIG)
(x,) = r["existentes"]
assert (x["clave"], x["servicios"], x["decision"], x["lo_pide_el_cliente"]) == ("aws", ["EC2", "S3"], "mantener", True), x
assert x["tarifa"] == tarifa and x["coste_migracion_eur"] == 2 * horas_servicio * tarifa and x["ahorro_eur"] == ahorro, x
texto = plataforma.resumen(r, CONFIG)
assert ("- **Amazon Web Services (AWS)** (EC2, S3): se mantiene e integra con Microsoft Azure, porque el cliente no "
        "quiere migrarlo.") in texto, texto
print(f"OK 1: el módulo de AWS (EC2 y S3) del 9 nov se mantiene porque el cliente no quiere migrarlo "
      f"(migrar: {x['coste_migracion_eur']} €; ahorro en {x['anios']} años: {x['ahorro_eur']} €)")

# 2) Sin que el cliente diga nada: se valora, y con poca ventaja se mantiene, con las cuentas y el porqué
r = plataforma.puntuar(REDSHIFT, CONFIG)
(x,) = r["existentes"]
assert x["servicios"] == ["Redshift"] and not x["lo_pide_el_cliente"] and x["decision"] == "mantener", x
assert x["ventaja"] < E["ventaja_minima"], x
texto = plataforma.resumen(r, CONFIG)
assert (f"- **Amazon Web Services (AWS)** (Redshift): se mantiene e integra con Microsoft Azure. Migrarlo costaría unos "
        f"{plataforma._miles(horas_servicio * tarifa)} € ({horas_servicio} h a {tarifa} €/h)") in texto, texto
assert f"pero Microsoft Azure solo le saca {plataforma._numero(x['ventaja'])} puntos (hacen falta 25" in texto, texto
print(f"OK 2: un Redshift en AWS sin que el cliente diga nada se mantiene: Azure solo le saca {x['ventaja']} puntos")

# 3) Con mucha ventaja y un ahorro que paga la migración, se migra; si el ahorro no llega, se mantiene
(x,) = plataforma.puntuar(REDSHIFT, con(ventaja_minima=5))["existentes"]
assert x["decision"] == "migrar" and x["compensa"] and not x["lo_pide_el_cliente"], x
assert "el ahorro paga con creces la migración" in plataforma.resumen(plataforma.puntuar(REDSHIFT, con(ventaja_minima=5)),
                                                                     con(ventaja_minima=5))
(x,) = plataforma.puntuar(REDSHIFT, con(ventaja_minima=5, horas_operar_otra_nube_mes=1))["existentes"]
assert x["decision"] == "mantener" and not x["compensa"], x
assert "el ahorro no llega a 1,5 veces lo que cuesta migrar" in plataforma.resumen(
    plataforma.puntuar(REDSHIFT, con(ventaja_minima=5, horas_operar_otra_nube_mes=1)),
    con(ventaja_minima=5, horas_operar_otra_nube_mes=1))
print("OK 3: con ventaja suficiente y un ahorro de 1,5 veces la migración, se migra; si el ahorro no llega, se mantiene")

# 4) Si el cliente pide migrarlo, se migra aunque no compense; si pide mantenerlo y compensa, se avisa
PIDE = caso(cloud_provider="Azure, y un Redshift en AWS que queremos pasarlo a Azure", stack_tecnologico_actual=MICROSOFT)
(x,) = plataforma.puntuar(PIDE, CONFIG)["existentes"]
assert (x["decision"], x["lo_pide_el_cliente"], x["compensa"]) == ("migrar", True, False), x
r = plataforma.puntuar(AGRO, con(ventaja_minima=5))
assert r["existentes"][0]["compensa"] and r["existentes"][0]["decision"] == "mantener"
assert "Migrar compensaría (migrarlo costaría" in plataforma.resumen(r, con(ventaja_minima=5))
assert "comentarlo con el cliente" in plataforma.resumen(r, con(ventaja_minima=5))
print("OK 4: «queremos pasarlo a Azure» migra; si dice que no lo migra y compensaría, se mantiene y se avisa")

# 5) Lo leen todos los agentes: «SE MANTIENE» o «SE MIGRA A», y el caso lo guarda
carpeta = Path(tempfile.mkdtemp())
plataforma.REPORT_PATH = carpeta / "informe.html"
utils.PROBLEM_CASE_PATH = carpeta / "problem_case.json"
litellm.completion = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin LLM en el test"))
for c, esperado in ((AGRO, "SE MANTIENE: lo que el cliente ya tiene en Amazon Web Services (AWS) (EC2, S3). No lo migres"),
                    (PIDE, "SE MIGRA A Microsoft Azure: lo que el cliente tiene en Amazon Web Services (AWS) (Redshift)")):
    shutil.copy("data_structures/solution_builder_report_recovery.html", plataforma.REPORT_PATH)
    with contextlib.redirect_stdout(io.StringIO()):
        guardado = plataforma.elegir_plataforma(c)
    assert guardado[utils.PLATAFORMA_OBJETIVO]["existentes"][0]["nube"] == "Amazon Web Services (AWS)"
    assert esperado in utils.contexto_cliente(guardado), utils.contexto_cliente(guardado)[:900]
print("OK 5: el contexto de todos los agentes dice SE MANTIENE (integrar y presupuestar la integración) o SE MIGRA A")

# 6) Al cambiar de nube, lo que ya tiene en la de antes se mantiene: la solución nueva va a la nueva
CAMBIO = caso(cloud_provider="IBM Cloud", tamano_equipo_tecnico="dos técnicos de sistemas",
              stack_tecnologico_actual="Microsoft 365, Power BI y Databricks para los cuadros de mando")
shutil.copy("data_structures/solution_builder_report_recovery.html", plataforma.REPORT_PATH)
with contextlib.redirect_stdout(io.StringIO()):
    guardado = plataforma.elegir_plataforma(CAMBIO)
objetivo = guardado[utils.PLATAFORMA_OBJETIVO]
assert objetivo["decision"] == "cambiar" and objetivo["existentes"][0]["decision"] == "mantener", objetivo
assert "lo que ya tiene allí se mantiene e integra" in objetivo["motivo"], objetivo["motivo"]
print("OK 6: al cambiar de IBM Cloud a Azure, lo de IBM se mantiene y la solución nueva va a Azure")

# 7) La tarifa sale de la tabla de los costes; sin la tabla, la del yaml
assert plataforma._tarifa_migracion(E) == tarifa
original, plataforma.TARIFAS = plataforma.TARIFAS, carpeta / "no_existe.csv"
assert plataforma._tarifa_migracion({**E, "coste_hora_migracion_eur": 44}) == 44
plataforma.TARIFAS = original
print("OK 7: la tarifa de migración sale de bd/costes_perfil.csv (Data Engineer, Middle); sin el CSV, del yaml")

print("\nTodo bien: el fallo 71 está arreglado.")
