import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from crewai import Agent, Crew, Process, Task
from dotenv import load_dotenv
import gateway
import progreso
from utils import (_make_agent, con_nota_de_servicios, contexto_cliente, load_agents_config, load_problem_case,
                   parse_html, md_to_html, sin_importes, sin_emojis)

load_dotenv(".env")

'''
MODEL_NAME = os.getenv("BIG_MODEL", "").strip()
SECOND_MODEL_NAME = os.getenv("REASONING_MODEL", "").strip()
FIRST_MODEL_NAME = os.getenv("REASONING_RIGOROUS_MODEL", "").strip()
THIRD_MODEL_NAME = os.getenv("SMALL_REASONING_MODEL", "").strip()
'''

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
# Fallo 67: si el agente de plataforma ha elegido una nube (PLATAFORMA OBJETIVO en los datos del cliente), manda esa.
PLATAFORMA = (
    "PLATAFORMA: si los datos del cliente traen una PLATAFORMA OBJETIVO, diseña en esa nube; si no, en la que indica "
    "el caso (cloud_provider). Usa además las herramientas que indica el caso (modelo_de_despliegue, "
    "herramienta_de_visualizacion, stack_tecnologico_actual) y las que ya eligió la propuesta de arquitectura. "
    "Si el cliente no tiene preferencia, elige tú y justifícalo. Nombra los servicios concretos de esa plataforma; "
    "los ejemplos de esta tarea son solo orientativos.\n\n"
)

# Fallo 73: las 7 fases se escriben a la vez (antes, una detrás de otra: 11,6 min el 12 nov). Ninguna lee lo que
# escribe otra: todas desarrollan la misma propuesta de arquitectura, que es la referencia común. Así tampoco se
# contradicen con ella (el 12 nov, el almacenamiento descartó las tres zonas del lago que proponía la arquitectura).
HILOS = 7  # fases a la vez; con 1 van en orden, una detrás de otra (los tests lo usan para saber qué salida es cuál)
TITULOS = ["Fuentes de datos", "Ingesta", "Almacenamiento", "Procesamiento y ETL", "Modelado y serving",
           "ML y MLOps", "Visualización"]
REFERENCIA = (
    "CÓMO ENCAJA TU FASE: las 7 fases del roadmap se escriben a la vez y cada una desarrolla su parte de la misma "
    "propuesta de arquitectura. Usa sus servicios, sus capas, su región y sus decisiones tal cual: no propongas "
    "otros ni cambies lo que ya está decidido. Si algo te parece un riesgo, dilo como riesgo y cómo mitigarlo, sin "
    "cambiar el diseño. No des por hecho lo que digan las otras fases: cuéntalo desde la propuesta.\n\n"
)

# Fallo 74: lo que tarda cada fase depende sobre todo de lo que escribe. El 12 nov, cada una escribió entre 14.000 y
# 20.000 caracteres (5.700–8.300 tokens) y las dos más largas tardaron casi 4 min. Con None, sin tope.
PALABRAS_POR_FASE = None  # Fallo 74 (13 nov): sin tope otra vez, con las fases a la vez; se mide en la próxima ejecución
EXTENSION = (f"\nExtensión: {PALABRAS_POR_FASE} palabras como máximo. Ve a lo concreto de este cliente, sin introducción "
             "ni repetir lo que ya dice la propuesta." if PALABRAS_POR_FASE else "")


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
    resumen_agent = _make_agent("resume_agent", agents_config, gateway.modelo("resumen"))
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

    # Fallo 65: sin emojis, para que las 7 fases del roadmap no copien el estilo
    resumen = sin_emojis(getattr(result, "raw", str(result))) if result else ""
    print(f"\n\n\n=========================================================================================\n RESUMEN DE LA SOLUCIÓN: {resumen}\n ========================================================================================= \n\n\n")
    return resumen




def roadmap_builder(combined_response: dict, problem_case: dict | None = None) -> dict:
    agents_config = load_agents_config()
    # Gateway de modelos: cada fase pide el suyo (data_structures/modelos.yaml)
    fuentes_de_datos = _make_agent("data_sources_agent", agents_config, gateway.modelo("roadmap_fuentes"))
    ingesta_de_datos = _make_agent("ingestion_agent", agents_config, gateway.modelo("roadmap_ingesta"))
    data_storage = _make_agent("storage_agent", agents_config, gateway.modelo("roadmap_almacenamiento"))
    procesamiento_etl = _make_agent("etl_processing_agent", agents_config, gateway.modelo("roadmap_etl"))
    modelado_y_serving = _make_agent("modeling_serving_agent", agents_config, gateway.modelo("roadmap_modelado"))
    ml_mlops = _make_agent("ml_mlops_agent", agents_config, gateway.modelo("roadmap_ml"))
    visualizacion = _make_agent("bi_visualization_agent", agents_config, gateway.modelo("roadmap_bi"))

    resumen = resumir_solucion(combined_response, agents_config)
    progreso.avanzar("Resumen de la propuesta")  # fallo 94

    # Las 7 tareas reciben, además del resumen, los datos del cliente del discovery.
    if problem_case is None:
        problem_case = load_problem_case()
    # Fallo 73: la arquitectura de la propuesta va entera, como referencia común de las 7 fases (ya sin importes)
    arquitectura = combined_response.get("architecture", "") if isinstance(combined_response, dict) else ""
    contexto = (f"{resumen}\n\n"
                + (f"PROPUESTA DE ARQUITECTURA (la referencia común de las 7 fases):\n{sin_emojis(arquitectura)}\n\n"
                   if arquitectura else "")
                + f"{contexto_cliente(problem_case)}\n{PLATAFORMA}{REFERENCIA}")

    data_sources_task = Task(
        description=(
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Tu tarea es analizar la propuesta de solución e integración de un problema propuesto e identificar todas las fuentes de datos relevantes para el proyecto. "
            "Para cada fuente debes determinar: qué sistema o plataforma la origina, qué tipo de datos contiene y su relevancia "
            "para el caso de uso, el formato probable (estructurado, semiestructurado, no estructurado), la frecuencia de "
            "actualización estimada (batch diario, tiempo real, ad-hoc...), y los riesgos o dependencias que presenta "
            "(calidad de datos, accesibilidad, necesidad de acuerdos con terceros, etc.).\n"
            "Sé honesto sobre lo que no puedes saber con la información disponible y explicita los supuestos que estás haciendo. "
            "Debe ser claro y accionable."
        ),
        expected_output=(
            "Un análisis estructurado de fuentes de datos con las siguientes secciones:\n"
            "1. Inventario de fuentes identificadas (una por una, con tipo, formato y frecuencia)\n"
            "2. Evaluación de calidad y riesgos por fuente\n"
            "3. Dependencias y supuestos asumidos\n"
            "4. Recomendaciones prioritarias para la fase de ingesta"
        ) + EXTENSION,
        agent=fuentes_de_datos,
    )

    ingestion_task = Task(
        description=(
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en las fuentes de datos del caso y de la propuesta, y en el contexto del cliente, diseña la estrategia "
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
        ) + EXTENSION,
        agent=ingesta_de_datos,
    )

    storage_task = Task(
        description=(
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en las fuentes y la ingesta de la propuesta, diseña la arquitectura de almacenamiento en la plataforma. "
            "Concreta la estructura de capas del Data Lake que define la propuesta de arquitectura (si no define ninguna, "
            "evalúa si aplica medallion completa Bronze/Silver/Gold o una solución más simple), los servicios "
            "recomendados (por ejemplo, ADLS Gen2 en Azure, Amazon S3 en AWS o Cloud Storage en Google Cloud, y el almacén "
            "analítico que corresponda) y la justificación de su elección, la estrategia de particionado "
            "y organización de carpetas, las políticas de retención y tier de acceso, y las consideraciones de seguridad y "
            "control de acceso.\n"
            "Parte siempre del volumen estimado, el equipo del cliente y su nivel de madurez para proponer algo que puedan "
            "operar de forma sostenible, no solo implementar. Si las capas de la propuesta te parecen excesivas para este "
            "caso, dilo como riesgo, pero diseña con ellas."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de arquitectura de almacenamiento con las siguientes secciones:\n"
            "1. Estructura de capas y su justificación\n"
            "2. Servicios recomendados y justificación\n"
            "3. Estructura de carpetas y estrategia de particionado\n"
            "4. Políticas de retención y tier de acceso\n"
            "5. Consideraciones de seguridad y gobierno de acceso\n"
            "6. Riesgos y recomendaciones operativas"
        ) + EXTENSION,
        agent=data_storage,
    )

    etl_processing_task = Task(
        description=(
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en el almacenamiento y la ingesta de la propuesta de arquitectura, diseña la capa de procesamiento. Evalúa si el perfil del "
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
            "2. Transformaciones principales entre las capas de la propuesta\n"
            "3. Estrategia de calidad de datos y validaciones\n"
            "4. Orquestación y scheduling de jobs\n"
            "5. Riesgos operativos y recomendaciones de mantenimiento"
        ) + EXTENSION,
        agent=procesamiento_etl,
    )

    modeling_serving_task = Task(
        description=(
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en los datos procesados de la última capa del Data Lake de la propuesta y en los "
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
        ) + EXTENSION,
        agent=modelado_y_serving,
    )

    ml_mlops_task = Task(
        description=(
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en los casos de uso de IA identificados en la propuesta de solución y en "
            "los datos disponibles en la capa de serving, define los modelos de ML más adecuados "
            "para el proyecto. Para cada caso de uso especifica: el tipo de modelo recomendado y "
            "su justificación (no siempre el más complejo es el mejor), los datos de entrenamiento "
            "necesarios y su disponibilidad según el caso y la propuesta, la "
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
        ) + EXTENSION,
        agent=ml_mlops,
    )

    bi_visualization_roadmap_task = Task(
        description=(
            "Tu tarea es diseñar la capa de visualización que hará que la solución sea visible y útil para el cliente.\n\n"
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            "Basándote en el modelo de datos de la propuesta y en los perfiles de usuario "
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
        ) + EXTENSION,
        agent=visualizacion,
    )

    tareas = [data_sources_task, ingestion_task, storage_task, etl_processing_task, modeling_serving_task,
              ml_mlops_task, bi_visualization_roadmap_task]
    salidas = [""] * len(tareas)

    def ejecutar(n: int, tarea: Task) -> None:
        """Una fase en su propia crew. Si falla, las demás siguen y su sección del informe sale vacía."""
        def guardar(task_output):
            if task_output is not None:
                salidas[n] = getattr(task_output, "raw", str(task_output))
            return task_output

        crew = Crew(agents=[tarea.agent], tasks=[tarea], process=Process.sequential, task_callback=guardar, cache=False)
        inicio = time.monotonic()
        try:
            crew.kickoff()
        except Exception as exc:
            print(f"[roadmap_builder] La fase «{TITULOS[n]}» ha fallado ({type(exc).__name__}: {exc}); su sección sale vacía")
            progreso.avanzar(f"Roadmap: {TITULOS[n]} (sin texto)")  # fallo 94
            return
        if not salidas[n]:  # sin callback, la salida de la tarea
            salidas[n] = getattr(getattr(tarea, "output", None), "raw", "") or ""
        print(f"[roadmap_builder] {TITULOS[n]}: {len(salidas[n])} caracteres · {time.monotonic() - inicio:.0f} s")
        progreso.avanzar(f"Roadmap: {TITULOS[n]}")  # fallo 94: desde el hilo solo se anota; lo pinta la sesión

    print(f"\n\n[roadmap_builder] Ejecutando las {len(tareas)} fases del roadmap"
          + (" a la vez" if HILOS > 1 else " en orden") + "...\n")
    inicio = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, HILOS), thread_name_prefix="roadmap") as hilos:
        # Fallo 94: el hilo de la sesión espera a las fases pintando la barra cada vez que acaba una
        progreso.esperar([hilos.submit(ejecutar, n, tarea) for n, tarea in enumerate(tareas)])
    vacias = [TITULOS[n] for n, texto in enumerate(salidas) if not texto.strip()]
    print(f"\n[roadmap_builder] Roadmap: {len(tareas) - len(vacias)} de {len(tareas)} fases en "
          f"{time.monotonic() - inicio:.0f} s" + (f"; sin texto: {', '.join(vacias)}" if vacias else "") + "\n")
    captured_outputs = salidas

    # Fallo 44: los importes salen solo en la sección de costes, donde se suman en Python
    captured_outputs[:] = [sin_importes(texto, problem_case, f"roadmap {n}") for n, texto in enumerate(captured_outputs, 1)]
    # Fallo 83: la fase que nombre un servicio que no está en la región o está retirado lleva una nota visible
    captured_outputs[:] = [con_nota_de_servicios(texto, problem_case, f"roadmap: {TITULOS[n]}") if texto.strip() else texto
                           for n, texto in enumerate(captured_outputs)]

    if len(captured_outputs) == 7:
        inject_roadmap_into_html(*captured_outputs)
        print("[roadmap_builder] Roadmap inyectado en HTML exitosamente.\n")
    else:
        print(f"[roadmap_builder] WARN: Se esperaban 7 outputs pero se capturaron {len(captured_outputs)}. No se inyecta HTML.\n")

    combined_output = "\n".join(captured_outputs)
    return combined_output

