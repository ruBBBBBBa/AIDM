"""Comprueba el gateway de modelos (gateway.py y data_structures/modelos.yaml).

Cada tarea tiene un perfil, y cada perfil una lista de modelos: el primero se usa y los demás son sus reservas. Con
MODELO_<TAREA> en el .env se pone otro modelo delante en una sola tarea. Cada llamada queda en el log y, con
GRABAR_LLAMADAS=1, en un archivo para comparar modelos.

No llama a ningún LLM: se sustituyen Crew.kickoff y litellm.completion por versiones simuladas, y donde hace falta
una llamada de verdad de CrewAI se usa la respuesta simulada de LiteLLM (mock_response), que no sale a la red.
No toca tu informe ni tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_gateway.py
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
import re
import shutil
import tempfile
from types import SimpleNamespace

import yaml
from crewai import Crew
from crewai.events import crewai_event_bus

import gateway
import utils
import agents.discovery_agent_logic as discovery
import agents.estimation_crew as ec
import agents.proposal_crew as pc
import agents.roadmap_builder as rb
import agents.structure_proposal_agent_logic as sp

# El .env de quien ejecuta el test no cuenta: sin MODELO_* ni grabación
for clave in [c for c in os.environ if c.startswith("MODELO_")] + ["GRABAR_LLAMADAS"]:
    os.environ.pop(clave, None)

config = yaml.safe_load(Path("data_structures/modelos.yaml").read_text(encoding="utf-8"))
PERFILES, TAREAS = config["perfiles"], config["tareas"]
PROVEEDORES = ("groq/", "gemini/", "mistral/", "sambanova/", "nvidia_nim/", "cloudflare/")

# 1) El yaml: cada tarea tiene un perfil que existe, las tareas son justo las que piden los agentes, y cada modelo
#    lleva uno de los seis proveedores
codigo = "".join(p.read_text(encoding="utf-8") for p in Path("agents").glob("*.py"))
pedidas = set(re.findall(r'gateway\.modelo\("(\w+)"\)', codigo))
assert set(TAREAS) == pedidas, set(TAREAS) ^ pedidas
assert all(perfil in PERFILES for perfil in TAREAS.values()), "una tarea apunta a un perfil que no existe"
assert config["por_defecto"] in PERFILES
for perfil, lista in PERFILES.items():
    assert lista and all(m.startswith(PROVEEDORES) for m in lista), (perfil, lista)
print(f"OK 1: las {len(TAREAS)} tareas tienen perfil, y los {len(PERFILES)} perfiles usan los seis proveedores")

# 2) modelo(): el primero del perfil, con las demás como reservas; MODELO_<TAREA> va delante; sin tarea, el perfil
#    por defecto
log = io.StringIO()
with contextlib.redirect_stdout(log):
    costes = gateway.modelo("costes")
assert costes == PERFILES["calculo"][0] and costes.tarea == "costes"
assert list(costes.reservas) == PERFILES["calculo"][1:]
assert f"[gateway] costes: {PERFILES['calculo'][0]} (perfil calculo; reservas: " in log.getvalue(), log.getvalue()
os.environ["MODELO_COSTES"] = "groq/openai/gpt-oss-120b"
with contextlib.redirect_stdout(io.StringIO()):
    propio = gateway.modelo("costes")
    nueva = gateway.modelo("tarea_nueva")
assert propio == "groq/openai/gpt-oss-120b" and list(propio.reservas) == PERFILES["calculo"]
del os.environ["MODELO_COSTES"]
assert nueva == PERFILES[config["por_defecto"]][0]
print("OK 2: cada tarea usa el primero de su perfil con el resto de reserva; MODELO_COSTES va delante")

# 3) build_llm(): las reservas van con sus reglas, la tarea va en la etiqueta y Gemini pasa por LiteLLM; un modelo
#    pasado a mano (los tests) no lleva reservas
with contextlib.redirect_stdout(io.StringIO()):
    llm = gateway.build_llm(gateway.modelo("costes"))
assert llm.model == PERFILES["calculo"][0] and llm.is_litellm
assert llm.additional_params["metadata"] == {"aidm_tarea": "costes"}
reservas = llm.additional_params["fallbacks"]
assert [r["model"] for r in reservas] == PERFILES["calculo"][1:]
assert {"model": "sambanova/gemma-4-31B-it", "num_retries": 0, "timeout": 100} in reservas  # la regla de SambaNova
assert llm.timeout == 300  # sin tope, el 3 nov el equipo esperó 2 × 600 s a Gemini
assert gateway.parametros("cloudflare/@cf/openai/gpt-oss-120b") == {"num_retries": 2, "timeout": 300, "max_tokens": 8192}
assert gateway.build_llm("gemini/gemini-3.6-flash").is_litellm
assert gateway.parametros("groq/openai/gpt-oss-20b") == {"num_retries": 0, "timeout": 300, "max_tokens": 4096}
# Las reglas del proveedor valen para cualquier modelo suyo, aunque no esté en el yaml, y las del modelo mandan
assert gateway.parametros("nvidia_nim/otro/modelo") == {"num_retries": 0, "timeout": 100}
reglas = {"proveedores": {"sambanova": {"num_retries": 0, "timeout": 100}}, "modelos": {"sambanova/x": {"timeout": 50}}}
assert gateway._reglas("sambanova/x", reglas) == {"num_retries": 0, "timeout": 50}
assert gateway._reglas("sambanova/y", reglas) == {"num_retries": 0, "timeout": 100}
assert utils.build_llm is gateway.build_llm
print("OK 3: cada modelo y sus reservas llevan sus reglas, con 5 minutos como máximo por intento, y Gemini va por LiteLLM")

# 4) Cada agente usa el modelo de su tarea: se le da uno distinto a cada una
for tarea in TAREAS:
    os.environ[f"MODELO_{tarea.upper()}"] = f"groq/prueba-{tarea}"
roles = {datos["role"].strip(): clave for clave, datos in utils.load_agents_config().items()}
usados: dict[str, dict[str, str]] = {}
fase = ""


def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        clave = task.name or roles[task.agent.role.strip()]  # perfiles y equipo comparten rol: van por su nombre
        usados.setdefault(fase, {})[clave] = task.agent.llm.model
        task.output = SimpleNamespace(raw="texto", pydantic=None)
        if getattr(self, "task_callback", None):
            self.task_callback(SimpleNamespace(raw="texto"))
    return SimpleNamespace(raw="texto")


def completion_simulada(**kwargs):
    usados.setdefault(fase, {})["litellm"] = kwargs["model"]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({"updates": [], "assistant_reply": "¿Cuál es el sector?", "update_status": None})))])


completion_real = discovery.litellm.completion
Crew.kickoff = kickoff_simulado
discovery.litellm.completion = completion_simulada
os.environ["INFRACOST_API_KEY"] = "prueba"  # para que se pida el modelo de Terraform; no se llama a Infracost
ec._resolve_infracost_binary = lambda: "infracost"
ec._generate_terraform_from_proposal = lambda propuesta, modelo: usados.setdefault(fase, {}).update(terraform=modelo) or ""
copia = Path(tempfile.mkdtemp()) / "informe.html"
shutil.copy("data_structures/solution_builder_report.html", copia)
rb.REPORT_PATH = copia
CASO = {"cloud_provider": {"description": "", "value": "Azure"}, "status": "completed"}

with contextlib.redirect_stdout(io.StringIO()):
    fase = "discovery"; discovery._propose_plan_with_agent("Somos una distribuidora", [])
    fase = "estructura"; sp.build_structure_proposal(CASO, chat_history=[])
    fase = "propuesta"; pc.run_proposal_crew("ESTRUCTURA", CASO, chat_history=[])
    fase = "costes"; ec.run_estimation_crew("PROPUESTA", problem_case=CASO)
    fase = "roadmap"; rb.roadmap_builder("PROPUESTA", problem_case=CASO)

p = lambda tarea: f"groq/prueba-{tarea}"
assert usados["discovery"] == {"litellm": p("discovery")}, usados["discovery"]
assert usados["estructura"] == {"structure_proposal_agent": p("estructura")}, usados["estructura"]
assert usados["propuesta"] == {"structure_proposal_agent": p("arquitectura"), "data_governance_agent": p("gobernanza"),
                               "data_ai_strategist": p("estrategia"),
                               "bi_visualization_specialist": p("visualizacion")}, usados["propuesta"]
assert usados["costes"] == {"terraform": p("terraform"), "costes": p("costes"), "perfiles": p("perfiles"),
                            "equipo": p("equipo")}, usados["costes"]
assert usados["roadmap"] == {"resume_agent": p("resumen"), "data_sources_agent": p("roadmap_fuentes"),
                             "ingestion_agent": p("roadmap_ingesta"), "storage_agent": p("roadmap_almacenamiento"),
                             "etl_processing_agent": p("roadmap_etl"), "modeling_serving_agent": p("roadmap_modelado"),
                             "ml_mlops_agent": p("roadmap_ml"), "bi_visualization_agent": p("roadmap_bi")}, usados["roadmap"]
for tarea in TAREAS:
    del os.environ[f"MODELO_{tarea.upper()}"]
print("OK 4: cada tarea usa su modelo; los perfiles del equipo ya no comparten modelo con el JSON del equipo")

# 5) Si un test pasa el modelo a la crew, vale para todos sus agentes, sin reservas (como antes)
usados.clear()
with contextlib.redirect_stdout(io.StringIO()):
    fase = "costes"; ec.run_estimation_crew("PROPUESTA", "groq/fijo", problem_case=CASO)
assert set(usados["costes"].values()) == {"groq/fijo"}, usados
print("OK 5: con el modelo pasado a mano, toda la crew usa ese")

# 6) Registro: cada llamada deja su línea en el log y, con GRABAR_LLAMADAS=1, se guarda para comparar modelos
discovery.litellm.completion = completion_real
grabaciones = Path(tempfile.mkdtemp()) / "grabaciones"
gateway.GRABACIONES, gateway._archivo = grabaciones, None
os.environ["GRABAR_LLAMADAS"] = "1"
log = io.StringIO()
with contextlib.redirect_stdout(log):
    llm = gateway.build_llm(gateway.modelo("costes"))
    llm.additional_params["mock_response"] = '{"componentes": []}'  # LiteLLM responde esto sin salir a la red
    llm.call([{"role": "user", "content": "Valora los componentes"}])
    gateway.completar(gateway.modelo("discovery"), [{"role": "user", "content": "Somos una distribuidora"}],
                      mock_response='{"updates": []}')
    crewai_event_bus.flush()
del os.environ["GRABAR_LLAMADAS"]
lineas = log.getvalue()
assert re.search(rf"\[gateway\] costes · {re.escape(PERFILES['calculo'][0])} · \d+ tokens de entrada y \d+ de salida · \d+,\d s", lineas), lineas
assert re.search(rf"\[gateway\] discovery · {re.escape(PERFILES['extraccion'][0])} · \d+ tokens de entrada", lineas), lineas
grabadas = [json.loads(l) for f in grabaciones.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
por_tarea = {g["tarea"]: g for g in grabadas}
assert set(por_tarea) == {"costes", "discovery"}, grabadas
assert por_tarea["costes"]["respuesta"] == '{"componentes": []}'
assert por_tarea["costes"]["mensajes"][-1]["content"] == "Valora los componentes"
assert por_tarea["discovery"]["mensajes"] == [{"role": "user", "content": "Somos una distribuidora"}]
print("OK 6: cada llamada sale en el log con sus tokens y segundos, y con GRABAR_LLAMADAS=1 se guarda su prompt")

# 7) Si responde una reserva, el log lo dice (el 3 nov decía Gemini 3.8 Flash en el roadmap y respondieron las reservas)
import litellm
log = io.StringIO()
with contextlib.redirect_stdout(log):
    llm = gateway.build_llm(gateway.modelo("arquitectura"))
    llm.additional_params["num_retries"] = 0
    llm.additional_params["mock_response"] = litellm.RateLimitError("Rate limit exceeded", llm_provider="mistral",
                                                                    model=PERFILES["redaccion"][0])
    llm.additional_params["fallbacks"][0]["mock_response"] = "Respuesta de la reserva"
    assert llm.call([{"role": "user", "content": "Escribe la arquitectura"}]) == "Respuesta de la reserva"
assert f"[gateway] arquitectura · {PERFILES['redaccion'][0]} → reserva {PERFILES['redaccion'][1]} · " in log.getvalue(), log.getvalue()
print("OK 7: si el primer modelo falla y responde una reserva, la línea del log lo dice")

# 8) Una respuesta que llega al límite de tokens sale como «cortada» en el log y en la grabación (el 5 nov, la
#    gobernanza de Gemma 4 en SambaNova acabó a media palabra al llenar su ventana de 8.192 tokens)
def completion_cortada(*args, **kwargs):
    return SimpleNamespace(model="gemma-4-31B-it", _hidden_params={"custom_llm_provider": "sambanova"},
                           choices=[SimpleNamespace(message=SimpleNamespace(content="…que el técnico de Iber"),
                                                    finish_reason="length")],
                           usage=SimpleNamespace(prompt_tokens=7154, completion_tokens=1038))


original, gateway._completion_registrada.original = gateway._completion_registrada.original, completion_cortada
gateway._archivo = None
os.environ["GRABAR_LLAMADAS"] = "1"
log = io.StringIO()
try:
    with contextlib.redirect_stdout(log):
        litellm.completion(model="sambanova/gemma-4-31B-it", messages=[{"role": "user", "content": "Gobernanza"}],
                           metadata={"aidm_tarea": "gobernanza"})
finally:
    gateway._completion_registrada.original = original
    del os.environ["GRABAR_LLAMADAS"]
assert "[gateway] gobernanza · sambanova/gemma-4-31B-it · 7.154 tokens de entrada y 1.038 de salida · " in log.getvalue()
assert log.getvalue().rstrip().endswith("· cortada: ha llegado al límite de tokens"), log.getvalue()
grabada = json.loads(gateway._archivo.read_text(encoding="utf-8").splitlines()[-1])
assert grabada["tarea"] == "gobernanza" and grabada["cortada"] is True
print("OK 8: una respuesta cortada por el límite de tokens lo dice en el log y en la grabación")

# 9) Fallo 82: el detector de respuestas degeneradas. El 12 nov la fase de BI fueron 8.192 «!»; las tablas markdown,
#    los separadores y las listas largas no son degenerados
parrafo = ("La capa de ingesta recoge los pedidos del ERP cada noche y los deja en la zona de datos sin procesar; "
           "después se limpian, se deduplican y se cruzan con el inventario para los cuadros de mando de ventas. ")
tabla = ("| Servicio | Uso | Justificación |\n|" + "-" * 40 + "|" + "-" * 40 + "|" + "-" * 60 + "|\n"
         + "".join(f"| Servicio {i} | Ingesta del sistema {i} | Se elige por coste y porque el equipo lo conoce ({i}) |\n"
                   for i in range(200)))
normales = {
    "párrafos": parrafo * 40,
    "tabla markdown con separadores largos": parrafo + tabla,
    "separador de 2.000 guiones": parrafo + "\n" + "-" * 2000 + "\n" + parrafo,
    "fila de separación de 150 columnas": "|" + "---|" * 150 + "\n" + parrafo * 3,
    "lista larga": "".join(f"- Paso {i}: revisar la tabla de pedidos del día {i} y avisar si faltan filas.\n"
                           for i in range(300)),
    "texto corto repetido": "ok " * 30,
    "JSON": json.dumps({"componentes": [{"componente": f"Servicio {i}", "unidades": i, "precio_unitario_eur": 0.5}
                                        for i in range(100)]}, ensure_ascii=False),
}
for nombre, texto in normales.items():
    assert gateway.motivo_degenerada(texto) == "", (nombre, gateway.motivo_degenerada(texto))
assert gateway.motivo_degenerada("!" * 8192) == "«!» repetido 8.192 veces seguidas"
assert gateway.motivo_degenerada(parrafo + "!" * 500) != ""            # basura detrás de un texto bueno
assert gateway.motivo_degenerada("datos " * 500) != ""                  # una palabra en bucle
assert gateway.motivo_degenerada("ab" * 1000 + parrafo) != ""
assert gateway.motivo_degenerada(("!?" * 7 + " ") * 200) != ""          # pocos caracteres distintos, sin trozo repetido largo
print("OK 9: el detector marca los 8.192 «!» y las repeticiones, y no las tablas, los separadores ni las listas")


# 10) Fallo 82: una respuesta degenerada pasa a la siguiente reserva, con sus reglas; si ninguna da una buena, la
#     sección lleva un aviso, nunca la basura. El log dice qué modelo la dio y a qué reserva pasa
def respuesta_de(modelo: str, texto: str):
    proveedor, _, nombre = modelo.partition("/")
    return SimpleNamespace(model=nombre, _hidden_params={"custom_llm_provider": proveedor},
                           choices=[SimpleNamespace(message=SimpleNamespace(content=texto), finish_reason="length")],
                           usage=SimpleNamespace(prompt_tokens=5000, completion_tokens=8192))


llamadas = []


def completion_degenerada(*args, **kwargs):
    llamadas.append(kwargs)
    return respuesta_de(kwargs["model"], textos.get(kwargs["model"], "!" * 8192))


tecnico = PERFILES["tecnico"]
original, gateway._completion_registrada.original = gateway._completion_registrada.original, completion_degenerada
try:
    textos = {tecnico[1]: parrafo * 10}  # el primero da basura y la primera reserva da un texto bueno
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        r = litellm.completion(model=str(bi := gateway.modelo("roadmap_bi")), **gateway.parametros(bi),
                               messages=[{"role": "user", "content": "Fase de BI"}])
    assert r.choices[0].message.content == parrafo * 10
    assert [k["model"] for k in llamadas] == tecnico[:2], llamadas
    assert llamadas[1]["metadata"] == {"aidm_tarea": "roadmap_bi"} and llamadas[1]["max_tokens"] == 8192  # su regla
    assert [f["model"] for f in llamadas[1]["fallbacks"]] == tecnico[2:]  # las que quedan detrás
    assert (f"[gateway] roadmap_bi · {tecnico[0]} ha dado una respuesta degenerada (" in log.getvalue()
            and f"pasa a la reserva {tecnico[1]}" in log.getvalue()), log.getvalue()

    llamadas.clear(); textos = {}  # ninguna da un texto bueno: aviso, y se han probado todas una vez
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        r = litellm.completion(model=str(bi := gateway.modelo("roadmap_bi")), **gateway.parametros(bi),
                               messages=[{"role": "user", "content": "Fase de BI"}])
    assert r.choices[0].message.content == gateway.AVISO_DEGENERADA and "!!!" not in r.choices[0].message.content
    assert [k["model"] for k in llamadas] == tecnico, llamadas
    assert f"[gateway] roadmap_bi · {tecnico[-1]} ha dado una respuesta degenerada" in log.getvalue()
    assert "no quedan reservas: la sección sale vacía, con un aviso" in log.getvalue(), log.getvalue()

    llamadas.clear(); textos = {tecnico[0]: parrafo * 10}  # una respuesta buena no se toca
    with contextlib.redirect_stdout(io.StringIO()):
        r = litellm.completion(model=str(bi := gateway.modelo("roadmap_bi")), **gateway.parametros(bi), messages=[])
    assert r.choices[0].message.content == parrafo * 10 and len(llamadas) == 1
finally:
    gateway._completion_registrada.original = original
print("OK 10: una respuesta degenerada pasa a la siguiente reserva y, si ninguna vale, la sección lleva un aviso")

print("\nTodo bien: el gateway de modelos funciona (en la parte que se puede comprobar sin el LLM real).")