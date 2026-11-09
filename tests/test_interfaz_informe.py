"""Comprueba el arreglo del fallo 3: el informe se muestra en la página y se descarga.

Ejecuta interface.py sin navegador con el AppTest de Streamlit. El grafo se
sustituye por uno simulado, así que no llama a ningún LLM ni cambia ningún archivo.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_interfaz_informe.py

Los avisos "missing ScriptRunContext" que salgan son normales con AppTest.
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import types
import webbrowser

import streamlit as st
from streamlit.testing.v1 import AppTest

if not hasattr(st, "iframe"):
    sys.exit(f"Tu Streamlit es {st.__version__}; st.iframe necesita 1.56 o superior: pip install -U streamlit")

INFORME = Path("data_structures/solution_builder_report.html")

# Grafo simulado: devuelve lo que devolvería run_graph() en cada caso
grafo = types.ModuleType("graph")
grafo.completar = True
grafo.run_graph = lambda prompt, chat_history=None: {
    "assistant_response": "¿Qué volumen de datos manejáis?",
    "completion_reached": grafo.completar,
    "problem_case": {"nombre_del_cliente": {"value": "Farma Norte, S.L."}},
}
sys.modules["graph"] = grafo  # interface.py importará este en lugar del real

# Si algo intentara abrir el navegador del servidor, quedaría registrado aquí
abiertos = []
webbrowser.open = lambda *args, **kwargs: abiertos.append(args)

# 1) Un turno normal de discovery: respuesta en el chat, sin informe
grafo.completar = False
at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
at.run()
at.chat_input[0].set_value("Somos una distribuidora farmacéutica").run()
assert not at.exception, at.exception
assert at.chat_message[-1].markdown[0].value == "¿Qué volumen de datos manejáis?"
assert not at.get("iframe") and not at.get("download_button"), "aún no debería haber informe"
print("OK 1: un turno normal responde en el chat y no muestra informe")

# 2) El turno que completa el discovery: informe en la página y botón de descarga
grafo.completar = True
at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
at.run()
at.chat_input[0].set_value("Ya está todo").run()
assert not at.exception, at.exception
assert abiertos == [], "no debe abrir el navegador del servidor"
assert "Aquí tienes la propuesta" in at.success[0].value

boton = at.get("download_button")
assert len(boton) == 1, "falta el botón de descarga"
assert "Descargar informe" in boton[0].proto.label

iframe = at.get("iframe")
assert len(iframe) == 1, "falta la vista previa del informe"
assert iframe[0].proto.src.startswith("data:text/html;base64,"), "el informe debe ir como data: URL"

assert at.session_state["report_html"] == INFORME.read_text(encoding="utf-8")
nombre = at.session_state["report_filename"]
assert nombre.startswith("informe_aidm_Farma_Norte_S_L_") and nombre.endswith(".html"), nombre
print(f"OK 2: el informe se ve en la página y se descarga como {nombre}")

# 3) Al recargar (por ejemplo, tras pulsar el botón) el informe sigue ahí
at.run()
assert len(at.get("iframe")) == 1 and len(at.get("download_button")) == 1
print("OK 3: el informe sigue visible al recargar la página")

print("\nTodo bien: el fallo 3 está arreglado.")