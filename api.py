"""API del frontend de AIDM.

Arranque, desde la carpeta AIDM (donde están graph.py e interface.py):
    pip install fastapi uvicorn
    python api.py
Con log:  python api.py > ejecucion_grabada_2026-11-12.log 2>&1
"""
import json
import re
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Igual que en interface.py: con la salida a un archivo en Windows, un print raro no debe tumbar el turno.
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

from graph import run_graph  # los módulos pesados se cargan una sola vez, al arrancar el servidor

DATA = Path("data_structures")
REPORT_PATH = DATA / "solution_builder_report.html"

app = FastAPI(title="AIDM Discovery Agent")
lock = threading.Lock()  # problem_case.json es un único archivo: una ejecución a la vez


class ChatIn(BaseModel):
    prompt: str
    history: list[dict[str, str]]  # incluye ya el mensaje nuevo del usuario, como en Streamlit


@app.get("/")
def home():
    return FileResponse("index.html")


@app.post("/api/chat")
def chat(body: ChatIn):
    with lock:
        try:
            raw = run_graph(body.prompt, chat_history=body.history)
        except Exception as e:
            return {"error": str(e)}

        res = raw if isinstance(raw, dict) else {"assistant_response": str(raw)}
        out = {
            "reply": res.get("assistant_response", ""),
            "completed": bool(res.get("completion_reached")),
        }
        if out["completed"]:
            cliente = res.get("problem_case", {}).get("nombre_del_cliente", {}).get("value", "")
            cliente = re.sub(r"[^\w-]+", "_", cliente).strip("_") or "cliente"
            out["report_html"] = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
            out["filename"] = f"informe_aidm_{cliente}_{datetime.now():%Y%m%d_%H%M}.html"
        return out


@app.post("/api/reset")
def reset():
    """Nueva sesión: copia la plantilla en blanco encima de problem_case.json."""
    with lock:
        shutil.copyfile(DATA / "problem_case_recovery.json", DATA / "problem_case.json")
    return {"ok": True}


@app.get("/api/caso")
def caso():
    """Estado actual de la entrevista, para la ficha del proyecto."""
    ruta = DATA / "problem_case.json"
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


if __name__ == "__main__":
    import webbrowser

    import uvicorn

    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()
    uvicorn.run(app, port=8000)