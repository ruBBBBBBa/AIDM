"""Comprueba el arreglo del fallo 35: un campo de texto que llega como lista se une en un texto.

En la ejecución real del 14 de octubre, el modelo mandó objetivo_del_proyecto como una lista
de dos frases y el cambio se descartó entero. Fue el único dato descartado en todo el discovery.

No llama a ningún LLM ni toca tu problem_case.json: trabaja sobre una copia de la plantilla vacía.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_discovery_listas.py
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

import utils
from agents.discovery_agent_logic import _apply_plan
from utils import _normalize_update_value

# El valor exacto que mandó el modelo el 14 de octubre
OBJETIVO_REAL = [
    "Unificar datos de flota, rutas y entregas en una plataforma centralizada",
    "Implementar modelo predictivo para identificar entregas con riesgo de retraso",
]

# 1) La lista se une en un texto, con «; » entre frases
valor = _normalize_update_value("objetivo_del_proyecto", OBJETIVO_REAL, "replace")
assert valor == ("Unificar datos de flota, rutas y entregas en una plataforma centralizada; "
                 "Implementar modelo predictivo para identificar entregas con riesgo de retraso"), valor
print("OK 1: el objetivo del 14 oct se guarda como un texto con las dos frases")

# 2) Se limpian espacios y elementos vacíos
assert _normalize_update_value("sector", ["  logística ", "", "   "], "replace") == "logística"
print("OK 2: se quitan espacios y elementos vacíos")

# 3) Lo que no es un dato se sigue rechazando aunque llegue como lista
for lista in (["No mencionado"], [], ["", "  "]):
    try:
        _normalize_update_value("objetivo_del_proyecto", lista, "replace")
    except ValueError:
        continue
    raise AssertionError(f"{lista!r} no debería guardarse")
print("OK 3: una lista vacía o con un relleno tipo «No mencionado» se sigue descartando")

# 4) El turno completo: el objetivo ya no se pierde
tmp = Path(tempfile.mkdtemp()) / "problem_case.json"
shutil.copy("data_structures/problem_case_recovery.json", tmp)
utils.PROBLEM_CASE_PATH = tmp  # la tool lee y escribe aquí, no en tu caso real
resultado = _apply_plan({
    "updates": [
        {"field_key": "nombre_del_cliente", "value": "Transportes Cantábrico", "operation": "replace"},
        {"field_key": "objetivo_del_proyecto", "value": OBJETIVO_REAL, "operation": "replace"},
    ],
    "update_status": None,
})
caso = json.loads(tmp.read_text(encoding="utf-8"))
assert resultado["rejected"] == [], resultado["rejected"]
assert caso["objetivo_del_proyecto"]["value"].startswith("Unificar datos de flota")
print("OK 4: en el turno completo no se descarta nada y el objetivo queda guardado")

print("\nTodo bien: el fallo 35 está arreglado.")