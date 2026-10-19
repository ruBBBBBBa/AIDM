import os
from pathlib import Path

from crewai import Agent, Crew, Process, Task
from dotenv import load_dotenv

from utils import _make_agent, contexto_cliente, load_agents_config, load_problem_case, parse_html, md_to_html

load_dotenv(".env")

MODEL_NAME = os.getenv("BIG_MODEL", "").strip()
SECOND_MODEL_NAME = os.getenv("REASONING_MODEL", "").strip()
FIRST_MODEL_NAME = os.getenv("REASONING_RIGOROUS_MODEL", "").strip()
THIRD_MODEL_NAME = os.getenv("SMALL_REASONING_MODEL", "").strip()

_ROADMAP_IDS = [
    "roadmap-sources",
    "roadmap-ingestion",
    "roadmap-storage",
    "roadmap-etl",
    "roadmap-modeling",
    "roadmap-ml",
    "roadmap-bi",
]

REPORT_PATH = Path("data_structures/solution_builder_report.html")

# Las tareas del roadmap no dan por hecha ninguna nube ni herramienta: usan las del cliente o las de la propuesta.
PLATAFORMA = (
    "PLATAFORMA: usa la nube y las herramientas que indica el caso (cloud_provider, modelo_de_despliegue, "
    "herramienta_de_visualizacion, stack_tecnologico_actual) y las que ya eligió la propuesta de arquitectura. "
    "Si el cliente no tiene preferencia, elige tú y justifícalo. Nombra los servicios concretos de esa plataforma; "
    "los ejemplos de esta tarea son solo orientativos.\n\n"
)


def inject_roadmap_into_html(
    data_sources_output: str,
    ingestion_output: str,
    storage_output: str,
    etl_output: str,
    modeling_output: str,
    ml_output: str,
    bi_output: str,
) -> None:
    """Inyecta los 7 outputs individuales del roadmap en sus respectivos divs en el HTML."""
    if not REPORT_PATH.exists():
        raise FileNotFoundError(
            f"El archivo HTML no existe en {REPORT_PATH}. "
            "Asegúrate de que solution_builder_report.html esté presente."
        )

    phase_outputs = [data_sources_output, ingestion_output, storage_output, etl_output, modeling_output, ml_output, bi_output]
    html = REPORT_PATH.read_text(encoding="utf-8")
    soup = parse_html(html)

    for div_id, raw_text in zip(_ROADMAP_IDS, phase_outputs):
        target = soup.find("div", id=div_id)
        if target is None:
            print(f"[roadmap_builder] WARN: div id='{div_id}' no encontrado en el HTML")
            continue
        target.clear()
        fragment = parse_html(md_to_html(raw_text))
        body = fragment.find("body") or fragment
        for child in list(body.children):
            target.append(child.__copy__())

    REPORT_PATH.write_text(str(soup), encoding="utf-8")
    print(f"[roadmap_builder] Roadmap inyectado en {REPORT_PATH}")

def resumir_solucion(propuesta_solucion: str, agents_config: dict) -> str:
    resumen_agent = _make_agent("resume_agent", agents_config, MODEL_NAME)
    resumen_task = Task(
        description=(
            f"Propuesta a resumir:\n{propuesta_solucion}"
        ),
        expected_output="Un resumen ejecutivo de la propuesta de solución en formato de viñetas claras y accionables.",
        agent=resumen_agent
    )
    resumen_crew = Crew(
        agents=[resumen_agent],
        tasks=[resumen_task],
        process=Process.sequential,
        cache=False,
    )
    try:
        result = resumen_crew.kickoff()
    except Exception as exc:
        print(f"[roadmap_builder] Error ejecutando crew de resumen: {exc}")
        raise

    print(f"\n\n\n=========================================================================================\n RESUMEN DE LA SOLUCIÓN: {result}\n ========================================================================================= \n\n\n")
    return getattr(result, "raw", str(result)) if result else ""




def roadmap_builder(combined_response: dict, problem_case: dict | None = None) -> dict:
    agents_config = load_agents_config()
    captured_outputs = []

    def capture_output(task_output):
        """Callback para capturar output de cada tarea en orden."""
        if task_output is not None:
            raw = getattr(task_output, "raw", str(task_output))
            captured_outputs.append(raw)
            print(f"[roadmap_builder] Capturado output #{len(captured_outputs)}: {len(raw)} chars")
        return task_output

    fuentes_de_datos = _make_agent("data_sources_agent", agents_config, FIRST_MODEL_NAME)
    ingesta_de_datos = _make_agent("ingestion_agent", agents_config, FIRST_MODEL_NAME)
    data_storage = _make_agent("storage_agent", agents_config, MODEL_NAME)
    procesamiento_etl = _make_agent("etl_processing_agent", agents_config, FIRST_MODEL_NAME)
    modelado_y_serving = _make_agent("modeling_serving_agent", agents_config, SECOND_MODEL_NAME)
    ml_mlops = _make_agent("ml_mlops_agent", agents_config, THIRD_MODEL_NAME)
    visualizacion = _make_agent("bi_visualization_agent", agents_config, MODEL_NAME)

    resumen = resumir_solucion(combined_response, agents_config)
    
    # Las 7 tareas reciben, además del resumen, los datos del cliente del discovery.
    if problem_case is None:
        problem_case = load_problem_case()
    contexto = f"{resumen}\n\n{contexto_cliente(problem_case)}\n{PLATAFORMA}"

    data_sources_task = Task(
        description=(
            "Eres el primer agente en ejecutarse. Tu análisis sentará las bases para todos los agentes posteriores.\n\n"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Tu tarea es analizar la propuesta de solución e integración de un problema propuesto e identificar todas las fuentes de datos relevantes para el proyecto. "
            "Para cada fuente debes determinar: qué sistema o plataforma la origina, qué tipo de datos contiene y su relevancia "
            "para el caso de uso, el formato probable (estructurado, semiestructurado, no estructurado), la frecuencia de "
            "actualización estimada (batch diario, tiempo real, ad-hoc...), y los riesgos o dependencias que presenta "
            "(calidad de datos, accesibilidad, necesidad de acuerdos con terceros, etc.).\n"
            "Sé honesto sobre lo que no puedes saber con la información disponible y explicita los supuestos que estás haciendo. "
            "Tu output será leído por el agente de ingesta, así que debe ser claro y accionable."
        ),
        expected_output=(
            "Un análisis estructurado de fuentes de datos con las siguientes secciones:\n"
            "1. Inventario de fuentes identificadas (una por una, con tipo, formato y frecuencia)\n"
            "2. Evaluación de calidad y riesgos por fuente\n"
            "3. Dependencias y supuestos asumidos\n"
            "4. Recomendaciones prioritarias para la fase de ingesta"
        ),
        agent=fuentes_de_datos,
    )

    ingestion_task = Task(
        description=(
            "El agente de fuentes de datos ha completado su análisis. Úsalo como base para diseñar la estrategia de ingesta.\n\n"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en las fuentes identificadas por el agente anterior y en el contexto del cliente, diseña la estrategia "
            "de ingesta con la herramienta de orquestación de la plataforma (por ejemplo, Azure Data Factory en Azure, AWS Glue "
            "en AWS o Cloud Data Fusion en Google Cloud). Define para cada fuente o grupo de fuentes: el patrón de ingesta más adecuado "
            "(batch, streaming, change data capture...), la estructura de pipelines recomendada, los conectores a utilizar "
            "y sus posibles limitaciones, la cadencia de ejecución, y qué transformaciones mínimas deben ocurrir en este punto "
            "antes de llegar al almacenamiento.\n"
            "Ten en cuenta el nivel de madurez de datos del cliente, el equipo disponible y las restricciones técnicas del "
            "problem case para no sobredimensionar la solución. Advierte explícitamente si alguna fuente presenta riesgos "
            "que puedan comprometer la ingesta."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de estrategia de ingesta con las siguientes secciones:\n"
            "1. Patrón de ingesta por fuente o grupo de fuentes (batch / streaming / CDC)\n"
            "2. Estructura de pipelines recomendada\n"
            "3. Conectores y configuraciones clave\n"
            "4. Cadencias y dependencias entre pipelines\n"
            "5. Riesgos identificados y recomendaciones de mitigación"
        ),
        agent=ingesta_de_datos,
    )

    storage_task = Task(
        description=(
            "Los agentes de fuentes de datos e ingesta han completado sus análisis. Úsalos como base para diseñar la arquitectura de almacenamiento.\n\n"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en las fuentes y la estrategia de ingesta definidas por los agentes anteriores, diseña la arquitectura "
            "de almacenamiento en la plataforma. Define la estructura de capas del Data Lake (evalúa si aplica arquitectura medallion "
            "completa Bronze/Silver/Gold o si una solución más simple es más adecuada para este cliente), los servicios de "
            "recomendados (por ejemplo, ADLS Gen2 en Azure, Amazon S3 en AWS o Cloud Storage en Google Cloud, y el almacén "
            "analítico que corresponda) y la justificación de su elección, la estrategia de particionado "
            "y organización de carpetas, las políticas de retención y tier de acceso, y las consideraciones de seguridad y "
            "control de acceso.\n"
            "Parte siempre del volumen estimado, el equipo del cliente y su nivel de madurez para proponer algo que puedan "
            "operar de forma sostenible, no solo implementar. Si la arquitectura medallion completa es excesiva para este "
            "caso, dilo explícitamente y propón una alternativa justificada."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de arquitectura de almacenamiento con las siguientes secciones:\n"
            "1. Decisión y justificación sobre la estructura de capas (medallion completa o simplificada)\n"
            "2. Servicios recomendados y justificación\n"
            "3. Estructura de carpetas y estrategia de particionado\n"
            "4. Políticas de retención y tier de acceso\n"
            "5. Consideraciones de seguridad y gobierno de acceso\n"
            "6. Riesgos y recomendaciones operativas"
        ),
        agent=data_storage,
    )

    first_crew = Crew(
        agents=[fuentes_de_datos, ingesta_de_datos, data_storage],
        tasks=[data_sources_task, ingestion_task, storage_task],
        process=Process.sequential,
        task_callback=capture_output,
        cache=False,
    )

    print("\n\n[roadmap_builder] Ejecutando PRIMERA CREW: Ingesta y Almacenamiento...\n")
    try:
        first_crew.kickoff()
    except Exception as exc:
        print(f"[roadmap_builder] Error ejecutando primera crew: {exc}")
        raise

    print(f"\n\n[roadmap_builder] PRIMERA CREW completada. Outputs capturados: {len(captured_outputs)}\n")
    
    # La segunda crew no ve lo que escribió la primera: se lo pasamos a ETL, la tarea que la abre.
    # Modelado, ML y BI lo reciben a través de la salida de ETL.
    titulos = ["Fuentes de datos", "Ingesta", "Almacenamiento"]
    analisis_primera_crew = (
        "ANÁLISIS DE LA PRIMERA CREW (fuentes, ingesta y almacenamiento):\n"
        + "\n\n".join(f"### {titulo}\n{texto}" for titulo, texto in zip(titulos, captured_outputs))
        + "\n\n"
    )

    etl_processing_task = Task(
        description=(
            "La primera crew ha completado el análisis de fuentes, ingesta y almacenamiento. "
            "Úsalo como base para diseñar la capa de procesamiento y ETL.\n\n"
            f"{analisis_primera_crew}"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en la arquitectura de almacenamiento y la estrategia de ingesta definidas "
            "en el análisis previo, diseña la capa de procesamiento. Evalúa si el perfil del "
            "cliente requiere un motor distribuido como Spark (por ejemplo, Databricks) o si basta con el motor SQL del "
            "almacén analítico de la plataforma (por ejemplo, Synapse o Fabric en Azure, Redshift en AWS o BigQuery en "
            "Google Cloud), justificando "
            "la decisión en base al volumen de datos, la complejidad de las transformaciones y la "
            "capacidad del equipo. Define las transformaciones principales que deben ocurrir en esta "
            "capa, la estrategia de calidad de datos (validaciones, detección de anomalías, "
            "reconciliación), la orquestación de los jobs de procesamiento y la promoción de datos "
            "entre capas del Data Lake. "
            "No propongas complejidad que el equipo del cliente no pueda operar. Si el motor SQL es "
            "suficiente, no recomiendes Spark solo porque es más moderno."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de capa de procesamiento y ETL con las siguientes secciones:\n"
            "1. Decisión y justificación del motor de procesamiento (Spark, SQL o combinación)\n"
            "2. Transformaciones principales por capa (Bronze→Silver, Silver→Gold)\n"
            "3. Estrategia de calidad de datos y validaciones\n"
            "4. Orquestación y scheduling de jobs\n"
            "5. Riesgos operativos y recomendaciones de mantenimiento"
        ),
        agent=procesamiento_etl,
    )

    modeling_serving_task = Task(
        description=(
            "El análisis previo ha definido toda la cadena de datos hasta la capa de serving. "
            "Úsalo como base para diseñar la capa de modelado y serving.\n\n"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en los datos procesados disponibles en la capa Gold del Data Lake y en los "
            "casos de uso de negocio del cliente, diseña el modelo de datos analítico. Define si "
            "aplica un esquema en estrella clásico, un modelo más flexible o una capa semántica "
            "en la herramienta de BI del cliente o en un servicio de modelo semántico. Especifica las entidades principales "
            "(tablas de hechos y dimensiones o equivalentes), las métricas de negocio clave que "
            "deben estar precalculadas, la estrategia de actualización de datos en esta capa y "
            "cómo se expondrán los datos para consumo por BI, APIs y modelos de ML. "
            "Prioriza la comprensibilidad del modelo para el equipo de negocio del cliente sobre "
            "la elegancia técnica."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de modelado y serving con las siguientes secciones:\n"
            "1. Decisión sobre el tipo de modelo analítico y justificación\n"
            "2. Entidades principales del modelo (hechos, dimensiones o equivalentes)\n"
            "3. Métricas de negocio clave y cálculos precalculados\n"
            "4. Estrategia de actualización y latencia de datos\n"
            "5. Exposición de datos para BI, ML y APIs\n"
            "6. Consideraciones de rendimiento y escalabilidad"
        ),
        agent=modelado_y_serving,
    )

    ml_mlops_task = Task(
        description=(
            "El análisis previo ha definido toda la cadena de datos hasta la capa de serving. "
            "Úsalo como base para diseñar la estrategia de machine learning y MLOps.\n\n"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en los casos de uso de IA identificados en la propuesta de solución y en "
            "los datos disponibles en la capa de serving, define los modelos de ML más adecuados "
            "para el proyecto. Para cada caso de uso especifica: el tipo de modelo recomendado y "
            "su justificación (no siempre el más complejo es el mejor), los datos de entrenamiento "
            "necesarios y su disponibilidad según lo definido en el análisis previo, la "
            "infraestructura de ML necesaria en la plataforma (por ejemplo, Azure Machine Learning en Azure, Amazon "
            "SageMaker en AWS o Vertex AI en Google Cloud: cómputo, entornos y endpoints), el ciclo "
            "de vida del modelo (entrenamiento, validación, despliegue, monitorización y "
            "reentrenamiento), y las métricas de evaluación tanto técnicas como de negocio. "
            "Sé explícito sobre qué puede construirse con el equipo y datos actuales del cliente "
            "y qué requeriría madurez adicional."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de estrategia ML y MLOps con las siguientes secciones:\n"
            "1. Casos de uso de ML priorizados y tipo de modelo recomendado por caso\n"
            "2. Datos de entrenamiento necesarios y disponibilidad real\n"
            "3. Infraestructura de ML requerida\n"
            "4. Ciclo de vida del modelo: entrenamiento, despliegue y monitorización\n"
            "5. Métricas de evaluación técnicas y de negocio\n"
            "6. Hoja de ruta de madurez: qué es viable ahora y qué requiere una segunda fase"
        ),
        agent=ml_mlops,
    )

    bi_visualization_roadmap_task = Task(
        description=(
            "Eres el último agente de la cadena. Todos los componentes técnicos han sido definidos por los análisis previos. "
            "Tu tarea es diseñar la capa de visualización que hará que todo ese trabajo sea visible "
            "y útil para el cliente.\n\n"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en el modelo de datos de la capa de serving y en los perfiles de usuario "
            "del cliente, diseña la arquitectura de la herramienta de BI del cliente (herramienta_de_visualizacion), o de la "
            "que recomiende la propuesta si no tiene ninguna, con las licencias que ya tiene; si algo necesita otras, dilo "
            "como coste por confirmar. Define el modo de conexión más adecuado (por ejemplo, Import, DirectQuery o "
            "Composite en Power BI, o su equivalente en otras herramientas) justificando la decisión según el volumen "
            "y la frecuencia de actualización. Propón la estructura de espacios de trabajo y la gobernanza "
            "del entorno de BI. Define los informes y dashboards principales por perfil de "
            "usuario (dirección, operaciones, analistas) y su propósito concreto. Incluye un plan "
            "de adopción básico: formación necesaria, métricas de uso para evaluar el éxito y "
            "riesgos de adopción. "
            "Recuerda que un dashboard que nadie usa es un proyecto fallido independientemente "
            "de su calidad técnica."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de estrategia de visualización con las siguientes secciones:\n"
            "1. Arquitectura de BI: herramienta, modo de conexión y justificación\n"
            "2. Estructura de espacios de trabajo y modelo de gobernanza\n"
            "3. Catálogo de informes y dashboards por perfil de usuario\n"
            "4. Plan de adopción: formación, rollout y métricas de éxito\n"
            "5. Riesgos de adopción y recomendaciones"
        ),
        agent=visualizacion,
    )

    second_crew = Crew(
        agents=[procesamiento_etl, modelado_y_serving, ml_mlops, visualizacion],
        tasks=[etl_processing_task, modeling_serving_task, ml_mlops_task, bi_visualization_roadmap_task],
        process=Process.sequential,
        task_callback=capture_output,
        cache=False,
    )

    print("\n\n[roadmap_builder] Ejecutando SEGUNDA CREW: Procesamiento, Modelado, ML y BI...\n")
    try:
        second_crew.kickoff()
    except Exception as exc:
        print(f"[roadmap_builder] Error ejecutando segunda crew: {exc}")
        raise

    print(f"\n\n[roadmap_builder] SEGUNDA CREW completada. Outputs capturados: {len(captured_outputs)}\n")

    if len(captured_outputs) == 7:
        inject_roadmap_into_html(*captured_outputs)
        print("[roadmap_builder] Roadmap inyectado en HTML exitosamente.\n")
    else:
        print(f"[roadmap_builder] WARN: Se esperaban 7 outputs pero se capturaron {len(captured_outputs)}. No se inyecta HTML.\n")

    combined_output = "\n".join(captured_outputs)
    return combined_output

