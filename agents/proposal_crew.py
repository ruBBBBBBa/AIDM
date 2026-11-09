from crewai import Crew, Process, Task
from pydantic import BaseModel
import gateway

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
    model_name: str | None = None,
) -> ProposalResult:
    agents_config = load_agents_config()
    transcript = _recent_chat_transcript(chat_history)
    contexto = contexto_cliente(problem_case)  # los mismos datos, con su estado, para los 4 agentes

    # Gateway de modelos: cada agente pide el suyo. model_name, si se pasa (lo hacen los tests), vale para los 4
    architect = _make_agent("structure_proposal_agent", agents_config, model_name or gateway.modelo("arquitectura"))
    data_governance = _make_agent("data_governance_agent", agents_config, model_name or gateway.modelo("gobernanza"))
    data_ai_strategist = _make_agent("data_ai_strategist", agents_config, model_name or gateway.modelo("estrategia"))
    bi_visualization_specialist = _make_agent("bi_visualization_specialist", agents_config,
                                              model_name or gateway.modelo("visualizacion"))

    architecture_task = Task(
        description=(
            "A partir de la propuesta de la solucion a un proyecto y la propia descripcion del problema, analiza el caso, detecta restricciones y objetivos, y propon una arquitectura coherente. "
            "Recomienda plataformas/servicios, justifica cada decisión y explicita supuestos cuando falten datos. "
            "Tambien dispones de el historial de el chat para que tengas contexto. "
            f"- Propuesta de solución:\n{structure_proposal}\n"
            f"{contexto}"
            f"- Historial reciente:\n{transcript}\n"
            "Ajusta la propuesta al tamaño del cliente y al alcance que pide: no añadas componentes que el caso no necesita y redactala acordemente"
        ),
        expected_output="Una propuesta de arquitectura justificada y proporcionada al cliente, con la siguiente estructura: Arquitectura Propuesta:\n (...). Sin dibujar ninguna tabla ni gráfico, solo texto.",
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
            "Basándote en los datos del cliente y en la estructura propuesta, realiza lo siguiente: "
            "1. Análisis crítico: evalúa el problema de negocio y la madurez de datos actual (madurez_analitica_actual). "
            "2. Funcionalidades: define las funcionalidades de IA y datos que resuelven lo que pide el cliente "
            "(objetivo_del_proyecto y enfoque_ai), ni más ni menos. Separa lo que cabe en el plazo pedido (plazo_objetivo) "
            "de lo que serían fases posteriores. "
            "3. Roadmap técnico: por cada funcionalidad, qué fuentes de datos usará, qué tipo de análisis se aplicará "
            "y cómo se integrará en el trabajo diario del cliente. "
            "4. Valor de negocio: cómo contribuye cada funcionalidad a los KPIs del cliente (kpis_de_exito). "
            "Si un KPI no se puede medir dentro del plazo pedido, dilo."
        ),
        expected_output="Texto estructurado que describa la propuesta de estrategia de datos e AI para el proyecto, incluyendo secciones claras, explicaciones comprensibles y justificación de las decisiones tomadas. Sin dibujar ninguna tabla ni gráfico, solo texto.",
        agent=data_ai_strategist,
    )

    bi_visualization_task = Task(
        description=(
            "Toma estos inputs como contexto: "
            f"{contexto}"
            f"estructura propuesta: {structure_proposal}\n"
            "Basándote en los datos del cliente y en las soluciones de IA propuestas anteriormente, desarrolla la estrategia de BI: "
            "1. KPIs de visualización: traduce los KPIs del cliente (kpis_de_exito) en indicadores visuales concretos. "
            "2. Dashboards: propón los que necesita el caso, empezando por los que pide el cliente; no añadas otros solo para completar. "
            "3. Usuarios: quién usará cada dashboard y cómo (filtros, drill-downs, alertas). Si el caso no dice quiénes son, "
            "márcalo como «Se asume». "
            "4. Herramienta: si el cliente ya tiene o prefiere una (herramienta_de_visualizacion), úsala y diseña con las "
            "licencias que tiene. Si alguna función necesita licencias que no tiene, dilo como coste por confirmar. "
            "Si no tiene ninguna, recomienda una y explica por qué."
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
