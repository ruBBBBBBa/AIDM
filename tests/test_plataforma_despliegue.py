"""Comprueba el fallo 70: el modelo de decisión recomienda el modelo de despliegue (nube, híbrido u on-premises).

Según el jefe, el modelo de despliegue es decisión de Inetum con la infraestructura que cuenta el cliente, no una
preferencia suya. El agente de plataforma pregunta al modelo de decisión con los datos del caso; si está seguro, la
sección «Plataforma Recomendada» lo dice, se guarda en la plataforma objetivo y lo leen todos los agentes. Si dice
«sin_datos», no está seguro o no responde, todo sigue como antes.

No llama a ningún LLM ni al modelo de decisión (se simulan) y no toca tu caso ni tu informe.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_plataforma_despliegue.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import contextlib
import io
import json
import shutil
import tempfile

import litellm

import decisiones
import utils
from agents import plataforma
from agents import estimation_crew as ec

PLANTILLA = json.loads(Path("data_structures/problem_case_recovery.json").read_text(encoding="utf-8"))
carpeta = Path(tempfile.mkdtemp())
plataforma.REPORT_PATH = carpeta / "informe.html"
utils.PROBLEM_CASE_PATH = carpeta / "problem_case.json"
litellm.completion = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sin LLM en el test"))  # sin explicación


def caso(**campos) -> dict:
    nuevo = json.loads(json.dumps(PLANTILLA))
    for campo, valor in campos.items():
        nuevo[campo]["value"] = valor
    nuevo["status"] = "completed"
    return nuevo


def simular(opcion, confianza=0.9):
    llamadas = []

    def decidir(estado, preguntas):
        llamadas.append((estado, preguntas))
        if opcion is None:
            return None
        if "despliegue" in preguntas:
            return {"despliegue": {"choice": opcion, "confidence": confianza}}
        return {clave: {"choice": "la_usa", "confidence": 0.9} for clave in preguntas}
    decisiones.decidir = decidir
    return llamadas


def elegir(c: dict) -> tuple[dict, str]:
    shutil.copy("data_structures/solution_builder_report_recovery.html", plataforma.REPORT_PATH)
    with contextlib.redirect_stdout(io.StringIO()):
        guardado = plataforma.elegir_plataforma(c)
    return guardado, plataforma.REPORT_PATH.read_text(encoding="utf-8")


PLANTA = caso(
    modelo_de_despliegue="Preferimos todo en la nube",
    cloud_provider="Azure",
    stack_tecnologico_actual="Microsoft 365; los SCADA de la planta no pueden ir a la nube",
    sector="Fabricación de piezas de automoción",
    evidencias=["Las máquinas necesitan respuesta en milisegundos", "IT son 6 personas"],
)

# 1) Lo que lee el modelo: los campos de CAMPOS_DESPLIEGUE con dato, en su orden, y la pregunta de despliegue
llamadas = simular("hibrido")
guardado, html = elegir(PLANTA)
estado, preguntas = [ll for ll in llamadas if "despliegue" in ll[1]][0]
assert preguntas == {"despliegue": decisiones.PREGUNTA_DESPLIEGUE}
assert estado == decisiones.texto_despliegue({
    "modelo_de_despliegue": "Preferimos todo en la nube", "cloud_provider": "Azure",
    "stack_tecnologico_actual": "Microsoft 365; los SCADA de la planta no pueden ir a la nube",
    "sector": "Fabricación de piezas de automoción",
    "evidencias": "Las máquinas necesitan respuesta en milisegundos | IT son 6 personas"}), estado
print("OK 1: el modelo lee los campos de despliegue con dato, en orden, con las evidencias juntas")

# 2) Híbrido: la sección lo dice con la oferta local de la nube y lo que pidió el cliente; los agentes lo leen
assert "Modelo de despliegue recomendado:</strong> Híbrido" in html, html[:3000]
assert "Azure Local y Azure Arc" in html and "El cliente dijo: «Preferimos todo en la nube»" in html
assert guardado[utils.PLATAFORMA_OBJETIVO]["despliegue"] == "hibrido"
contexto = utils.contexto_cliente(guardado)
assert "MODELO DE DESPLIEGUE: híbrido, por decisión de Inetum" in contexto and "PLATAFORMA OBJETIVO: Microsoft Azure" in contexto
print("OK 2: híbrido: la sección lo explica (Azure Local y Azure Arc, y lo que dijo el cliente) y llega a todos los agentes")

# 3) On-premises: los agentes diseñan para sus servidores y la tabla de precios de la nube no se usa
DEFENSA = caso(cloud_provider="No usamos ninguna nube",
               regulacion_y_compliance="Por contrato con Defensa, ningún dato puede salir de nuestras instalaciones",
               stack_tecnologico_actual="SQL Server y Power BI Report Server en nuestro CPD")
simular("on_premises", 0.86)
guardado, html = elegir(DEFENSA)
assert guardado[utils.PLATAFORMA_OBJETIVO]["despliegue"] == "on_premises"
assert "recomendado:</strong> On-premises (en sus servidores): toda la solución en sus servidores" in html, html[:3000]
contexto = utils.contexto_cliente(guardado)
assert "PLATAFORMA OBJETIVO: los servidores del cliente (on-premises), por decisión de Inetum" in contexto, contexto[:600]
assert ec._tabla_precios(guardado, "Azure Data Lake Storage (ADLS) y Azure Data Factory") == ""
print("OK 3: on-premises: los agentes diseñan para sus servidores y no se pasa la tabla de precios de la nube")

# 4) «sin_datos», poca confianza o sin el modelo: nada cambia respecto al fallo 67
for opcion, confianza in (("sin_datos", 0.95), ("hibrido", 0.6), (None, 0.0)):
    simular(opcion, confianza)
    guardado, html = elegir(PLANTA)
    assert "despliegue" not in guardado[utils.PLATAFORMA_OBJETIVO], (opcion, confianza)
    assert "Modelo de despliegue recomendado" not in html and "MODELO DE DESPLIEGUE" not in utils.contexto_cliente(guardado)
    assert ec._tabla_precios(guardado, "Azure Data Lake Storage (ADLS) y Azure Data Factory") != ""
print("OK 4: con «sin_datos», con confianza 0,6 o sin el modelo, la sección y los agentes quedan como antes")

# 5) Nube aunque el cliente pida sus servidores: manda la infraestructura, y la sección enseña lo que dijo
simular("cloud")
guardado, html = elegir(caso(modelo_de_despliegue="En local", stack_tecnologico_actual="Todo es SaaS: Microsoft 365"))
assert "recomendado:</strong> En la nube: toda la solución en Microsoft Azure. El cliente dijo: «En local»." in html, html[:3000]
assert "MODELO DE DESPLIEGUE" not in utils.contexto_cliente(guardado)
print("OK 5: si recomienda la nube aunque el cliente dijera «En local», la sección lo dice y enseña lo que pidió")

# 6) Cada nube tiene su oferta para el centro de datos del cliente en el yaml
assert all(p.get("hibrido") for p in plataforma._config()["proveedores"].values())
print("OK 6: las cinco nubes tienen su oferta híbrida en hiperescalares.yaml")

print("\nTodo bien: el fallo 70 está arreglado.")
