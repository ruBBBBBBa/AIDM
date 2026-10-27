import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from agents.estimation_crew import run_estimation_crew
from agents.proposal_crew import run_proposal_crew
from utils import md_to_html, parse_html, sin_importes

load_dotenv(".env")

SOLUTION_REPORT_PATH = Path("data_structures/solution_builder_report.html")
_SECTION_IDS = {
    "architecture":    "content-architecture",
    "governance":      "content-governance",
    "data_ai":         "content-data-ai",
    "bi":              "content-bi",
    "cost_estimation": "content-cost-estimation",
    "team_profiles":   "content-team-profiles",
}


class SolutionResult(BaseModel):
    architecture: str
    governance: str
    data_ai_strategy: str
    bi_visualization: str
    cost_estimation: str
    team_profiles: str


def _inject_into_html(
    architecture_output: str,
    governance_output: str,
    data_ai_output: str,
    bi_visualization_output: str,
    cost_estimation_output: str,
    team_profiles_output: str,
) -> None:
    """
    Lee el HTML de plantilla existente y reemplaza el contenido de cada
    <div id="..."> con el output renderizado del agente correspondiente.
    El fichero debe existir previamente; esta función nunca lo crea desde cero.
    """
    if not SOLUTION_REPORT_PATH.exists():
        raise FileNotFoundError(
            f"El archivo HTML de plantilla no existe en {SOLUTION_REPORT_PATH}. "
            "Asegúrate de que solution_builder_report.html esté presente antes de ejecutar este módulo."
        )

    html = SOLUTION_REPORT_PATH.read_text(encoding="utf-8")
    soup = parse_html(html)

    replacements = {
        _SECTION_IDS["architecture"]:    md_to_html(architecture_output),
        _SECTION_IDS["governance"]:      md_to_html(governance_output),
        _SECTION_IDS["data_ai"]:         md_to_html(data_ai_output),
        _SECTION_IDS["bi"]:              md_to_html(bi_visualization_output),
        _SECTION_IDS["cost_estimation"]: cost_estimation_output,
        _SECTION_IDS["team_profiles"]:   md_to_html(team_profiles_output),
    }

    for div_id, new_html in replacements.items():
        target = soup.find("div", id=div_id)
        if target is None:
            continue
        target.clear()
        fragment = parse_html(new_html)
        # lxml wraps content in <html><body> — grab only the body children
        body = fragment.find("body") or fragment
        for child in list(body.children):
            target.append(child.__copy__())

    SOLUTION_REPORT_PATH.write_text(str(soup), encoding="utf-8")


def propose_solution(
    structure_proposal: str,
    problem_case: dict,
    chat_history: list[dict[str, str]] | None,
) -> dict:
    model_name = os.getenv("BIG_MODEL", "").strip()
    if not model_name:
        return "No hay modelo configurado en .env (variable BIG_MODEL)."

    try:
        proposal = run_proposal_crew(structure_proposal, problem_case, chat_history, model_name)
         # Fallo 46: la tarea de costes solo necesita los componentes y sus volúmenes, que están en la arquitectura
        estimation = run_estimation_crew(proposal.as_combined_context(), model_name, problem_case=problem_case,
                                         componentes=proposal.architecture)
    except RuntimeError as e:
        return str(e)

    # Fallo 44: los importes salen solo en la sección de costes, donde se suman en Python
    proposal = proposal.model_copy(update={
        campo: sin_importes(getattr(proposal, campo), problem_case, campo)
        for campo in ("architecture", "governance", "data_ai_strategy", "bi_visualization")
    })

    _inject_into_html(
        proposal.architecture,
        proposal.governance,
        proposal.data_ai_strategy,
        proposal.bi_visualization,
        estimation.cost_estimation,
        estimation.team_profiles,
    )

    return SolutionResult(
        **proposal.model_dump(),
        **estimation.model_dump(),
    ).model_dump()
