"""Comprueba que los datos del modelo de decisión (modelo_decision/datos/) están al día con decisiones.py.

El modelo se entrena con las preguntas y los textos de decisiones.py, y AIDM le pregunta con esos mismos. Si alguien
cambia una pregunta, una opción o un texto y no vuelve a generar los datos, el modelo se entrenaría con otra cosa:
este test lo avisa. También comprueba que hay ejemplos de todas las opciones de las tres tareas y que ningún ejemplo
de prueba está en el entrenamiento.

No llama a ningún modelo. Genera los datos en una carpeta temporal y los compara con los del repo.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_datos_decision.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
sys.path.insert(0, str(RAIZ / "modelo_decision"))
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import contextlib
import io
import json
import tempfile

import decisiones
import generar_datos

DATOS = Path("modelo_decision/datos")


def leer(ruta: Path) -> list[dict]:
    return [json.loads(linea) for linea in ruta.read_text(encoding="utf-8").splitlines() if linea.strip()]


# 1) Los datos del repo son los que salen ahora del generador (mismas preguntas y textos que decisiones.py)
generar_datos.SALIDA = Path(tempfile.mkdtemp())
with contextlib.redirect_stdout(io.StringIO()):
    generar_datos.main()
for nombre in ("entrenamiento", "prueba"):
    nuevos = (generar_datos.SALIDA / f"{nombre}.jsonl").read_text(encoding="utf-8")
    assert nuevos == (DATOS / f"{nombre}.jsonl").read_text(encoding="utf-8"), (
        f"modelo_decision/datos/{nombre}.jsonl no está al día: ejecuta python modelo_decision/generar_datos.py "
        "y vuelve a entrenar")
print("OK 1: los datos del repo son los que genera generar_datos.py con el decisiones.py de ahora")

# 2) Cada pregunta de los datos es la que hace AIDM, y hay ejemplos de todas las opciones de cada tarea
entrenamiento, prueba = leer(DATOS / "entrenamiento.jsonl"), leer(DATOS / "prueba.jsonl")
opciones = {"estado": set(decisiones.PREGUNTA_ESTADO["criteria"]), "uso": set(decisiones.CRITERIOS_USO),
            "despliegue": set(decisiones.PREGUNTA_DESPLIEGUE["criteria"])}
for nombre, filas in (("entrenamiento", entrenamiento), ("prueba", prueba)):
    vistas = {tarea: set() for tarea in opciones}
    for f in filas:
        for clave, pregunta in f["questions"].items():
            if f["tarea"] == "estado":
                assert pregunta == decisiones.PREGUNTA_ESTADO
            elif f["tarea"] == "despliegue":
                assert pregunta == decisiones.PREGUNTA_DESPLIEGUE
            else:
                assert pregunta["criteria"] == decisiones.CRITERIOS_USO and clave.startswith("uso_")
            vistas[f["tarea"]].add(f["gold"][clave])
        assert len(f["questions"]) <= decisiones.USOS_POR_LLAMADA
    assert vistas == opciones, (nombre, vistas)
print("OK 2: las preguntas son las de decisiones.py y hay ejemplos de todas las opciones de las 3 tareas")

# 3) Ningún ejemplo de prueba está en el entrenamiento, y todos caben en la ventana del modelo
assert not {f["state"] for f in entrenamiento} & {f["state"] for f in prueba}
largo = max(len(f["state"]) + len(json.dumps(f["questions"], ensure_ascii=False)) for f in entrenamiento + prueba)
assert largo < 2500, f"una fila ocupa {largo} caracteres: puede no caber en max_seq_length=1024"
print(f"OK 3: la prueba no repite el entrenamiento y la fila más larga ocupa {largo} caracteres (cabe en 1.024 tokens)")

# 4) La regla de despliegue: manda lo que obliga, no lo que el cliente dice preferir
regla = generar_datos.despliegue_correcto
assert regla({"prohibe_todo", "saas"}) == "on_premises"
assert regla({"planta", "nube", "pide_nube"}) == "hibrido"
assert regla({"pide_local", "saas"}) == "cloud"
assert regla({"pide_local", "sector"}) == "sin_datos"
print("OK 4: la regla de despliegue de Inetum da on-premises, híbrido, nube o sin datos según lo que obliga")

print("\nTodo bien: los datos del modelo de decisión están al día.")
