"""Comprueba el arreglo del fallo 48: un mensaje escrito mientras se genera el informe no lo vuelve a generar.

En la ejecución del 27 de octubre, el usuario escribió «como vamos?» mientras se generaba el informe. Streamlit
cortó el script al cerrar el spinner (antes de marcar el informe como hecho) y lo relanzó con ese mensaje; como el
caso ya estaba completo, se generó el informe entero otra vez.

Ejecuta interface.py sin navegador con el AppTest de Streamlit. El grafo se sustituye por uno simulado, así que
no llama a ningún LLM ni cambia ningún archivo. El corte se simula con un spinner que, al cerrarse, para el
script como lo haría Streamlit al recibir el mensaje nuevo.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_interfaz_sin_relanzar.py

Los avisos "missing ScriptRunContext" que salgan son normales con AppTest.
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import contextlib
import types

import streamlit as st
from streamlit.runtime.scriptrunner_utils.exceptions import StopException
from streamlit.testing.v1 import AppTest

# Grafo simulado: cuenta cuántas veces se genera el informe
grafo = types.ModuleType("graph")
grafo.llamadas = []
grafo.run_graph = lambda prompt, chat_history=None: grafo.llamadas.append(prompt) or {
    "assistant_response": "Informe generado",
    "completion_reached": True,
    "problem_case": {"nombre_del_cliente": {"value": "Iberdrola"}},
}
sys.modules["graph"] = grafo  # interface.py importará este en lugar del real

# Spinner que, al cerrarse, corta el script: lo que hace Streamlit cuando llega un mensaje durante la ejecución
spinner_original = st.spinner


@contextlib.contextmanager
def spinner_cortado(*args, **kwargs):
    yield
    raise StopException()


# 1) El turno que genera el informe se corta al cerrar el spinner
st.spinner = spinner_cortado
at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
at.run()
at.chat_input[0].set_value("Discovery completado").run()
assert grafo.llamadas == ["Discovery completado"]
assert at.session_state["solution_completed"], "el informe debe quedar marcado como hecho antes del corte"
assert at.session_state["report_filename"].startswith("informe_aidm_Iberdrola_")
print("OK 1: el informe queda marcado como hecho aunque Streamlit corte el script al cerrar el spinner")

# 2) La ejecución siguiente (la del mensaje nuevo) muestra el informe y no vuelve a llamar al grafo
st.spinner = spinner_original
at.run()
assert grafo.llamadas == ["Discovery completado"], f"se volvió a generar el informe: {grafo.llamadas}"
assert len(at.get("iframe")) == 1 and len(at.get("download_button")) == 1
assert not at.chat_input, "con el informe hecho no se pueden mandar más mensajes en esta sesión"
assert "no vuelven a generar el informe" in at.info[0].value
print("OK 2: el mensaje siguiente no relanza nada: se ve el informe y no hay caja de mensajes")

print("\nTodo bien: el fallo 48 está arreglado.")