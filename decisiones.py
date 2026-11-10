"""Modelo de decisión de AIDM: preguntas cerradas que contesta un modelo ajustado con Unsloth, no un LLM.

El modelo se entrena con modelo_decision/entrenar_decision.ipynb y se sirve con llama-server (llama.cpp, en CPU) en
el mismo ordenador que AIDM (modelo_decision/README.md). AIDM lo llama por HTTP, en /v1/systemone, como a cualquier API.

En el .env:
    AIDM_DECISION_URL=http://127.0.0.1:8090
Con un Space de Hugging Face (modelo_decision/space, necesita PRO), la URL del Space y además HF_TOKEN=hf_...

Sin AIDM_DECISION_URL, o si no responde, las funciones devuelven None y AIDM sigue como antes.
"""
import logging
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv(".env")
logger = logging.getLogger("aidm")

# En CPU tarda unos 4 s. Si no contesta a tiempo (llama-server cerrado o un Space dormido), el turno sigue sin el
# modelo y no se le vuelve a llamar hasta pasada la pausa, para no frenar cada turno del discovery.
TIEMPO_MAXIMO = 30  # s
PAUSA_TRAS_FALLO = 300  # s
CONFIANZA_MINIMA = 0.7
_sin_respuesta_hasta = 0.0

# La pregunta con la que se entrena el modelo (modelo_decision/generar_datos.py la importa de aquí).
# Si cambian las instrucciones o las opciones, hay que volver a generar los datos y reentrenar.
PREGUNTA_ESTADO = {
    "type": "choice",
    "instructions": "¿Qué hace la respuesta del cliente con el dato por el que se le pregunta?",
    "criteria": {
        "dato": "Da el dato o parte de él, aunque sea aproximado o negativo (por ejemplo «no tenemos», "
                "«ninguno» o «solo usamos Excel»).",
        "no_lo_sabe": "Dice que no lo sabe, que lo desconoce o que no está seguro, sin dar ningún valor.",
        "no_aplica": "Dice que ese tema no aplica a su proyecto, que no lo necesita o que no es relevante para él.",
        "no_responde": "No contesta a lo que se le pregunta: cambia de tema, da otro dato, pregunta otra cosa "
                       "o lo deja para más tarde.",
        # Fallo 68: la respuesta habla del dato pero no sirve para estimar nada; el discovery vuelve a preguntar
        "vaga": "Habla del dato pero sin concretar nada que sirva para estimarlo: ni una cifra, ni un rango, ni un "
                "nombre (por ejemplo «bastantes», «lo normal», «depende» o «un poco de todo»).",
    },
}



# Fallo 69: qué dice el cliente de una herramienta o una nube que nombra. El agente de plataforma lo pregunta solo
# cuando cerca hay una negación o un cambio («no usan Power BI», «dejaron Oracle»): sin eso, nombrarla es usarla.
CRITERIOS_USO = {
    "la_usa": "La empresa la usa hoy, aunque sea poco o solo en una parte.",
    "quiere_usarla": "Todavía no la usa, pero quiere usarla o la prefiere para este proyecto.",
    "no_la_usa": "Dice que no la usa, que no la tiene o que no la quiere.",
    "la_deja": "La usaba o la usa todavía, pero la ha dejado o la va a dejar.",
}
USOS_POR_LLAMADA = 4  # como en el entrenamiento: hasta 4 herramientas por texto


def pregunta_uso(nombre: str) -> dict:
    """La pregunta por una herramienta o una nube, con su nombre tal como lo escribe el cliente."""
    return {"type": "choice", "instructions": f"¿Qué dice el cliente de {nombre}?", "criteria": CRITERIOS_USO}


def texto_uso(campo: str, texto: str) -> str:
    """El estado que lee el modelo para la tarea de uso. Es el mismo texto en el entrenamiento y en AIDM."""
    return f"Lo que ha dicho el cliente ({campo}): {(texto or '').strip()[:600]}"


def texto_estado(campo: str, descripcion: str, pregunta: str, respuesta: str) -> str:
    """El estado que lee el modelo. Es el mismo texto en el entrenamiento y en AIDM."""
    return (
        f"Dato que se pregunta: {campo} ({(descripcion or '').strip()})\n"
        f"Pregunta del asistente: {(pregunta or '').strip()[-600:]}\n"
        f"Respuesta del cliente: {(respuesta or '').strip()[:600]}"
    )


def decidir(estado: str, preguntas: dict) -> dict | None:
    """Las respuestas del modelo ({nombre: respuesta}) o None si no está configurado o no responde."""
    global _sin_respuesta_hasta
    url = os.getenv("AIDM_DECISION_URL", "").strip().rstrip("/")
    if not url or time.monotonic() < _sin_respuesta_hasta:
        return None
    cabeceras = {"Content-Type": "application/json"}
    token = os.getenv("HF_TOKEN", "").strip()
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"
    try:
        respuesta = httpx.post(
            f"{url}/v1/systemone",
            json={"state": estado, "questions": preguntas},
            headers=cabeceras,
            timeout=httpx.Timeout(TIEMPO_MAXIMO, connect=10),
        )
        respuesta.raise_for_status()
        respuestas = respuesta.json().get("answers")
    except Exception as exc:
        # Ni el estado ni la respuesta van al log: llevan datos del cliente.
        _sin_respuesta_hasta = time.monotonic() + PAUSA_TRAS_FALLO
        logger.warning("[decisiones] El modelo de decisión no responde (%s); se sigue sin él %d min",
                       type(exc).__name__, PAUSA_TRAS_FALLO // 60)
        return None
    return respuestas if isinstance(respuestas, dict) else None



def _eleccion(decision: dict | None, criterios: dict) -> tuple[str, float] | None:
    """(opción, confianza) de una respuesta del modelo, o None si no es una de las opciones de la pregunta."""
    decision = decision if isinstance(decision, dict) else {}
    eleccion = decision.get("choice")
    if eleccion not in criterios:
        return None
    confianza = decision.get("confidence")
    if not isinstance(confianza, (int, float)):
        confianza = (decision.get("probabilities") or {}).get(eleccion, 0.0)
    return eleccion, float(confianza)


def estado_respuesta(campo: str, descripcion: str, pregunta: str, respuesta: str) -> tuple[str, float] | None:
    """('dato' | 'no_lo_sabe' | 'no_aplica' | 'no_responde' | 'vaga', confianza) o None si el modelo no está
    disponible."""
    inicio = time.monotonic()
    respuestas = decidir(texto_estado(campo, descripcion, pregunta, respuesta), {"estado": PREGUNTA_ESTADO})
    resultado = _eleccion((respuestas or {}).get("estado"), PREGUNTA_ESTADO["criteria"])
    if resultado:
        print(f"[decisiones] estado_respuesta · {campo} · {resultado[0]} ({resultado[1]:.2f}) · "
              f"{time.monotonic() - inicio:.1f} s")
    return resultado



def usos(campo: str, texto: str, nombres: list[str]) -> dict[str, tuple[str, float]] | None:
    """Fallo 69: {nombre: (opción de CRITERIOS_USO, confianza)} de cada herramienta o nube nombrada en el texto de un
    campo, con una pregunta por cada una (hasta USOS_POR_LLAMADA por llamada). None si el modelo no está disponible."""
    resultado = {}
    for desde in range(0, len(nombres), USOS_POR_LLAMADA):
        grupo = nombres[desde:desde + USOS_POR_LLAMADA]
        preguntas = {f"uso_{i}": pregunta_uso(nombre) for i, nombre in enumerate(grupo, 1)}
        inicio = time.monotonic()
        respuestas = decidir(texto_uso(campo, texto), preguntas)
        if respuestas is None:
            return None
        for clave, nombre in zip(preguntas, grupo):
            eleccion = _eleccion(respuestas.get(clave), CRITERIOS_USO)
            if eleccion:
                resultado[nombre] = eleccion
        print(f"[decisiones] usos · {campo} · " + ", ".join(
            f"{n}: {resultado[n][0]} ({resultado[n][1]:.2f})" for n in grupo if n in resultado)
            + f" · {time.monotonic() - inicio:.1f} s")
    return resultado
