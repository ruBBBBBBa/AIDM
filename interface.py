import base64
import re
from datetime import datetime
from pathlib import Path

import sys

import streamlit as st

from graph import run_graph

REPORT_PATH = Path("data_structures/solution_builder_report.html")

# En Windows, si la salida va a un archivo, Python escribe en cp1252 y un print con "≥" o un emoji
# lanza un error que tumba el turno. Con UTF-8 (y "replace" por si acaso) un print nunca rompe nada.
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")
        
# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AIDM - Discovery Agent",
    page_icon="🧠",
    layout="centered",
)

# ── Session state ──────────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Describe la estructura y los requisitos de tu proyecto de DATA & AI 👇"}
    ]

if "solution_completed" not in st.session_state:
    st.session_state.solution_completed = False

def guardar_informe(response: dict) -> None:
    """Guarda el informe en la sesión y da la fase de solución por hecha.

    Fallo 48: se llama nada más volver run_graph, antes de cualquier otra llamada a Streamlit. Si el usuario
    escribe mientras se genera el informe, Streamlit corta el script en la siguiente llamada (al cerrar el
    spinner) y lo vuelve a lanzar con el mensaje nuevo; sin la marca, ese mensaje generaba el informe otra vez.
    """
    # webbrowser.open() lo abría en el ordenador donde corre Streamlit, no en el del usuario.
    # Se guarda una copia en la sesión para que siga visible aunque otra ejecución sobrescriba el archivo.
    cliente = response.get("problem_case", {}).get("nombre_del_cliente", {}).get("value", "")
    cliente = re.sub(r"[^\w-]+", "_", cliente).strip("_") or "cliente"
    st.session_state.report_html = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
    st.session_state.report_filename = f"informe_aidm_{cliente}_{datetime.now():%Y%m%d_%H%M}.html"
    st.session_state.solution_completed = True

def mostrar_informe() -> None:
    """Muestra el informe guardado en la sesión: botón de descarga y vista previa."""
    report_html = st.session_state.get("report_html", "")
    if not report_html:
        st.warning("No se ha encontrado el informe generado.")
        return

    st.download_button(
        "⬇️ Descargar informe (HTML)",
        data=report_html,
        file_name=st.session_state.report_filename,
        mime="text/html",
    )
    # El informe lleva texto generado por el LLM: se carga como data: URL para que el iframe
    # tenga su propio origen y su JavaScript no pueda acceder a la app.
    data_url = "data:text/html;base64," + base64.b64encode(report_html.encode("utf-8")).decode("ascii")
    st.iframe(data_url, height=900)

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown("### 🧠 AIDM Discovery Agent")
st.caption("Primera PoC - Demo conversacional para captura de requisitos de proyectos DATA & AI.")
st.divider()

# ── Chat history ───────────────────────────────────────────────────────────────
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# ── Blocked state ──────────────────────────────────────────────────────────────
if st.session_state.solution_completed:
    st.success("✅ Discovery completado. Aquí tienes la propuesta de solución.")
    mostrar_informe()
    st.info("Inicia una nueva sesión para comenzar un nuevo discovery. Los mensajes que escribas aquí no vuelven a generar el informe.")
    st.stop()

# ── User input ─────────────────────────────────────────────────────────────────
prompt = st.chat_input("Describe tu proyecto de DATA & AI")

if not prompt:
    st.stop()

# ── User message ───────────────────────────────────────────────────────────────
st.session_state.messages.append({"role": "user", "content": prompt})
with st.chat_message("user"):
    st.markdown(prompt)
    print(f"\n\n1. -----User prompt: {prompt}-----\n\n")

# ── Agent execution ────────────────────────────────────────────────────────────
# El spinner cubre toda la ejecución del grafo. Si este turno completa el
# discovery y lanza los 6 agentes, el usuario ve el spinner durante todo
# ese tiempo. Si es un turno normal de discovery, el spinner desaparece
# rápido y se muestra la respuesta en el chat.
response: dict = {}
assistant_response = ""

with st.spinner("Procesando..."):
    try:
        raw_response = run_graph(prompt, chat_history=st.session_state.messages)
        response = raw_response if isinstance(raw_response, dict) else {"assistant_response": str(raw_response)}
        assistant_response = response.get("assistant_response", "")
        if response.get("completion_reached", False):
            guardar_informe(response)  # fallo 48: antes de salir del spinner
    except Exception as e:
        assistant_response = (
            "⚠️ No se pudo ejecutar el agente. "
            "Revisa la configuración del modelo (MODEL y API KEY) en `.env`.\n\n"
            f"**Detalle:** {e}"
        )

completion_reached = response.get("completion_reached", False)

# ── Turno normal de discovery: mostrar respuesta en el chat ───────────────────
if not completion_reached:
    with st.chat_message("assistant"):
        st.markdown(assistant_response)
    st.session_state.messages.append({"role": "assistant", "content": assistant_response})
    st.stop()

# ── Fase de solución: el informe ya está guardado en la sesión (guardar_informe); se muestra en la página ─
st.rerun()  # vuelve a pintar la página, ya en el estado "completado", que muestra el informe