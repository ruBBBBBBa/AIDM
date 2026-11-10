"""Comprueba el fallo 68: una respuesta vaga («bastantes datos», «lo normal») no se guarda y se vuelve a preguntar.

El modelo de decisión lee la respuesta en otro hilo mientras el LLM prepara el turno. Si dice «vaga» con confianza
suficiente, el valor que haya guardado el LLM para ese campo se descarta y el asistente repite la pregunta, pidiendo
algo concreto, en el idioma del cliente. Solo una vez por campo: a la segunda, se queda lo que guarde el LLM.

No llama a ningún LLM ni al modelo de decisión (los dos se simulan) ni toca tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_discovery_vaga.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import json
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace

import decisiones
import utils
import agents.discovery_agent_logic as discovery

PLANTILLA = Path("data_structures/problem_case_recovery.json")
PREGUNTA = "¿Qué volumen de datos manejáis, aproximadamente?"


def preparar(campo_preguntado="volumen_de_datos", repreguntados=None) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "problem_case.json"
    shutil.copy(PLANTILLA, tmp)
    caso = json.loads(tmp.read_text(encoding="utf-8"))
    caso["nombre_del_cliente"]["value"] = "Logística Ebro"
    caso[discovery.CAMPO_PREGUNTADO] = campo_preguntado
    if repreguntados:
        caso[discovery.CAMPOS_REPREGUNTADOS] = repreguntados
    tmp.write_text(json.dumps(caso, ensure_ascii=False), encoding="utf-8")
    utils.PROBLEM_CASE_PATH = tmp  # la tool lee y escribe aquí, no en tu caso real
    return tmp


llm_empezado = threading.Event()
mensajes_enviados = []


def simular_llm(updates, siguiente="¿Para cuándo lo necesitaríais?", campo_siguiente="plazo_objetivo"):
    def llm(**kwargs):
        llm_empezado.set()
        mensajes_enviados.append(kwargs["messages"])
        time.sleep(0.2)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
            {"updates": updates, "assistant_reply": siguiente, "campo_preguntado": campo_siguiente,
             "update_status": "pending fields"}, ensure_ascii=False)))])
    discovery.litellm.completion = llm


def simular_modelo(eleccion, confianza=0.9, esperar_al_llm=False):
    llamadas = []

    def decidir(estado, preguntas):
        en_paralelo = llm_empezado.wait(3) if esperar_al_llm else None
        llamadas.append({"estado": estado, "preguntas": preguntas, "en_paralelo": en_paralelo})
        if eleccion is None:
            return None
        return {"estado": {"choice": eleccion, "confidence": confianza}}
    decisiones.decidir = decidir
    return llamadas


def turno(respuesta, pregunta=PREGUNTA):
    llm_empezado.clear()
    historial = [{"role": "assistant", "content": pregunta}, {"role": "user", "content": respuesta}]
    return discovery.run_agent(respuesta, chat_history=historial)


VAGO = [{"field_key": "volumen_de_datos", "operation": "replace", "value": "Bastantes datos"},
        {"field_key": "sector", "operation": "replace", "value": "Logística"}]

# 1) Respuesta vaga: no se guarda, se repite la pregunta y el resto de lo que dijo sí se guarda
tmp = preparar()
simular_llm(VAGO)
llamadas = simular_modelo("vaga", esperar_al_llm=True)
inicio = time.monotonic()
respuesta = turno("Bastantes datos. Somos de logística.")
caso = json.loads(tmp.read_text(encoding="utf-8"))
assert utils.es_valor_de_relleno(caso["volumen_de_datos"]["value"]), "el valor vago no se debe guardar"
assert caso["sector"]["value"] == "Logística", "lo demás que dijo sí se guarda"
assert respuesta == discovery.REPREGUNTA["es"] + PREGUNTA, respuesta
assert caso[discovery.CAMPO_PREGUNTADO] == "volumen_de_datos", "se sigue preguntando por el mismo campo"
assert caso[discovery.CAMPOS_REPREGUNTADOS] == ["volumen_de_datos"]
assert len(llamadas) == 1 and llamadas[0]["preguntas"] == {"estado": decisiones.PREGUNTA_ESTADO}
assert "Respuesta del cliente: Bastantes datos. Somos de logística." in llamadas[0]["estado"]
print("OK 1: «Bastantes datos» no se guarda, se repite la pregunta y el sector de la misma respuesta sí se guarda")

# 2) El modelo ha leído la respuesta a la vez que el LLM, no antes ni después
assert llamadas[0]["en_paralelo"] is True, "el modelo de decisión debe ir en otro hilo, a la vez que el LLM"
print(f"OK 2: el modelo de decisión va a la vez que el LLM (turno entero en {time.monotonic() - inicio:.1f} s)")

# 3) A la segunda respuesta vaga del mismo campo, se queda lo que guarde el LLM (no se pregunta en bucle)
simular_llm([{"field_key": "volumen_de_datos", "operation": "replace", "value": "Muchos datos"}])
simular_modelo("vaga")
respuesta = turno("Pues muchos, no sé decirte más.", pregunta=discovery.REPREGUNTA["es"] + PREGUNTA)
caso = json.loads(tmp.read_text(encoding="utf-8"))
assert caso["volumen_de_datos"]["value"] == "Muchos datos" and respuesta == "¿Para cuándo lo necesitaríais?"
assert caso[discovery.CAMPO_PREGUNTADO] == "plazo_objetivo"
print("OK 3: a la segunda respuesta vaga se guarda lo que dice el LLM y se pasa al siguiente campo")

# 4) En inglés, la repregunta va en inglés
tmp = preparar()
simular_llm([{"field_key": "volumen_de_datos", "operation": "replace", "value": "Quite a lot"}])
simular_modelo("vaga", 0.82)
pregunta_en = "Roughly how much data do you handle?"
respuesta = turno("Quite a lot, to be honest.", pregunta=pregunta_en)
assert respuesta == discovery.REPREGUNTA["en"] + pregunta_en, respuesta
print("OK 4: con un cliente en inglés, la repregunta va en inglés")

# 5) Con poca confianza, o si el modelo ve un dato, se queda lo que guarde el LLM
for eleccion, confianza in (("vaga", 0.55), ("dato", 0.95)):
    tmp = preparar()
    simular_llm(VAGO)
    simular_modelo(eleccion, confianza)
    respuesta = turno("Bastantes datos. Somos de logística.")
    caso = json.loads(tmp.read_text(encoding="utf-8"))
    assert caso["volumen_de_datos"]["value"] == "Bastantes datos", (eleccion, confianza)
    assert respuesta == "¿Para cuándo lo necesitaríais?" and discovery.CAMPOS_REPREGUNTADOS not in caso
print("OK 5: con confianza 0,55 o si el modelo ve un dato, se guarda lo del LLM y no se repregunta")

# 6) Fallo 36 sigue igual: «No lo sé» sin guardar por el LLM se guarda como «no lo sabe», con o sin el modelo
for eleccion in ("no_lo_sabe", None):
    tmp = preparar()
    simular_llm([])
    simular_modelo(eleccion)
    turno("No lo sé")
    caso = json.loads(tmp.read_text(encoding="utf-8"))
    assert caso["volumen_de_datos"]["value"] == utils.VALORES_CERRADOS["no_lo_sabe"], eleccion
print("OK 6: «No lo sé» se sigue guardando como «no lo sabe», con el modelo y sin él (por la frase)")

# 7) Sin modelo de decisión, una respuesta vaga se guarda como hasta ahora; y el LLM no ve los campos internos
tmp = preparar(repreguntados=["plazo_objetivo"])
simular_llm(VAGO)
simular_modelo(None)
mensajes_enviados.clear()
turno("Bastantes datos. Somos de logística.")
caso = json.loads(tmp.read_text(encoding="utf-8"))
assert caso["volumen_de_datos"]["value"] == "Bastantes datos"
usuario = mensajes_enviados[0][1]["content"]
assert discovery.CAMPOS_REPREGUNTADOS not in usuario and discovery.CAMPO_PREGUNTADO not in usuario
print("OK 7: sin el modelo de decisión todo sigue como antes, y el LLM no ve los campos internos")

# 8) Si el LLM daba el caso por cerrado con la respuesta vaga, no se cierra: se repregunta
tmp = preparar()
caso = json.loads(tmp.read_text(encoding="utf-8"))
for campo, datos in caso.items():
    if isinstance(datos, dict) and campo not in ("volumen_de_datos", "evidencias"):
        datos["value"] = "dato de prueba"
tmp.write_text(json.dumps(caso, ensure_ascii=False), encoding="utf-8")


def llm_cierra(**kwargs):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
        {"updates": [VAGO[0]], "assistant_reply": "¡Listo, ya lo tengo todo!", "campo_preguntado": None,
         "update_status": "completed"}, ensure_ascii=False)))])


discovery.litellm.completion = llm_cierra
simular_modelo("vaga")
respuesta = turno("Bastantes.")
caso = json.loads(tmp.read_text(encoding="utf-8"))
assert caso["status"] != "completed" and respuesta == discovery.REPREGUNTA["es"] + PREGUNTA, (caso["status"], respuesta)
print("OK 8: si el LLM cerraba el caso con la respuesta vaga, el caso sigue abierto y se repregunta")

print("\nTodo bien: el fallo 68 está arreglado.")
