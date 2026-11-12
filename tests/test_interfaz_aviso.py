"""Comprueba el fallo 72: lo que decide la plataforma sale en el chat antes del informe, y no se puede escribir mientras.

Cuando el discovery termina, el agente de plataforma decide la nube y, antes de que empiecen los agentes del informe
(unos minutos), el chat enseña un mensaje informativo con lo que ha decidido. Mientras AIDM trabaja, la caja de
mensajes está desactivada: así nadie corta la ejecución escribiendo (fallo 48), y el mensaje se puede pintar a mitad.

Ejecuta interface.py sin navegador con el AppTest de Streamlit y el grafo con sus agentes simulados: no llama a ningún
LLM ni toca tu caso ni tu informe.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_interfaz_aviso.py

Los avisos "missing ScriptRunContext" que salgan son normales con AppTest.
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto
os.environ["AIDM_DECISION_URL"] = ""  # sin el modelo de decisión, aunque esté en tu .env

import contextlib
import io
import json
import tempfile
import types

import streamlit as st
from streamlit.runtime.scriptrunner_utils.exceptions import StopException
from streamlit.testing.v1 import AppTest

import utils
from agents import plataforma

PLANTILLA = json.loads(Path("data_structures/problem_case_recovery.json").read_text(encoding="utf-8"))
CONFIG = plataforma._config()


def caso(**campos) -> dict:
    nuevo = json.loads(json.dumps(PLANTILLA))
    for campo, valor in campos.items():
        nuevo[campo]["value"] = valor
    nuevo["status"] = "completed"
    return nuevo


AGRO = caso(cloud_provider="Azure como prioridad, con un módulo actual en AWS (EC2 y S3 para sensores de temperatura de "
                           "cámaras frigoríficas) que no quieren migrar ni rehacer",
            stack_tecnologico_actual="Microsoft 365 y Excel en toda la empresa; ERP propio")

# 1) El mensaje: plataforma, lo que ya tiene en otra nube, y que el informe sigue; en el idioma del cliente
texto = plataforma.mensaje_chat(plataforma.puntuar(AGRO, CONFIG), CONFIG, AGRO)
assert texto.startswith("Antes de preparar el informe, esto es lo que propongo:"), texto
assert "- **Plataforma:** Microsoft Azure: ya la usáis y es la que mejor encaja." in texto, texto
assert ("- **Lo que tenéis en Amazon Web Services (AWS) (EC2, S3):** lo mantendría y lo integraría con Microsoft "
        "Azure, como nos habéis dicho.") in texto, texto
assert texto.endswith("tardará unos minutos.")
INGLES = caso(cloud_provider="We use IBM Cloud for the old billing system",
              stack_tecnologico_actual="Microsoft 365 and Power BI for all the reporting, and Databricks for the data team",
              tamano_equipo_tecnico="two people in IT")
r = plataforma.puntuar(INGLES, CONFIG)
r["despliegue"] = {"opcion": "hibrido", "confianza": 0.9, "cliente": ""}
texto = plataforma.mensaje_chat(r, CONFIG, INGLES)
assert texto.startswith("Before I prepare the report") and "- **Deployment:** hybrid." in texto, texto
assert "- **Training:** 2 people from your team" in texto and "What you have in IBM Cloud" in texto, texto
print("OK 1: el mensaje dice la plataforma, lo que ya tiene en otra nube, el despliegue y la formación, en su idioma")

# 2) El grafo lo manda al chat al acabar la plataforma, antes de los agentes del informe
import graph

orden = []
carpeta = Path(tempfile.mkdtemp())
utils.PROBLEM_CASE_PATH = carpeta / "problem_case.json"
utils.PROBLEM_CASE_PATH.write_text(json.dumps(AGRO, ensure_ascii=False), encoding="utf-8")
graph.run_agent = lambda prompt, chat_history=None: orden.append("discovery") or "Perfecto, ya tengo todo"
graph.elegir_plataforma = lambda c: orden.append("plataforma") or {
    **c, utils.PLATAFORMA_OBJETIVO: {"value": "Microsoft Azure", "mensaje": "Antes de preparar el informe…"}}
graph.build_structure_proposal = lambda *a: orden.append("estructura") or "estructura"
graph.propose_solution = lambda *a: orden.append("propuesta") or "propuesta"
graph.roadmap_builder = lambda *a: orden.append("roadmap") or "roadmap"
graph.gateway.resumen_consumo = lambda *a, **k: None
with contextlib.redirect_stdout(io.StringIO()):
    graph.run_graph("Ya está todo", [], avisar=lambda t: orden.append(f"chat: {t}"))
assert orden == ["discovery", "plataforma", "chat: Antes de preparar el informe…", "estructura", "propuesta",
                 "roadmap"], orden
assert graph._avisar is None, "al acabar, el grafo no se queda con la función del chat"
orden.clear()


def chat_que_falla(t):
    raise RuntimeError("sin chat")


with contextlib.redirect_stdout(io.StringIO()):
    graph.run_graph("Ya está todo", [], avisar=chat_que_falla)
assert orden[-1] == "roadmap", "si el chat falla, el informe sigue"
print("OK 2: el grafo manda el mensaje al chat entre la plataforma y la estructura; si el chat falla, sigue")

# 3) En la interfaz: mientras trabaja, la caja de mensajes está desactivada y el aviso sale en el chat
grafo = types.ModuleType("graph")
grafo.llamadas = []


def grafo_simulado(prompt, chat_history=None, avisar=None):
    grafo.llamadas.append(prompt)
    avisar("Antes de preparar el informe, esto es lo que propongo: Microsoft Azure.")
    raise StopException()  # corta aquí, a mitad del informe, para ver la página tal como está mientras trabaja


grafo.run_graph = grafo_simulado
sys.modules["graph"] = grafo  # interface.py importará este en lugar del real
at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
at.run()
assert not at.chat_input[0].proto.disabled, "antes de escribir, la caja está activa"
at.chat_input[0].set_value("Ya está todo").run()
assert grafo.llamadas == ["Ya está todo"]
assert at.chat_input[0].proto.disabled, "mientras trabaja, no se puede escribir"
assert at.chat_message[-1].markdown[0].value.startswith("Antes de preparar el informe"), at.chat_message[-1]
print("OK 3: mientras se genera el informe la caja de mensajes está desactivada, y el aviso ya está en el chat")

# 4) Si la ejecución se corta, la siguiente no la vuelve a lanzar y la caja vuelve a estar activa
at.run()
assert grafo.llamadas == ["Ya está todo"], grafo.llamadas
assert not at.chat_input[0].proto.disabled
roles = [m["role"] for m in at.session_state["messages"]]
assert roles[-2:] == ["user", "assistant"], roles
print("OK 4: si algo corta la ejecución, no se relanza y se puede volver a escribir")

# 5) Un turno normal de discovery: la respuesta sale en el chat y la caja queda activa
grafo.run_graph = lambda prompt, chat_history=None, avisar=None: {"assistant_response": "¿Qué volumen manejáis?",
                                                                  "completion_reached": False}
at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
at.run()
at.chat_input[0].set_value("Somos una distribuidora").run()
assert at.chat_message[-1].markdown[0].value == "¿Qué volumen manejáis?"
assert not at.chat_input[0].proto.disabled
print("OK 5: en un turno normal, la respuesta sale en el chat y se puede seguir escribiendo")

print("\nTodo bien: el fallo 72 está arreglado.")
