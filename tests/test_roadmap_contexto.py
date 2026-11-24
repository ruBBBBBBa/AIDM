"""Comprueba el arreglo del fallo 4 (el roadmap recibe el caso) y el 73: las 7 fases van a la vez, ninguna lee a
otra y todas llevan la arquitectura de la propuesta como referencia común.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt
de cada tarea y devuelve una salida simulada. Tampoco toca tu informe: el roadmap se
inyecta en una copia temporal.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_roadmap_contexto.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import hashlib
import shutil
import tempfile
from types import SimpleNamespace

# Modelos de relleno solo para poder crear los agentes; no se llega a llamar a ninguno.
for clave in ("BIG_MODEL", "REASONING_MODEL", "REASONING_RIGOROUS_MODEL", "SMALL_REASONING_MODEL"):
    os.environ.setdefault(clave, "groq/openai/gpt-oss-20b")

from crewai import Crew

import agents.roadmap_builder as rb
import graph

INFORME = Path("data_structures/solution_builder_report.html")
huella_antes = hashlib.md5(INFORME.read_bytes()).hexdigest()

copia = Path(tempfile.mkdtemp()) / "informe.html"
shutil.copy(INFORME, copia)
rb.REPORT_PATH = copia  # el roadmap se inyecta en la copia, no en tu informe
rb.HILOS = 1  # fallo 73: las fases en orden, para saber qué prompt es de cuál

# Crew.kickoff simulado: guarda el prompt de cada tarea y devuelve "SALIDA n"
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    if self.task_callback is None:  # la crew del resumen
        prompts.append(self.tasks[0].description)
        return SimpleNamespace(raw="## 🧭 RESUMEN SIMULADO 🚀")  # fallo 65: con emojis, como lo escribió Gemini
    for task in self.tasks:
        prompts.append(task.description)
        self.task_callback(SimpleNamespace(raw=f"SALIDA {len(prompts) - 1} ({task.agent.role})"))
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado

caso = {
    "status": "completed",
    "nombre_del_cliente": {"description": "Cliente", "value": "FarmaNorte"},
    "volumen_de_datos": {"description": "Volumen", "value": "2 TB de histórico"},
    "restriccion_presupuestaria": {"description": "Presupuesto", "value": "150.000 a 200.000 euros"},
    "regulacion_y_compliance": {"description": "Regulación", "value": "no mencionado"},
    "evidencias": {"description": "Evidencias", "value": ["Frase larga de la conversación"]},
}

# 1) contexto_cliente (desde el fallo 25 sustituye a caso_compacto): datos reales, sin descripciones ni rellenos
compacto = rb.contexto_cliente(caso)
assert "- volumen_de_datos: 2 TB de histórico" in compacto
assert "no mencionado" not in compacto and "Volumen" not in compacto
assert "- regulacion_y_compliance" in compacto.split("Sin dato")[1], "el relleno debe salir como «Sin dato»"
print("OK 1: el contexto del cliente lleva solo los datos reales; el relleno sale como «Sin dato»")

# 2) Las 7 tareas del roadmap reciben el caso
rb.roadmap_builder("PROPUESTA SIMULADA", problem_case=caso)
assert len(prompts) == 8, f"se esperaban 8 prompts (resumen + 7 tareas) y hay {len(prompts)}"
tareas = prompts[1:]
for n, prompt in enumerate(tareas, start=1):
    assert "DATOS DEL CLIENTE" in prompt and "2 TB de histórico" in prompt, f"la tarea {n} no lleva el caso"
    assert "🧭" not in prompt and "🚀" not in prompt, f"la tarea {n} recibió el resumen con emojis (fallo 65)"
print("OK 2: las 7 tareas del roadmap reciben el resumen, sin emojis, y los datos del cliente")

# 3) Fallo 73: ninguna fase lee lo que escribe otra; todas llevan la arquitectura de la propuesta como referencia
prompts.clear()
rb.roadmap_builder({"architecture": "ARQUITECTURA SIMULADA: lago en tres zonas"}, problem_case=caso)
tareas = prompts[1:]
assert len(tareas) == 7
for n, prompt in enumerate(tareas, start=1):
    assert "ARQUITECTURA SIMULADA: lago en tres zonas" in prompt, f"la fase {n} no lleva la arquitectura"
    assert "CÓMO ENCAJA TU FASE" in prompt, f"la fase {n} no lleva la regla de seguir la propuesta"
    assert "SALIDA" not in prompt and "PRIMERA CREW" not in prompt, f"la fase {n} lee lo que escribió otra"
print("OK 3: las 7 fases son independientes y siguen la misma arquitectura")

# 4) graph.py le pasa el caso al roadmap
recibido = {}
graph.roadmap_builder = lambda propuesta, problem_case=None, fases=None: recibido.update(caso=problem_case,
                                                                                         fases=fases) or ""
FASES_VISTAS = {"roadmap-ml": (False, "sin modelos")}
graph.roadmap_node({"problem_solution": "x", "problem_case": caso, "fases_roadmap": FASES_VISTAS})
assert recibido["caso"] is caso, "roadmap_node no pasa el problem_case"
assert recibido["fases"] is FASES_VISTAS, "roadmap_node no pasa las fases del roadmap (fallo 131)"
print("OK 4: graph.py pasa el caso y las fases del estado al roadmap")

# 5) Fallo 94: la barra de progreso avanza con el resumen y con cada fase al terminar. Las fases van en hilos y solo
#    anotan su avance: la barra la pinta siempre el hilo que la empezó (el de la sesión de Streamlit)
import random
import threading
import time

import progreso

rb.HILOS = 7
pintadas = []


def kickoff_lento(self, *args, **kwargs):
    if self.task_callback is not None:
        time.sleep(random.uniform(0.6, 1.8))  # las fases acaban a destiempo, como de verdad
    return kickoff_simulado(self, *args, **kwargs)


Crew.kickoff = kickoff_lento
progreso.empezar(lambda fraccion, texto: pintadas.append((threading.get_ident(), fraccion, texto)), total=8)
try:
    rb.roadmap_builder({"architecture": "ARQUITECTURA SIMULADA"}, problem_case=caso)
finally:
    progreso.terminar()
    Crew.kickoff = kickoff_simulado
hilos = {hilo for hilo, _, _ in pintadas}
assert hilos == {threading.get_ident()}, "la barra solo se pinta desde el hilo de la sesión"
fracciones = [f for _, f, _ in pintadas]
assert fracciones == sorted(fracciones) and fracciones[-1] == 1.0, fracciones
assert len(pintadas) >= 3, pintadas  # se ha pintado mientras las fases iban acabando, no solo al final
assert pintadas[0][2] == "Resumen de la propuesta · 1 de 8 pasos" and pintadas[-1][2].endswith("· 8 de 8 pasos")
assert any(texto.startswith("Roadmap: ") for _, _, texto in pintadas)
progreso.avanzar("Sin barra")  # sin empezar (tests, scripts), no hace nada
print(f"OK 5: la barra avanza con el resumen y las 7 fases ({len(pintadas)} veces), pintada siempre por la sesión")

# 6) Fallo 88: con el inventario de la arquitectura, el resumen no se llama; las 7 fases reciben el inventario (con la
#    orquestación) y la sección de la propuesta que desarrolla cada una, que es lo que antes les llegaba en el resumen
rb.HILOS = 1
prompts.clear()
propuesta = {"architecture": "ARQUITECTURA SIMULADA", "governance": "GOBERNANZA SIMULADA: retención de 7 años",
             "data_ai_strategy": "ESTRATEGIA SIMULADA: previsión de demanda", "bi_visualization": "BI SIMULADA: 4 cuadros",
             "inventario": [{"servicio": "AWS Step Functions", "capa": "orquestación", "fase": "mvp", "alternativa": ""},
                            {"servicio": "Amazon SageMaker AI", "capa": "IA", "fase": "proyecto_completo",
                             "alternativa": ""}]}
rb.roadmap_builder(propuesta, problem_case=caso)
assert len(prompts) == 7, "sin la llamada al resumen"  # antes, 8: el resumen y las 7 fases
for n, prompt in enumerate(prompts):
    assert "- AWS Step Functions · orquestación · desde el MVP" in prompt and "La orquestación es la del inventario" in prompt
    assert "RESUMEN SIMULADO" not in prompt
fuentes, ingesta, almacenamiento, etl, modelado, ml, bi = prompts
assert "ESTRATEGIA SIMULADA" in fuentes and "ESTRATEGIA SIMULADA" in ml and "BI SIMULADA" in bi and "BI SIMULADA" in modelado
assert "GOBERNANZA SIMULADA" in almacenamiento and "GOBERNANZA SIMULADA" in etl
assert not any(s in ingesta for s in ("ESTRATEGIA SIMULADA", "GOBERNANZA SIMULADA", "BI SIMULADA"))
# Fallo 130: Modelado / Análisis cubre el modelo dimensional y la analítica; Ops, la operación y, si hay modelos, MLOps
assert "modelo dimensional" in modelado and "análisis que pide el cliente" in modelado
assert "monitorización y alertas" in ml and "Si no incluye modelos, no los inventes" in ml and "MLOps" in ml
print("OK 6: con el inventario no se llama al resumen; cada fase recibe el inventario y su sección de la propuesta")

# 7) Fallo 131: la estructura termina con el bloque FASES DEL ROADMAP (fase | sí / no / ya lo tiene | frase); ninguna
#    fase es fija. Sin Modelado / Análisis ni Ops: 5 fases, 5 llamadas, la barra llega al final, el informe dice qué
#    no aplica y los pasos van del 01 al 05. Solo cuadros de mando con el DWH y el ETL en el stack: 1 fase y 4 «Ya
#    implantado» en el flujo, y la propuesta recibe la línea de lo que se reutiliza. «ya lo tiene» sin nada en el stack:
#    la fase se genera y lo dice el log. Todas «no» o «ya lo tiene», o sin bloque: las 7.
import logging

from bs4 import BeautifulSoup

avisos: list[str] = []
recoger = logging.Handler()
recoger.emit = lambda registro: avisos.append(registro.getMessage())
logging.getLogger("aidm").addHandler(recoger)


def bloque(*estados: str) -> str:
    """La estructura simulada con el bloque, una línea por fase: «estado | frase»."""
    return ("1- Introducción\n2- Servicios\n3- Arquitectura\n\n**FASES DEL ROADMAP**\n"
            + "\n".join(f"- {nombre} | {e}" for nombre, e in zip(rb.NOMBRES_FASES, estados)))


def generar(caso_, fases_) -> BeautifulSoup:
    shutil.copy(INFORME, copia)
    prompts.clear()
    pintadas.clear()
    progreso.empezar(lambda fraccion, texto: pintadas.append((threading.get_ident(), fraccion, texto)), total=8)
    try:
        rb.roadmap_builder(propuesta, problem_case=caso_, fases=fases_)
    finally:
        progreso.terminar()
    assert pintadas[-1][1] == 1.0, pintadas  # la barra llega al final aunque haya fases sin generar
    return BeautifulSoup(copia.read_text(encoding="utf-8"), "html.parser")


def textos(sopa_, selector: str) -> list[str]:
    return [s.get_text(strip=True) for s in sopa_.select(selector)]


# a) Sin Modelado / Análisis ni Ops: 5 fases
graph.build_structure_proposal = lambda caso_, historial: bloque(
    "sí | El ERP y el CRM", "sí | Carga nocturna", "sí | Un lago", "sí | Limpieza de ventas",
    "no | El cliente solo quiere ver sus ventas tal cual", "no | Sin modelos ni operación propia",
    "sí | Cuadros de ventas")
estado = graph.structure_proposal_node({"problem_case": caso, "chat_history": []})
assert estado["assistant_response"] == "1- Introducción\n2- Servicios\n3- Arquitectura", estado["assistant_response"]
fases = estado["fases_roadmap"]
assert [d for d, (e, _) in fases.items() if e == rb.NO] == ["roadmap-modeling", "roadmap-ml"], fases
sopa = generar(caso, fases)
assert len(prompts) == 5, f"solo las 5 fases que aplican: {len(prompts)} llamadas"
assert all("las 5 fases del roadmap se escriben a la vez" in p for p in prompts)
assert any("Ops (no aplica)" in texto for _, _, texto in pintadas), pintadas
assert textos(sopa, ".rdm-pipe-name") == ["Fuentes", "Ingesta", "Almacenamiento", "ETL", "Visualización"]
assert len(sopa.select(".rdm-pipe-arrow")) == 4
assert textos(sopa, "#rdm-stages-container .rdm-step") == ["01", "02", "03", "04", "05"]
assert textos(sopa, ".rdm-no-aplica") == ["No aplica a este proyecto: Modelado / Análisis (El cliente solo quiere ver "
                                          "sus ventas tal cual); Ops (Sin modelos ni operación propia)"]
assert "roadmap-" not in sopa.select_one("#tab-roadmap").get_text(), "un id interno se ve en el informe"

# b) Solo cuadros de mando, con el DWH, el ETL y las fuentes en el stack: Visualización y 4 «ya lo tiene»
CON_STACK = {**caso, "stack_tecnologico_actual": {"description": "Stack", "value": [
    "SAP Business One como ERP", "almacén de datos en SQL Server 2019", "paquetes de SSIS que cargan cada noche"]}}
graph.build_structure_proposal = lambda caso_, historial: bloque(
    "ya lo tiene | Su ERP SAP Business One", "ya lo tiene | Los paquetes de SSIS que cargan cada noche",
    "ya lo tiene | Su almacén de datos en SQL Server", "ya lo tiene | Las transformaciones en SSIS",
    "no | Los cuadros muestran los datos tal cual", "no | Sin modelos que operar", "sí | Los cuadros de mando de ventas")
estado = graph.structure_proposal_node({"problem_case": CON_STACK, "chat_history": []})
fases = estado["fases_roadmap"]
assert [d for d, (e, _) in fases.items() if e == rb.YA] == rb._ROADMAP_IDS[:4], fases
assert estado["assistant_response"].endswith(
    "Ya implantado en el cliente (se reutiliza y se integra; no se presupuesta como nuevo): Fuentes (Su ERP SAP "
    "Business One); Ingesta (Los paquetes de SSIS que cargan cada noche); Almacenamiento (Su almacén de datos en SQL "
    "Server); ETL (Las transformaciones en SSIS)."), estado["assistant_response"]
assert "FASES DEL ROADMAP" not in estado["assistant_response"]
sopa = generar(CON_STACK, fases)
assert len(prompts) == 1, f"solo Visualización: {len(prompts)} llamadas"
assert textos(sopa, ".rdm-pipe-name") == ["Fuentes", "Ingesta", "Almacenamiento", "ETL", "Visualización"]
assert textos(sopa, ".rdm-pipe-implantado .rdm-pipe-tech") == ["Ya implantado"] * 4
assert len(sopa.select(".rdm-pipe-arrow")) == 4
assert [c["id"] for c in sopa.select("#rdm-stages-container > .section-card")] == ["card-roadmap-bi"]
assert textos(sopa, "#rdm-stages-container .rdm-step") == ["01"]
assert textos(sopa, ".rdm-ya-implantado") == [
    "Se reutiliza: Fuentes (Su ERP SAP Business One); Ingesta (Los paquetes de SSIS que cargan cada noche); "
    "Almacenamiento (Su almacén de datos en SQL Server); ETL (Las transformaciones en SSIS)"]
assert "roadmap-" not in sopa.select_one("#tab-roadmap").get_text(), "un id interno se ve en el informe"

# c) «ya lo tiene» sin nada en el stack que lo cubra: la fase se genera y lo dice el log
avisos.clear()
_, fases = rb.leer_fases(bloque("ya lo tiene | Su almacén en Snowflake", "sí | a", "sí | b", "sí | c", "no | d",
                                "no | e", "sí | f"), CON_STACK)
assert fases["roadmap-sources"][0] == rb.SI, fases
assert any(a.startswith("[fases] Fuentes") and "se genera" in a for a in avisos), avisos
_, fases = rb.leer_fases(bloque("ya lo tiene | Su ERP SAP Business One", *["sí | x"] * 6), caso)  # caso sin stack
assert fases["roadmap-sources"][0] == rb.SI, fases

# d) Todas «no» o «ya lo tiene», y sin bloque: las 7, con su línea en el log
avisos.clear()
_, fases = rb.leer_fases(bloque("ya lo tiene | Su ERP SAP Business One", *["no | x"] * 6), CON_STACK)
assert fases is None and any(a.startswith("[fases]") and "van las 7" in a for a in avisos), avisos
avisos.clear()
texto, sin_fases = rb.leer_fases("1- Introducción\n2- Servicios", caso)
assert sin_fases is None and texto == "1- Introducción\n2- Servicios"
assert any(a.startswith("[fases]") and "van las 7" in a for a in avisos), avisos
prompts.clear()
rb.roadmap_builder(propuesta, problem_case=caso, fases=sin_fases)
assert len(prompts) == 7
logging.getLogger("aidm").removeHandler(recoger)
print("OK 7: solo las fases «sí» (5 del 01 al 05; o 1 con 4 «Ya implantado» validadas por el stack); sin stack, se genera; todas «no»/«ya» o sin bloque, las 7")

assert hashlib.md5(INFORME.read_bytes()).hexdigest() == huella_antes, "el informe real no debía cambiar"
print("\nTodo bien: los fallos 4, 73, 88, 94 y 131 están arreglados.")