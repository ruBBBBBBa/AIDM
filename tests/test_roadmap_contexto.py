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

# c2) Fallo 131 (parte 2): «ya lo tiene» por la herramienta, con trabajo por hacer en la frase, va como «sí» y lo dice
#     el log; las fuentes que de verdad tiene siguen como «ya lo tiene», y una negación («nada nuevo») no cuenta
CON_BI = {**CON_STACK, "stack_tecnologico_actual": {"description": "Stack", "value": [
    "SAP Business One como ERP", "Power BI Pro para dirección"]}}
avisos.clear()
_, fases = rb.leer_fases(bloque("ya lo tiene | Su ERP SAP Business One", "sí | a", "sí | b", "sí | c", "sí | d",
                                "no | e", "ya lo tiene | Sus licencias de Power BI Pro, que se reutilizarán para "
                                "conectar los nuevos modelos de datos"), CON_BI)
assert fases["roadmap-sources"][0] == rb.YA and fases["roadmap-bi"][0] == rb.SI, fases
assert any(a.startswith("[fases] Visualización") and "trabajo por hacer («se reutilizaran para»)" in a
           for a in avisos), avisos
avisos.clear()
_, fases = rb.leer_fases(bloque("ya lo tiene | Su ERP SAP Business One", "sí | a", "sí | b", "sí | c", "sí | d",
                                "no | e", "ya lo tiene | Sus cuadros de mando en Power BI Pro; no hace falta nada "
                                "nuevo"), CON_BI)
assert fases["roadmap-bi"][0] == rb.YA and not any("trabajo por hacer" in a for a in avisos), (fases, avisos)
assert "Si solo tiene la herramienta o las licencias" in rb.INSTRUCCION_FASES

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
print("OK 7: solo las fases «sí» (5 del 01 al 05; o 1 con 4 «Ya implantado» validadas por el stack); sin stack o con trabajo por hacer, se genera; todas «no»/«ya» o sin bloque, las 7")

# 8) Fallo 132: el resumidor. La llamada 1 (las secciones de la propuesta) va a la vez que las fases del roadmap y la 2,
#    al acabar. El JSON llega a los bloques de la pestaña Resumen, a los puntos clave, a los interruptores «Detalle |
#    Resumen», al flujo y al CRISP-DM; una frase con una cifra que no está en su sección y un punto clave con un servicio
#    se quitan y lo dice el log; el bloque de costes lleva las cifras de Python; la barra suma los 2 pasos. Si las
#    llamadas fallan o su JSON no se lee: bloques sin texto, sin interruptores, una línea en el log y el informe sigue.
import json

from agents import resumen_secciones as rs
from agents.discovery_agent_logic import _cifras

graph.roadmap_builder = rb.roadmap_builder
graph.resumir_propuesta, graph.completar_resumen = rs.resumir_propuesta, rs.completar_resumen
rb.HILOS = 1
empezado = {"resumen": threading.Event(), "fase": threading.Event()}
a_la_vez: list[bool] = []
respuestas: dict = {}
prompts_resumen: dict = {}


def kickoff_resumen(self, *args, **kwargs):
    descripcion = self.tasks[0].description
    llamada = 1 if descripcion.startswith("Resume cada sección") else 2 if descripcion.startswith(
        "Con lo que ya se ha escrito") else 0
    if llamada:
        prompts_resumen[llamada] = descripcion
        if llamada == 1:
            empezado["resumen"].set()
            a_la_vez.append(empezado["fase"].wait(5))  # las fases empiezan mientras la llamada 1 sigue en marcha
        respuesta = respuestas.get(llamada, "")
        if isinstance(respuesta, Exception):
            raise respuesta
        return SimpleNamespace(raw=respuesta)
    empezado["fase"].set()
    a_la_vez.append(empezado["resumen"].wait(5))  # y la llamada 1 ha empezado sin esperar a que acaben
    return kickoff_simulado(self, *args, **kwargs)


plantilla = BeautifulSoup(INFORME.read_text(encoding="utf-8"), "html.parser")
cifra_buena = max(_cifras(plantilla.find(id="content-architecture").get_text(" ", strip=True)), key=len)
respuestas[1] = "```json\n" + json.dumps({
    "arquitectura": {"para_todos": "Una plataforma en la nube que reúne los datos del cliente.",
                     "decision_principal": f"Arquitectura sin servidores con {cifra_buena} piezas.",
                     "por_que": "Escala sola. Ahorra 9137 horas al año.",
                     "piezas_clave": ["Un lago de datos", "Un flujo de 9137 tareas"],
                     "riesgos": ["Depender de un proveedor"], "siguiente_paso": "Validar el piloto."},
    "gobernanza": {"para_todos": "Reglas claras sobre quién ve cada dato.", "decision_principal": "Un comité del dato."},
    "plataforma": "no es un objeto"}, ensure_ascii=False) + "\n```"
respuestas[2] = json.dumps({
    "resumen_general": "El cliente tendrá sus datos en un solo sitio y cuadros para decidir.",
    "puntos_clave": [{"texto": "Los datos se reúnen en un único lugar.", "seccion": "arquitectura"},
                     {"texto": "Los cuadros de ventas llegan a dirección.", "seccion": "visualizacion_roadmap"},
                     {"texto": "Se orquesta con AWS Step Functions.", "seccion": "etl"},
                     {"texto": "Se ahorran 9137 horas.", "seccion": "arquitectura"},
                     {"texto": "El equipo se forma desde el principio.", "seccion": None},
                     {"texto": "Un sexto punto que sobra.", "seccion": None}],
    "fases": {"fuentes": {"puntos": ["El ERP y el CRM", "Ficheros de ventas"], "decision_principal": "Conectar el ERP."},
              "ops": {"puntos": ["Alertas de los procesos"], "decision_principal": "Vigilar las cargas."}},
    "crisp": {clave: [f"Punto de {clave}"] for clave, _, _ in rs.CRISP}}, ensure_ascii=False)
solucion = {**propuesta, "cifras_costes": {"total_etiqueta": "Coste total (equipo incluido)",
                                           "total": "72.000 € – 80.000 €", "meses": "4 meses", "mvp": "",
                                           "al_mes": "1.200 €"}}


def resumir(fases_=None) -> BeautifulSoup:
    shutil.copy(INFORME, copia)
    for evento in empezado.values():
        evento.clear()
    a_la_vez.clear()
    avisos.clear()
    pintadas.clear()
    prompts_resumen.clear()
    Crew.kickoff = kickoff_resumen
    # 1 (el inventario, sin resumen) + las 7 fases + las 2 llamadas del resumidor
    progreso.empezar(lambda fraccion, texto: pintadas.append((threading.get_ident(), fraccion, texto)), total=10)
    try:
        graph.roadmap_node({"problem_solution": solucion, "problem_case": caso, "fases_roadmap": fases_})
    finally:
        progreso.terminar()
        Crew.kickoff = kickoff_simulado
    assert pintadas[-1][1] == 1.0 and {hilo for hilo, _, _ in pintadas} == {threading.get_ident()}, pintadas
    pasos = [texto for _, _, texto in pintadas]
    assert pasos[-1].endswith("10 de 10 pasos"), pasos  # la llamada 1 cuenta aunque su texto no llegue a pintarse
    assert any(t.startswith("Resumen: puntos clave y fases") for t in pasos), pasos
    return BeautifulSoup(copia.read_text(encoding="utf-8"), "html.parser")


logging.getLogger("aidm").addHandler(recoger)
sopa = resumir()
assert a_la_vez and all(a_la_vez), "la llamada 1 del resumidor no va a la vez que las fases del roadmap"
assert graph.PASOS_DEL_INFORME == 15
assert "fases: solo estas: fuentes, ingesta, almacenamiento, etl, modelado, ops, visualizacion_roadmap." \
       in prompts_resumen[2] and "72.000" not in prompts_resumen[2], "la llamada 2 no recibe cifras de costes"
# La pestaña Resumen va primera y activa
assert sopa.select(".tab-btn")[0].get_text(strip=True) == "Resumen" and "active" in sopa.select(".tab-btn")[0]["class"]
assert "active" in sopa.find(id="tab-resumen")["class"] and "active" not in sopa.find(id="tab-propuesta")["class"]
assert sopa.find(id="resumen-general").get_text() == "El cliente tendrá sus datos en un solo sitio y cuadros para decidir."
# Bloques: todos con enlace (todo se generó); el texto de la llamada 1 y, en costes, las cifras de Python
bloques = sopa.select("#resumen-bloques > .resumen-bloque")
assert [b.h3.get_text() for b in bloques] == ["Plataforma", "Arquitectura", "Gobernanza", "Datos e IA", "Visualización",
                                               "Costes"]
assert [b.get("href") for b in bloques] == ["#seccion-plataforma", "#seccion-arquitectura", "#seccion-gobernanza",
                                            "#seccion-datos-ia", "#seccion-visualizacion", "#seccion-costes"]
assert [b.p.get_text() if b.p else "" for b in bloques] == [
    "", "Una plataforma en la nube que reúne los datos del cliente.", "Reglas claras sobre quién ve cada dato.", "", "",
    "Coste total (equipo incluido): 72.000 € – 80.000 € · al mes, lo que queda funcionando: 1.200 € · plazo: 4 meses."]
# El «Resumen» de arquitectura: sin la frase de 9137 ni la pieza de 9137; la cifra que sí está en la sección se queda
tarjeta = sopa.find(id="seccion-arquitectura").find_parent(class_="section-card")
assert [(b.get_text(), b["aria-pressed"]) for b in tarjeta.select(".vista-btn")] == [("Resumen", "true"),
                                                                                      ("Detalle", "false")]
assert "modo-resumen" in tarjeta["class"]  # se abre en el resumen
resumen_arq = tarjeta.select_one(".resumen-seccion")
assert cifra_buena in resumen_arq.get_text() and "9137" not in resumen_arq.get_text(), resumen_arq
assert [li.get_text() for li in resumen_arq.select("li")] == ["Un lago de datos", "Depender de un proveedor"]
assert "Escala sola." in resumen_arq.get_text() and "Ahorra" not in resumen_arq.get_text()
assert any("fuera una frase con cifras" in a and "9137" in a for a in avisos), avisos
# Interruptor solo donde hay resumen: arquitectura, gobernanza y las fases Fuentes y Ops (nunca costes ni perfiles)
con_interruptor = [s.find_parent(class_="section-card").select_one(".section-header")["id"]
                   for s in sopa.select(".vista-seccion")]
assert con_interruptor == ["seccion-arquitectura", "seccion-gobernanza", "fase-fuentes", "fase-ops"], con_interruptor
# Puntos clave: el del servicio y el de la cifra, fuera; como mucho 5; cada uno con el enlace a su sección
puntos = sopa.select("#resumen-puntos > li")
assert [(li.get_text(), li.a["href"] if li.a else None) for li in puntos] == [
    ("Los datos se reúnen en un único lugar.", "#seccion-arquitectura"),
    ("Los cuadros de ventas llegan a dirección.", "#fase-visualizacion"),
    ("El equipo se forma desde el principio.", None)], puntos
assert any("nombra un servicio (AWS Step Functions)" in a for a in avisos), avisos
# El flujo y el CRISP-DM, con sus puntos; la pestaña de Asier aún no lee #crisp-datos: oculta y sin enlaces
nodos = sopa.select("#resumen-flujo .rdm-pipe-node")
assert [n.get("href") for n in nodos] == [f"#{d}" for _, _, d in rs.FASES] and not any(n.get("onclick") for n in nodos)
assert [li.get_text() for li in nodos[0].select(".resumen-puntos-fase li")] == ["El ERP y el CRM", "Ficheros de ventas"]
assert sopa.find(id="tab-crispdm").has_attr("hidden") and any("no lee #crisp-datos" in a for a in avisos), avisos
fases_crisp = sopa.select("#resumen-crisp .crisp-phase")
assert len(fases_crisp) == 6 and all(f.name == "div" for f in fases_crisp), fases_crisp
assert fases_crisp[0].select_one(".resumen-puntos-fase li").get_text() == "Punto de negocio"
assert json.loads(sopa.find(id="crisp-datos").string)["1"] == {"titulo": "01 · Comprensión del negocio",
                                                              "actividades": ["Punto de negocio"]}
assert "roadmap-" not in sopa.find(id="tab-resumen").get_text(), "un id interno se ve en la pestaña Resumen"

# Si el JSON no se lee (llamada 2) o la llamada falla (llamada 1): bloques sin texto, sin interruptores, el log lo dice
respuestas.update({1: RuntimeError("503 simulado"), 2: "Lo siento, no puedo."})
sopa = resumir()
assert not sopa.select(".vista-seccion, .resumen-seccion"), "sin resumen no hay interruptor"
assert [b.p.get_text() for b in sopa.select("#resumen-bloques > .resumen-bloque") if b.p] == [
    "Coste total (equipo incluido): 72.000 € – 80.000 € · al mes, lo que queda funcionando: 1.200 € · plazo: 4 meses."]
assert sopa.find(id="resumen-puntos-parte").has_attr("hidden") and not sopa.find(id="resumen-general").get_text()
assert len(sopa.select("#resumen-flujo .rdm-pipe-node")) == 7 and not sopa.select(".resumen-puntos-fase")
assert any("La llamada 1 ha fallado (RuntimeError: 503 simulado)" in a for a in avisos), avisos
assert any(a.startswith("[resumen] Llamada 2 sin JSON") for a in avisos), avisos
assert sopa.select_one("#roadmap-sources").get_text(strip=True), "el roadmap sigue en el informe"
logging.getLogger("aidm").removeHandler(recoger)
print("OK 8: el resumen de la propuesta va a la vez que el roadmap; el JSON se reparte; cifras inventadas y servicios, "
      "fuera; costes con las cifras de Python; sin JSON, sin interruptores y el informe sigue")

# 9) Fallo 132 (parte 3): un punto clave que nombra un servicio nuevo, aunque sea a medias («Microsoft Fabric»,
#    «Fabric»), se quita; la nube, lo que el cliente ya usa y los conceptos se quedan («Power BI Premium» se quita si
#    solo tiene Pro). Con menos de 4, lo dice el log. Una viñeta larga nunca se corta a mitad: en su última coma o
#    entera. Las cifras de los datos del cliente valen. Los prompts llevan las reglas.
caso_8, solucion_8 = caso, solucion
caso = {**caso_8, "volumen_de_datos": {"description": "Volumen", "value": "38 tiendas y 2 TB de histórico"},
        "stack_tecnologico_actual": {"description": "Stack", "value": "Microsoft Dynamics 365 Business Central, "
                                     "Microsoft 365, Azure, Power BI Pro y SQL Server en local para los TPV"}}
solucion = {**solucion_8, "inventario": [{"servicio": s, "capa": c, "fase": "mvp", "alternativa": ""} for s, c in (
    ("Microsoft Dynamics 365 Business Central", "fuentes"), ("Microsoft Fabric OneLake", "almacenamiento"),
    ("Delta Lake", "almacenamiento"), ("Power BI Premium", "visualización"), ("Microsoft Purview", "gobierno"))]}
SIN_COMA = "Las ventas de cada tienda llegan solas al almacén central cada noche sin que nadie tenga que tocar nada"
CON_COMA = "Los datos se revisan antes de cargarse, para evitar errores en los cuadros de mando de la dirección general"
respuestas.update({1: json.dumps({"arquitectura": {"para_todos": "Un almacén único para los datos.",
                                                   "riesgos": ["Coordinar las 38 tiendas a la vez."]}}),
                   2: json.dumps({"resumen_general": "Los datos de la cadena, juntos y al día.", "puntos_clave": [
                       {"texto": "Arquitectura Lakehouse con Microsoft Fabric unifica los datos.", "seccion": None},
                       {"texto": "Todo queda guardado en Fabric.", "seccion": None},
                       {"texto": "Power BI Premium para toda la plantilla.", "seccion": None},
                       {"texto": "Los datos de Business Central y de los TPV llegan cada noche a Azure.", "seccion": None},
                       {"texto": "Un Lakehouse (un almacén único) alimenta los cuadros de Power BI.", "seccion": None}],
                       "fases": {"fuentes": {"puntos": [SIN_COMA, CON_COMA]}},
                       "crisp": {"negocio": ["Entender qué decide la dirección cada semana."]}})})
logging.getLogger("aidm").addHandler(recoger)  # el caso 8 lo quitó al acabar
try:
    sopa = resumir()
finally:
    logging.getLogger("aidm").removeHandler(recoger)
assert [li.get_text() for li in sopa.select("#resumen-puntos > li")] == [
    "Los datos de Business Central y de los TPV llegan cada noche a Azure.",
    "Un Lakehouse (un almacén único) alimenta los cuadros de Power BI."], sopa.select("#resumen-puntos > li")
assert sum("nombra un servicio (Microsoft Fabric OneLake)" in a for a in avisos) == 2, avisos
assert any("nombra un servicio (Power BI Premium)" in a for a in avisos), avisos
assert any("Solo 2 puntos clave" in a for a in avisos), avisos
assert [li.get_text() for li in sopa.select("#resumen-flujo .rdm-pipe-node")[0].select(".resumen-puntos-fase li")] == [
    SIN_COMA, "Los datos se revisan antes de cargarse"]
riesgos = sopa.find(id="seccion-arquitectura").find_parent(class_="section-card").select(".resumen-seccion li")
assert [li.get_text() for li in riesgos] == ["Coordinar las 38 tiendas a la vez."], riesgos
assert all("Lakehouse (un almacén único para todos los datos)" in prompts_resumen[n] for n in (1, 2))
assert "ni su marca ni su nombre corto" in prompts_resumen[2] and prompts_resumen[2].count("VACUUM/OPTIMIZE") == 2
caso, solucion = caso_8, solucion_8
print("OK 9: fuera el punto clave con un servicio nuevo, también a medias; se quedan la nube, lo del cliente y los "
      "conceptos; las viñetas no se cortan a mitad y valen las cifras del cliente")

assert hashlib.md5(INFORME.read_bytes()).hexdigest() == huella_antes, "el informe real no debía cambiar"
print("\nTodo bien: los fallos 4, 73, 88, 94, 131 y 132 están arreglados.")