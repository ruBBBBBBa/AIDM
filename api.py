"""API del frontend de AIDM.

Arranque, desde la carpeta AIDM (donde están graph.py e interface.py):
    pip install fastapi uvicorn
    python api.py
Con log:  python api.py > ejecucion_grabada_2026-11-12.log 2>&1
"""
import inspect
import json
import re
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Igual que en interface.py: con la salida a un archivo en Windows, un print raro no debe tumbar el turno.
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

from graph import run_graph  # los módulos pesados se cargan una sola vez, al arrancar el servidor
from utils import fuentes_locales  # fallo 66: el informe sin enlaces a Google Fonts

DATA = Path("data_structures")
REPORT_PATH = DATA / "solution_builder_report.html"

app = FastAPI(title="AIDM Discovery Agent")
lock = threading.Lock()  # problem_case.json es un único archivo: una ejecución a la vez

# ---- Estado compartido con el frontend (GET /api/estado) ----
_estado_lock = threading.Lock()
_n_avisos = 0  # contador que no se reinicia: el frontend se queda con el último id que ya pintó
estado = {"ocupado": False, "fraccion": 0.0, "texto": "", "avisos": [], "completado": False}
informe = None  # {"report_html": ..., "filename": ...} cuando el caso está completo y el informe hecho (fallo 48)


def pintar_progreso(fraccion, texto=""):
    """Fallo 94: guarda el avance real (0..1) y su texto para que la web lo lea."""
    with _estado_lock:
        estado["fraccion"] = max(0.0, min(1.0, float(fraccion)))
        estado["texto"] = str(texto or "")


def avisar(texto):
    """Fallo 72: aviso de la plataforma. Va al estado, nunca al historial que leen los agentes."""
    global _n_avisos
    with _estado_lock:
        _n_avisos += 1
        estado["avisos"] = (estado["avisos"] + [{"id": _n_avisos, "texto": str(texto)}])[-20:]


def _extras_graph():
    """Pasa a run_graph solo los parámetros que acepta, para no romper si el nombre cambia."""
    params = inspect.signature(run_graph).parameters
    extras = {}
    if "avisar" in params:
        extras["avisar"] = avisar
    for nombre in ("pintar_progreso", "progreso", "on_progress"):
        if nombre in params:
            extras[nombre] = pintar_progreso
            break
    return extras


def _leer_informe():
    html = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
    return fuentes_locales(html) if html else ""


class ChatIn(BaseModel):
    prompt: str
    history: list[dict[str, str]]  # incluye ya el mensaje nuevo del usuario, como en Streamlit


@app.get("/")
def home():
    return FileResponse("index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/chat")
def chat(body: ChatIn):
    global informe
    with lock:
        # Fallo 48: con el informe ya hecho, una segunda petición (otra pestaña, o en cola) lo devuelve sin llamar a run_graph.
        if informe is not None:
            return {"reply": "", "completed": True, **informe}

        with _estado_lock:
            estado.update(ocupado=True, fraccion=0.0, texto="")
        try:
            raw = run_graph(body.prompt, chat_history=body.history, **_extras_graph())
        except Exception as e:
            return {"error": str(e)}
        finally:
            with _estado_lock:
                estado["ocupado"] = False

        res = raw if isinstance(raw, dict) else {"assistant_response": str(raw)}
        out = {
            "reply": res.get("assistant_response", ""),
            "completed": bool(res.get("completion_reached")),
        }
        if out["completed"]:
            cliente = res.get("problem_case", {}).get("nombre_del_cliente", {}).get("value", "")
            cliente = re.sub(r"[^\w-]+", "_", cliente).strip("_") or "cliente"
            informe = {
                "report_html": _leer_informe(),
                "filename": f"informe_aidm_{cliente}_{datetime.now():%Y%m%d_%H%M}.html",
            }
            with _estado_lock:
                estado.update(completado=True, fraccion=1.0)
            out.update(informe)
        return out


@app.post("/api/reset")
def reset():
    """Nueva sesión (solo el botón): copia la plantilla en blanco encima de problem_case.json.
    Si hay una ejecución en curso no espera ni borra nada: devuelve 409."""
    global informe
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "Hay una ejecución en curso. Espera a que termine."}, status_code=409)
    try:
        shutil.copyfile(DATA / "problem_case_recovery.json", DATA / "problem_case.json")
        informe = None
        with _estado_lock:
            estado.update(ocupado=False, fraccion=0.0, texto="", avisos=[], completado=False)
    finally:
        lock.release()
    return {"ok": True}


@app.get("/api/caso")
def caso():
    """Estado actual de la entrevista, para la ficha del proyecto."""
    ruta = DATA / "problem_case.json"
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


@app.get("/api/estado")
def get_estado():
    """Progreso real, avisos de la plataforma y si el informe ya está hecho. Se sondea cada 3 s."""
    with _estado_lock:
        return {**estado, "avisos": list(estado["avisos"])}


@app.get("/api/informe")
def get_informe():
    """El informe ya generado, para recuperarlo tras recargar la página."""
    if informe is None:
        return JSONResponse({"error": "Todavía no hay informe."}, status_code=404)
    return informe


if __name__ == "__main__":
    import webbrowser

    import uvicorn

    if not _extras_graph():
        print("AVISO: run_graph no acepta 'avisar' ni un parámetro de progreso; la barra y los avisos no se alimentarán.")
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()
    uvicorn.run(app, port=8000)
