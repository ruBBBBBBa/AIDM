"""Comprueba el arreglo del fallo 33: el discovery guarda las respuestas con su sentido.

En la prueba del 15 de octubre, «No hay equipo de datos» se guardó como «Confirmado por el
usuario: no aplica», el tipo de servicio («que lo implementéis y luego forméis al equipo») acabó
solo en evidencias, y «2 saben SQL», «algo de Azure» y «15 licencias Pro» se perdieron.

El arreglo está en el prompt y en las descripciones de los campos. Este test comprueba que llegan
al modelo y que una respuesta negativa se guarda como dato. Si el modelo las sigue, solo se ve en
una ejecución real.

No llama a ningún LLM (se simula su respuesta) ni toca tu problem_case.json.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_discovery_respuestas.py
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
from types import SimpleNamespace

import utils
import agents.discovery_agent_logic as discovery
from utils import es_valor_de_relleno

PLANTILLA = Path("data_structures/problem_case_recovery.json")
CAMPOS = {
    "nombre_del_cliente", "tamaño_empresa", "sector", "tiene_equipo_datos", "tipo_de_servicio",
    "objetivo_del_proyecto", "modelo_de_despliegue", "cloud_provider", "tipo_de_arquitectura",
    "herramienta_de_visualizacion", "enfoque_ai", "integration_scope", "volumen_de_datos",
    "madurez_analitica_actual", "tamano_equipo_tecnico", "stack_tecnologico_actual",
    "restriccion_presupuestaria", "plazo_objetivo", "regulacion_y_compliance", "kpis_de_exito", "evidencias",
}

# 1) La plantilla sigue igual salvo las descripciones nuevas
plantilla = json.loads(PLANTILLA.read_text(encoding="utf-8"))
assert set(plantilla) - {"status"} == CAMPOS, "la plantilla debe tener los mismos campos que antes"
assert all(not v["value"] for k, v in plantilla.items() if k != "status"), "la plantilla debe estar vacía"
desc = {k: v["description"] for k, v in plantilla.items() if k != "status"}
assert "implementación" in desc["tipo_de_servicio"] and "formación" in desc["tipo_de_servicio"]
assert "no tenemos" in desc["tiene_equipo_datos"]
assert "conocimientos" in desc["tamano_equipo_tecnico"]
assert "aunque las use poco" in desc["stack_tecnologico_actual"]
assert "licencias" in desc["herramienta_de_visualizacion"]
print("OK 1: la plantilla tiene los mismos campos, vacíos, con las 5 descripciones nuevas")

# 2) Una respuesta negativa es un dato, no un relleno
for negativa in ("No tiene equipo de datos", "Sin proveedores externos", "No tenemos CRM", "Ninguno"):
    assert not es_valor_de_relleno(negativa), f"«{negativa}» debe contar como respuesta"
assert es_valor_de_relleno("No mencionado"), "los rellenos siguen sin valer"
print("OK 2: «No tiene equipo de datos» cuenta como respuesta; «No mencionado» sigue sin valer")

# 3) El turno real del 15 de octubre, con el LLM simulado respondiendo como pide el prompt nuevo
RESPUESTA_REAL = ("No hay equipo de datos. IT son 8 personas internas y 2 saben SQL. Sin proveedores externos. "
                  "Queremos que lo implementéis y luego forméis al equipo.")

tmp = Path(tempfile.mkdtemp()) / "problem_case.json"
shutil.copy(PLANTILLA, tmp)
utils.PROBLEM_CASE_PATH = tmp  # la tool lee y escribe aquí, no en tu caso real

mensajes_enviados = []
respuesta_llm = {
    "updates": [
        {"field_key": "tiene_equipo_datos", "operation": "replace",
         "value": "No tiene equipo de datos; sin proveedores externos"},
        {"field_key": "tamano_equipo_tecnico", "operation": "replace", "value": "8 personas de IT interno, 2 saben SQL"},
        {"field_key": "tipo_de_servicio", "operation": "replace",
         "value": "Que Inetum lo implemente y después forme al equipo interno"},
        {"field_key": "evidencias", "operation": "merge", "value": ["IT son 8 personas internas y 2 saben SQL"]},
    ],
    "assistant_reply": "¿Dónde queréis desplegar la solución?",
    "update_status": "pending fields",
}

def llm_simulado(**kwargs):
    mensajes_enviados.append(kwargs["messages"])
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps(respuesta_llm, ensure_ascii=False)))])

os.environ["BIG_MODEL"] = "simulado/modelo"
discovery.litellm.completion = llm_simulado
historial = [{"role": "assistant", "content": "¿Tenéis equipo de datos?"}, {"role": "user", "content": RESPUESTA_REAL}]
discovery.run_agent(RESPUESTA_REAL, chat_history=historial)

sistema, usuario = mensajes_enviados[0][0]["content"], mensajes_enviados[0][1]["content"]
assert "Una respuesta negativa SÍ es un dato" in sistema
assert "aunque no sea el campo por el que preguntaste" in sistema
assert "sin cambiarles el sentido" in sistema
assert "tiene_equipo_datos='No tiene equipo de datos'" in sistema, "falta el ejemplo"
assert desc["tipo_de_servicio"] in usuario, "el modelo debe ver la descripción nueva del tipo de servicio"
print("OK 3: el prompt lleva las reglas nuevas y el ejemplo, y el modelo ve las descripciones nuevas")

caso = json.loads(tmp.read_text(encoding="utf-8"))
assert caso["tiene_equipo_datos"]["value"] == "No tiene equipo de datos; sin proveedores externos"
assert caso["tamano_equipo_tecnico"]["value"] == "8 personas de IT interno, 2 saben SQL"
assert caso["tipo_de_servicio"]["value"] == "Que Inetum lo implemente y después forme al equipo interno"
pendientes = " ".join(discovery._get_pending_fields(caso))
for campo in ("tiene_equipo_datos", "tamano_equipo_tecnico", "tipo_de_servicio"):
    assert campo not in pendientes, f"{campo} no debe quedar pendiente"
print("OK 4: la respuesta negativa, las 2 personas con SQL y el tipo de servicio se guardan en su campo")

print("\nTodo bien: el fallo 33 está arreglado (en la parte que se puede comprobar sin el LLM real).")