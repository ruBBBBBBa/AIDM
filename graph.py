from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypedDict
from langgraph.graph import END, START, StateGraph

from agents.discovery_agent_logic import run_agent
from agents.plataforma import elegir_plataforma
from agents.structure_proposal_agent_logic import build_structure_proposal
from agents.solution_builder_crew import propose_solution
from agents.roadmap_builder import leer_fases, roadmap_builder
from agents.resumen_secciones import completar_resumen, resumir_propuesta

from utils import PLATAFORMA_OBJETIVO, load_problem_case

import gateway
import progreso

# Fallo 94: los pasos de la barra de progreso, de la plataforma al informe: plataforma, estructura, propuesta, costes,
# perfiles y equipo, resumen y las 7 fases del roadmap. Fallo 132: y las 2 llamadas del resumidor
PASOS_DEL_INFORME = 15

class AgentState(TypedDict):
    user_prompt: str
    chat_history: list[dict[str, str]]
    assistant_response: str
    problem_case: dict[str, Any]
    completion_reached: bool
    problem_solution: dict[str, Any]
    roadmap:str
    fases_roadmap: dict[str, Any] | None  # fallo 131: las del bloque FASES DEL ROADMAP de la estructura


def discovery_node(state: AgentState) -> AgentState:
    prompt = state.get("user_prompt", "")
    history = state.get("chat_history", [])
    assistant_response = run_agent(prompt, chat_history=history)
    
    # Recargamos el JSON tras ejecutar el agente para rutear con el estado actualizado.
    problem_case = load_problem_case()
    current_status = problem_case.get("status")
    # Activar fase de solución si el status es "completed", independientemente de si ya lo era al entrar.
    completion_reached = current_status == "completed"
    
    return {
        **state,
        "assistant_response": assistant_response,
        "problem_case": problem_case,
        "completion_reached": completion_reached,
    }

# Fallo 72: run_graph() pone aquí la función de la interfaz que escribe en el chat (None fuera de Streamlit)
_avisar = None


def plataforma_node(state: AgentState) -> AgentState:
    """Fallo 67: después del discovery se elige el hiperescalar; su nube manda en todo el informe.
    Fallo 72: lo que ha decidido sale en el chat antes de que empiecen los agentes del informe."""
    progreso.pintar_pendiente()  # fallo 94: la barra sale en 0 en cuanto empieza el informe
    gateway.escribir_cupos("Al empezar el informe")  # fallo 92: cuántas peticiones de Gemini quedan hoy
    progreso.pintar_pendiente()  # fallo 94: la barra sale en 0 en cuanto empieza el informe
    problem_case = elegir_plataforma(state.get("problem_case", {}))
    progreso.avanzar("Plataforma elegida")
    mensaje = (problem_case.get(PLATAFORMA_OBJETIVO) or {}).get("mensaje")
    if _avisar and mensaje:
        try:
            _avisar(mensaje)
        except Exception as exc:  # el mensaje es informativo: si falla, el informe sigue
            print(f"[plataforma] No se pudo escribir el mensaje en el chat: {type(exc).__name__}: {exc}")
    return {**state, "problem_case": problem_case}


def structure_proposal_node(state: AgentState) -> AgentState:
    print("DISCOVERY COMPLETADO\n\n\n")
    problem_case = state.get("problem_case", {})
    chat_history = state.get("chat_history", [])
    
    structure_proposal = build_structure_proposal(problem_case, chat_history)
    print("STRUCTURE PROPOSAL COMPLETADO\n\n\n")
    progreso.avanzar("Estructura de la solución")  # fallo 94
    # Fallo 131: el bloque FASES DEL ROADMAP va al roadmap; la propuesta recibe la estructura sin él y, si el cliente
    # ya tiene alguna fase, la línea «Ya implantado en el cliente…» (el stack del caso valida cada «ya lo tiene»)
    structure_proposal, fases = leer_fases(structure_proposal, problem_case)

    return {
        **state,
        "assistant_response": structure_proposal,
        "fases_roadmap": fases,
    }

def solution_building_node(state: AgentState) -> AgentState:
    problem_case = state.get("problem_case", {})
    chat_history = state.get("chat_history", [])
    structure_proposal = state.get("assistant_response", "")

    solution_proposal = propose_solution(structure_proposal, problem_case, chat_history)
    print("SOLUTION PROPOSAL COMPLETADO\n\n\n")

    return {
        **state,
        "assistant_response": solution_proposal,
        "problem_solution": solution_proposal,
    }

def roadmap_node(state: AgentState) -> AgentState:
    problem_solution = state.get("problem_solution")

    # Fallo 132: la llamada 1 del resumidor (las secciones de la propuesta) va en un hilo, a la vez que el roadmap
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="resumen") as hilo:
        resumen_propuesta = hilo.submit(resumir_propuesta, state.get("problem_case"))
        roadmap_response = roadmap_builder(problem_solution, state.get("problem_case"),
                                           fases=state.get("fases_roadmap"))  # fallo 131
        progreso.esperar([resumen_propuesta])
    print("ROADMAP COMPLETADO\n\n\n")
    # Fallo 132: la llamada 2 (general, puntos clave, fases y CRISP-DM) y la pestaña Resumen del informe
    completar_resumen(resumen_propuesta.result(), problem_solution, state.get("problem_case"),
                      state.get("fases_roadmap"))

    return {
        **state,
        "assistant_response": roadmap_response,
        "roadmap": roadmap_response,
    }

def route_after_discovery(state: AgentState) -> str:
    """
    Si este turno ha completado el discovery, continúa con la fase de solución.
    En cualquier otro caso termina — la interfaz espera el siguiente mensaje.
    """
    if state.get("completion_reached"):
        return "plataforma"
    return END


def build_graph():
    builder = StateGraph(AgentState)
    
    builder.add_node("discovery", discovery_node)
    builder.add_node("plataforma", plataforma_node)
    builder.add_node("structure_proposal", structure_proposal_node)
    builder.add_node("solution_building", solution_building_node)
    builder.add_node("roadmap", roadmap_node)

    builder.add_edge(START, "discovery")
    builder.add_conditional_edges("discovery", route_after_discovery)
    builder.add_edge("plataforma", "structure_proposal")
    builder.add_edge("structure_proposal", "solution_building")
    builder.add_edge("solution_building", "roadmap")
    builder.add_edge("roadmap", END)

    return builder.compile()


aidm_graph = build_graph()

# Agent run function


def run_graph(user_prompt: str, chat_history: list[dict[str, str]] | None = None, avisar=None,
              pintar_progreso=None) -> dict:
    """avisar (fallo 72): función que recibe un texto y lo escribe en el chat mientras sigue el grafo."""
    global _avisar
    _avisar = avisar
    initial_state: AgentState = {
        "user_prompt": user_prompt,
        "chat_history": chat_history or [],
        "assistant_response": "",
        "problem_case": load_problem_case(),
        "completion_reached": False,
        "roadmap": "",
    }
    # Fallo 94: pintar_progreso(fraccion, texto) pinta la barra; solo se llama desde este hilo (el de la sesión)
    if pintar_progreso:
        progreso.empezar(pintar_progreso, PASOS_DEL_INFORME, "Eligiendo la plataforma…")
    try:
        resultado = aidm_graph.invoke(initial_state)
    finally:
        _avisar = None
        progreso.terminar()
    if resultado.get("completion_reached"):
        gateway.resumen_consumo()  # lo que ha gastado la ejecución, al acabar el informe
    return resultado
