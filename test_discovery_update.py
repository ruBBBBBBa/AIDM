"""Comprueba el arreglo del fallo 1: un cambio inválido no debe anular los demás.

No llama a ningún LLM ni toca tu problem_case.json: trabaja sobre una copia
temporal de la plantilla vacía.

Uso (desde la carpeta del proyecto, con el venv activado):
    python test_discovery_update.py
"""
import json
import shutil
import tempfile
from pathlib import Path

import utils
from agents.discovery_agent_logic import _apply_plan

PLANTILLA = Path("data_structures/problem_case_recovery.json")


def caso_temporal() -> Path:
    tmp = Path(tempfile.mkdtemp()) / "problem_case.json"
    shutil.copy(PLANTILLA, tmp)
    utils.PROBLEM_CASE_PATH = tmp  # la tool lee y escribe aquí, no en el caso real
    return tmp


def leer(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# 1) El caso de la revisión: 3 campos, uno viene como lista en un campo de texto
path = caso_temporal()
resultado = _apply_plan({
    "updates": [
        {"field_key": "nombre_del_cliente", "value": "FarmaNorte", "operation": "replace"},
        {"field_key": "sector", "value": "distribución farmacéutica", "operation": "replace"},
        {"field_key": "stack_tecnologico_actual", "value": ["SAP Business One", "Salesforce"], "operation": "replace"},
    ],
    "update_status": None,
})
caso = leer(path)
assert caso["nombre_del_cliente"]["value"] == "FarmaNorte", "no se guardó el cliente"
assert caso["sector"]["value"] == "distribución farmacéutica", "no se guardó el sector"
assert caso["stack_tecnologico_actual"]["value"] == "", "el stack inválido no debería guardarse"
assert resultado["updated_fields"] == ["nombre_del_cliente", "sector"]
assert [r["field_key"] for r in resultado["rejected"]] == ["stack_tecnologico_actual"]
print("OK 1: se guardan los 2 campos buenos y se descarta el stack")

# 2) Un value con formato imposible (un número) tampoco tumba el resto
path = caso_temporal()
_apply_plan({
    "updates": [
        {"field_key": "volumen_de_datos", "value": 2, "operation": "replace"},
        {"field_key": "cloud_provider", "value": "Azure", "operation": "replace"},
    ],
    "update_status": None,
})
caso = leer(path)
assert caso["cloud_provider"]["value"] == "Azure", "no se guardó el cloud"
assert caso["volumen_de_datos"]["value"] == "", "el número no debería guardarse"
print("OK 2: un value con formato no válido se descarta sin perder los demás")

# 3) Un campo que no existe se descarta y 'evidencias' sigue funcionando con merge
path = caso_temporal()
_apply_plan({
    "updates": [
        {"field_key": "campo_inventado", "value": "x", "operation": "replace"},
        {"field_key": "evidencias", "value": ["Cliente: FarmaNorte", "Cliente: FarmaNorte"], "operation": "merge"},
    ],
    "update_status": None,
})
caso = leer(path)
assert caso["evidencias"]["value"] == ["Cliente: FarmaNorte"], "merge no debería duplicar"
print("OK 3: campo inexistente descartado y merge sin duplicados")

print("\nTodo bien: el fallo 1 está arreglado.")