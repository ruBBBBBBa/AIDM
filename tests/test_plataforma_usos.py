"""Comprueba el fallo 69: lo que el cliente nombra para decir que no lo usa (o que lo deja) no suma en la plataforma.

Antes, el agente de plataforma contaba cada herramienta o nube que salía en el caso: «no usan Power BI ni Microsoft
365» sumaba al ecosistema Microsoft. Ahora, en los campos con una negación o un cambio cerca («no», «ni», «dejaron»,
«migrando desde»…), pregunta al modelo de decisión qué dice el cliente de cada una; si no la usa o la deja, no cuenta.

No llama a ningún LLM ni al modelo de decisión (se simula) y no toca tu caso.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_plataforma_usos.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import json

import decisiones
from agents import plataforma

PLANTILLA = json.loads(Path("data_structures/problem_case_recovery.json").read_text(encoding="utf-8"))
CONFIG = plataforma._config()


def caso(**campos) -> dict:
    nuevo = json.loads(json.dumps(PLANTILLA))
    for campo, valor in campos.items():
        nuevo[campo]["value"] = valor
    return nuevo


def simular(respuestas: dict[str, tuple[str, float]] | None):
    """El modelo contesta a cada herramienta por su nombre; guarda lo que se le pregunta."""
    llamadas = []

    def decidir(estado, preguntas):
        llamadas.append((estado, {k: v["instructions"] for k, v in preguntas.items()}))
        if respuestas is None:
            return None
        nombre = lambda p: p["instructions"].removeprefix("¿Qué dice el cliente de ").removesuffix("?")
        return {clave: {"choice": respuestas.get(nombre(p), ("la_usa", 0.9))[0],
                        "confidence": respuestas.get(nombre(p), ("la_usa", 0.9))[1]}
                for clave, p in preguntas.items()}
    decisiones.decidir = decidir
    return llamadas


SIN_POWER_BI = caso(cloud_provider="AWS",
                    stack_tecnologico_actual="Python y Excel; no usan Power BI ni Microsoft 365")

# 1) «no usan Power BI ni Microsoft 365»: se pregunta por las tres en una llamada y solo Excel cuenta
llamadas = simular({"Power BI": ("no_la_usa", 0.93), "Microsoft 365": ("no_la_usa", 0.88)})
s = plataforma.senales(SIN_POWER_BI, CONFIG)
assert s["ecosistemas"]["microsoft"] == ["excel"], s["ecosistemas"]
assert s["no_cuentan"] == {"Power BI": "no_la_usa", "Microsoft 365": "no_la_usa"}, s["no_cuentan"]
assert len(llamadas) == 1, llamadas
estado, preguntas = llamadas[0]
assert estado == decisiones.texto_uso("stack_tecnologico_actual", "Python y Excel; no usan Power BI ni Microsoft 365")
assert sorted(preguntas.values()) == sorted(f"¿Qué dice el cliente de {n}?" for n in ("Excel", "Power BI", "Microsoft 365"))
assert set(preguntas) == {"uso_1", "uso_2", "uso_3"}
print("OK 1: «no usan Power BI ni Microsoft 365»: una llamada con las 3 herramientas, como las escribe; solo cuenta Excel")

# 2) Antes del arreglo (o sin el modelo de decisión), las tres contaban
simular(None)
s = plataforma.senales(SIN_POWER_BI, CONFIG)
assert sorted(s["ecosistemas"]["microsoft"]) == ["excel", "microsoft 365", "power bi"] and not s["no_cuentan"]
print("OK 2: sin el modelo de decisión, se cuenta todo lo nombrado, como antes")

# 3) Un campo sin negación ni cambio no se pregunta: nombrarla es usarla
llamadas = simular({})
s = plataforma.senales(caso(stack_tecnologico_actual="Usamos Power BI y SAP en toda la empresa"), CONFIG)
assert not llamadas and set(s["ecosistemas"]) == {"microsoft", "sap"}, (llamadas, s["ecosistemas"])
print("OK 3: «Usamos Power BI y SAP» no pasa por el modelo: las dos cuentan")

# 4) Una nube que el cliente está dejando no da continuidad, y la sección lo dice
DEJA_AWS = caso(cloud_provider="Estamos dejando AWS; la idea es pasar a Azure",
                stack_tecnologico_actual="Microsoft 365")
simular({"AWS": ("la_deja", 0.9), "Azure": ("quiere_usarla", 0.85)})
r = plataforma.puntuar(DEJA_AWS, CONFIG)
assert r["senales"]["actuales"] == ["azure"] and r["senales"]["dejando"] == ["aws"], r["senales"]
assert r["recomendado"] == "azure" and r["decision"] == "mantener", r
texto = plataforma.resumen(r, CONFIG)
assert "**No cuenta, aunque el cliente lo nombra:** AWS (la ha dejado o la va a dejar)." in texto, texto
print("OK 4: «Estamos dejando AWS; la idea es pasar a Azure»: AWS no cuenta, Azure sí, y la sección lo explica")

# 5) Si solo deja una nube y no va a ninguna, la decisión es «elegir» y lo dice
simular({"AWS": ("la_deja", 0.9)})
r = plataforma.puntuar(caso(cloud_provider="Antes usábamos AWS, pero lo quitamos el año pasado"), CONFIG)
assert r["decision"] == "elegir" and r["senales"]["dejando"] == ["aws"], r
assert "El cliente está dejando Amazon Web Services (AWS) y no usa ni prefiere otra de las cinco." in \
    plataforma.resumen(r, CONFIG)
print("OK 5: si deja AWS y no usa otra, la decisión es elegir y la sección dice que la está dejando")

# 6) Con poca confianza, no se quita nada
simular({"Power BI": ("no_la_usa", 0.6), "Microsoft 365": ("no_la_usa", 0.65)})
s = plataforma.senales(SIN_POWER_BI, CONFIG)
assert len(s["ecosistemas"]["microsoft"]) == 3 and not s["no_cuentan"]
print("OK 6: con confianza por debajo de 0,7, todo cuenta como antes")

# 7) Más de 4 herramientas en un campo: varias llamadas de 4 como mucho, como en el entrenamiento
llamadas = simular({"Tableau": ("no_la_usa", 0.9)})
plataforma.senales(caso(stack_tecnologico_actual="SAP, Excel, SharePoint, Teams, Power BI y Databricks; nada de "
                                                 "Google Workspace"), CONFIG)
assert [len(p) for _, p in llamadas] == [4, 3], [len(p) for _, p in llamadas]
print("OK 7: 7 herramientas en un campo con «nada de»: dos llamadas, de 4 y 3 preguntas")

print("\nTodo bien: el fallo 69 está arreglado.")
