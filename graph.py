from typing import Any, TypedDict
from langgraph.graph import END, START, StateGraph

from agents.discovery_agent_logic import run_agent
from agents.structure_proposal_agent_logic import build_structure_proposal
from agents.solution_builder_crew import propose_solution
from agents.roadmap_builder import roadmap_builder

from utils import load_problem_case

class AgentState(TypedDict):
    user_prompt: str
    chat_history: list[dict[str, str]]
    assistant_response: str
    problem_case: dict[str, Any]
    completion_reached: bool
    problem_solution: dict[str, Any]
    roadmap:str


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

def structure_proposal_node(state: AgentState) -> AgentState:
    print("DISCOVERY COMPLETADO\n\n\n")
    problem_case = state.get("problem_case", {})
    chat_history = state.get("chat_history", [])
    
    structure_proposal = build_structure_proposal(problem_case, chat_history)
    print("STRUCTURE PROPOSAL COMPLETADO\n\n\n")
    
    return {
        **state,
        "assistant_response": structure_proposal,
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

    roadmap_response = roadmap_builder(problem_solution)
    print("ROADMAP COMPLETADO\n\n\n")

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
        return "structure_proposal"
    return END


def build_graph():
    builder = StateGraph(AgentState)
    
    builder.add_node("discovery", discovery_node)
    builder.add_node("structure_proposal", structure_proposal_node)
    builder.add_node("solution_building", solution_building_node)
    builder.add_node("roadmap", roadmap_node)

    builder.add_edge(START, "discovery")
    builder.add_conditional_edges("discovery", route_after_discovery)
    builder.add_edge("structure_proposal", "solution_building")
    builder.add_edge("solution_building", "roadmap")
    builder.add_edge("roadmap", END)

    return builder.compile()


aidm_graph = build_graph()

# Agent run function


def run_graph(user_prompt: str, chat_history: list[dict[str, str]] | None = None) -> dict:
    initial_state: AgentState = {
        "user_prompt": user_prompt,
        "chat_history": chat_history or [],
        "assistant_response": "",
        "problem_case": load_problem_case(),
        "completion_reached": False,
        "roadmap": "",
    }
    return aidm_graph.invoke(initial_state)
