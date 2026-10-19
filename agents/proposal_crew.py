from crewai import Crew, Process, Task
from pydantic import BaseModel

from utils import _make_agent, _recent_chat_transcript, load_agents_config, contexto_cliente


class ProposalResult(BaseModel):
    architecture: str
    governance: str
    data_ai_strategy: str
    bi_visualization: str

    def as_combined_context(self) -> str:
        sections = [
            ("Propuesta de Arquitectura", self.architecture),
            ("Propuesta de Gobernanza de Dato e IA", self.governance),
            ("Propuesta de Estrategia de Datos e IA", self.data_ai_strategy),
            ("Propuesta de Visualización de Datos", self.bi_visualization),
        ]
        return "\n\n".join(
            f" {title}\n{body.strip()}" for title, body in sections
        ).strip()


def run_proposal_crew(
    structure_proposal: str,
    problem_case: dict,
    chat_history: list[dict[str, str]] | None,
    model_name: str,
) -> ProposalResult:
    agents_config = load_agents_config()
    transcript = _recent_chat_transcript(chat_history)
    contexto = contexto_cliente(problem_case)  # los mismos datos, con su estado, para los 4 agentes

    architect = _make_agent("structure_proposal_agent", agents_config, model_name)
    data_governance = _make_agent("data_governance_agent", agents_config, model_name)
    data_ai_strategist = _make_agent("data_ai_strategist", agents_config, model_name)
    bi_visualization_specialist = _make_agent("bi_visualization_specialist", agents_config, model_name)

    architecture_task = Task(
        description=(
            "A partir de la propuesta de la solucion a un proyecto y la propia descripcion del problema, analiza el caso, detecta restricciones y objetivos, y propon una arquitectura coherente. "
            "Recomienda plataformas/servicios, justifica cada decisión y explicita supuestos cuando falten datos. "
            "Tambien dispones de el historial de el chat para que tengas contexto. "
            f"- Propuesta de solución:\n{structure_proposal}\n"
            f"{contexto}"
            f"- Historial reciente:\n{transcript}\n"
            "Procura que la respuesta sea detallada y este bien redactada."
        ),
        expected_output="Una propuesta de arquitectura detallada, extensa y justificada, con la siguiente estructura: Arquitectura Propuesta:\n (...). Sin dibujar ninguna tabla ni gráfico, solo texto.",
        agent=architect,
    )

    governance_task = Task(
        description=(
            "Analiza el contexto completo del proyecto a partir de la descripción del problema de negocio y la propuesta de solución planteada. "
            "A partir de esta información, diseña una propuesta de gobernanza del dato y de inteligencia artificial adaptada al proyecto. "
            "Tambien dispones de el historial de el chat para que tengas contexto. "
            f"- Propuesta de solución:\n{structure_proposal}\n"
            f"{contexto}"
            f"- Historial reciente:\n{transcript}\n"
        ),
        expected_output="Texto estructurado que describa la propuesta de gobernanza del dato y AI para el proyecto, incluyendo secciones claras, explicaciones comprensibles y justificación de las decisiones tomadas. Sin dibujar ninguna tabla ni gráfico, solo texto.",
        agent=data_governance,
    )

    data_ai_task = Task(
        description=(
            "Toma estos inputs como contexto: "
            f"{contexto}"
            f"estructura propuesta: {structure_proposal}\n"
            "Basándote en el 'problem case' completo y en la propuesta de arquitectura realizada para el problema, realiza lo siguiente: "
            "1. Análisis Crítico: Evalúa el problema de negocio y la madurez de datos actual. "
            "2. Propuesta de Funcionalidades: Define al menos 3 funcionalidades clave de IA/Data "
            "(ej. Modelos predictivos, RAG con LLMs, Optimización, etc.) que resuelvan el problema. "
            "3. Roadmap Técnico: Por cada funcionalidad, detalla qué fuentes de datos usará, "
            "qué tipo de análisis se aplicará y cómo se integrará en el flujo de trabajo. "
            "4. Valor de Negocio: Explica cómo estas soluciones impactarán en las métricas "
            "de éxito (success_metrics) definidas en el contexto."
        ),
        expected_output="Texto estructurado que describa la propuesta de estrategia de datos e AI para el proyecto, incluyendo secciones claras, explicaciones comprensibles y justificación de las decisiones tomadas. Sin dibujar ninguna tabla ni gráfico, solo texto.",
        agent=data_ai_strategist,
    )

    bi_visualization_task = Task(
        description=(
            "Toma estos inputs como contexto: "
            f"{contexto}"
            f"estructura propuesta: {structure_proposal}\n"
            "Basándote en el 'Client Context' y las soluciones de IA propuestas anteriormente, desarrolla la estrategia de BI: "
            "1. Definición de KPIs de Visualización: Traduce las 'success_metrics' en indicadores "
            "visuales concretos (ej. Gauge charts para cumplimiento de objetivos, Heatmaps de ventas). "
            "2. Diseño de Dashboards: Propón la estructura de al menos 2 dashboards (ej. Dashboard "
            "Ejecutivo y Dashboard de Operaciones Detallado). "
            "3. User Experience (UX): Especifica quiénes son los 'data_consumers' y cómo deben "
            "interactuar con la información (filtros, drill-downs, alertas). "
            "4. Stack de BI: Recomienda la herramienta ideal (Power BI, Tableau, Looker, Streamlit) "
            "según las 'technology_constraints' del cliente."
        ),
        expected_output="Texto estructurado que describa la propuesta de estrategia de visualización de datos para el proyecto, incluyendo secciones claras, explicaciones comprensibles y justificación de las decisiones tomadas. Sin dibujar ninguna tabla ni gráfico, solo texto.",
        agent=bi_visualization_specialist,
    )

    crew = Crew(
        agents=[architect, data_governance, data_ai_strategist, bi_visualization_specialist],
        tasks=[architecture_task, governance_task, data_ai_task, bi_visualization_task],
        process=Process.sequential,
        cache=False,
    )
    result = crew.kickoff()

    def _extract(task) -> str:
        obj = getattr(task, "output", None)
        return getattr(obj, "raw", "") or str(obj or "")

    architecture = _extract(architecture_task)
    governance = _extract(governance_task)
    data_ai_strategy = _extract(data_ai_task)
    bi_visualization = _extract(bi_visualization_task)

    if not any([architecture, governance, data_ai_strategy, bi_visualization]):
        raise RuntimeError(getattr(result, "raw", "Error: proposal crew produjo outputs vacíos."))

    return ProposalResult(
        architecture=architecture,
        governance=governance,
        data_ai_strategy=data_ai_strategy,
        bi_visualization=bi_visualization,
    )
