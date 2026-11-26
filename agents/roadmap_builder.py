import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from crewai import Agent, Crew, Process, Task
from dotenv import load_dotenv
import gateway
import progreso
from agents import inventario
from utils import (INFORME_GENERADO, PLATAFORMA_OBJETIVO, _make_agent, _normalize_text, con_nota_de_servicios, contexto_cliente, es_valor_de_relleno, html_del_informe,
                   load_agents_config, load_problem_case, parse_html, md_to_html, sin_importes, sin_emojis)

load_dotenv(".env")
logger = logging.getLogger("aidm")

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

REPORT_PATH = INFORME_GENERADO  # fallo 116: el informe de la ejecución, no la plantilla

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
# Fallo 130: «Modelado y serving» pasa a «Modelado / Análisis» y «ML y MLOps» a «Ops», con un contenido más general
TITULOS = ["Fuentes de datos", "Ingesta", "Almacenamiento", "Procesamiento y ETL", "Modelado / Análisis",
           "Ops", "Visualización"]

# Fallo 131: solo las fases que pide el objetivo. La estructura de la solución termina con el bloque «FASES DEL ROADMAP»
# (una línea por fase: fase | sí / no / ya lo tiene | frase); Python lo lee como el inventario. Ninguna fase es fija: un
# cliente que ya tiene fuentes, ingesta, almacenamiento y ETL y solo pide cuadros de mando necesita solo Visualización.
# «no»: no se genera ni sale en el informe. «ya lo tiene»: no se genera y sale en el flujo como «Ya implantado», pero
# solo si stack_tecnologico_actual o madurez_analitica_actual nombran lo que dice la frase. Sin bloque, si no se lee o
# si no queda ninguna fase que generar, van las 7.
FASES_TITULO = "FASES DEL ROADMAP"
NOMBRES_FASES = ["Fuentes", "Ingesta", "Almacenamiento", "ETL", "Modelado / Análisis", "Ops", "Visualización"]
SI, NO, YA = "si", "no", "ya_lo_tiene"
INSTRUCCION_FASES = (
    f"Termina la respuesta con un bloque que empiece por la línea «{FASES_TITULO}» y siga con una línea por cada una "
    f"de estas 7 fases del roadmap, en este orden: {', '.join(NOMBRES_FASES)}. Formato de cada línea:\n"
    "- fase | sí / no / ya lo tiene | una frase\n"
    "«sí»: el objetivo la necesita y hay que construirla; la frase dice por qué. «no»: el objetivo no la necesita; la "
    "frase dice por qué. «ya lo tiene»: el cliente ya tiene hecho lo que entregaría esta fase y no queda nada que "
    "construir en ella; la frase dice qué tiene, con sus palabras y el nombre de su herramienta (por ejemplo, «su "
    "almacén de datos en SQL Server»). Si solo tiene la herramienta o las licencias (por ejemplo, la herramienta de BI, "
    "pero no los cuadros de mando que pide el proyecto), la fase va como «sí» y la frase dice qué se reutiliza. Ninguna fase "
    "va siempre: un cliente que ya tiene sus fuentes, su ingesta, su almacén y su ETL y solo pide cuadros de mando "
    "necesita solo Visualización (y quizá Modelado / Análisis). Lo que el cliente ya tiene se reutiliza: la "
    "arquitectura lo integra y los costes no lo presupuestan como nuevo. Ningún otro texto después del bloque."
)
_CLAVES_FASE = [("fuentes", "fuente"), ("ingesta",), ("almacenamiento",), ("etl", "procesamiento"),
                ("modelado", "analisis"), ("ops", "operacion"), ("visualizacion", "bi")]
_CAMPOS_STACK = ("stack_tecnologico_actual", "madurez_analitica_actual")
# Siglas y palabras que nombran un tipo de pieza, no una herramienta del cliente: no bastan para dar por buena la fase
_GENERICAS = {"su", "sus", "el", "la", "los", "las", "un", "una", "etl", "elt", "dwh", "bi", "erp", "crm", "api",
              "apis", "sql", "kpi", "kpis", "ia", "ai", "ml", "cloud", "data", "lake", "excel"}
_NOMBRE_PROPIO = re.compile(r"(?:\b(?:[A-Z][\w.+-]*|\w*\d\w*)\b[ \t]*)+")


def _texto_stack(problem_case: dict | None) -> str:
    """stack_tecnologico_actual y madurez_analitica_actual con dato real, juntos y normalizados."""
    partes = []
    for campo in _CAMPOS_STACK:
        valor = (problem_case or {}).get(campo)
        valor = valor.get("value") if isinstance(valor, dict) else valor
        partes += [str(v) for v in (valor if isinstance(valor, list) else [valor])
                   if isinstance(v, str) and not es_valor_de_relleno(v)]
    return _normalize_text(" | ".join(partes))


def _herramienta_en_stack(frase: str, stack: str) -> str | None:
    """Fallo 131: el nombre propio de la frase («SQL Server» en «su DWH en SQL Server») que el stack del cliente también
    nombra, comparado como inventario._clave() (sin paréntesis ni el nombre de la nube). None si no hay ninguno."""
    for candidato in _NOMBRE_PROPIO.findall(re.sub(r"\([^)]*\)", " ", frase or "")):
        palabras = inventario._clave(candidato).split()
        while palabras and palabras[0] in _GENERICAS:  # «Su ERP SAP Business One» → «sap business one»
            palabras.pop(0)
        while palabras and palabras[-1] in _GENERICAS:
            palabras.pop()
        clave = " ".join(palabras)
        if (clave and not clave.isdigit()
                and re.search(rf"(?<![a-z0-9]){re.escape(clave)}(?![a-z0-9])", stack)):
            return clave
    return None


# Fallo 131 (parte 2): una frase de «ya lo tiene» que habla de trabajo por hacer. El 25 nov, Visualización salió «ya lo
# tiene» por las licencias de Power BI, con «se reutilizarán para conectar los nuevos modelos de datos»
_TRABAJO_POR_HACER = re.compile(r"\b(?:se reutilizar(?:a|an) para|conectar(?:a|an)?|nuev[oa]s?|crear(?:a|an)?|"
                                r"desarrollar(?:a|an)?)\b")
_NEGACION = {"no", "sin", "nada", "ni", "ningun", "ninguna", "ninguno"}


def _trabajo_por_hacer(frase: str) -> str | None:
    """Fallo 131 (parte 2): lo que en la frase dice que queda trabajo («conectar», «nuevos»…), o None. No cuenta si
    va negado en las palabras de antes («no hace falta nada nuevo»)."""
    normal = _normalize_text(frase or "")
    for m in _TRABAJO_POR_HACER.finditer(normal):
        if not _NEGACION & set(normal[:m.start()].split()[-4:]):
            return m.group(0)
    return None


def leer_fases(texto: str, problem_case: dict | None = None) -> tuple[str, dict[str, tuple[str, str]] | None]:
    """Fallo 131: el texto de la estructura sin el bloque «FASES DEL ROADMAP» y, por fase ({div_id: (estado, frase)},
    estado SI, NO o YA), lo que dice el bloque. «ya lo tiene» solo vale si el stack del cliente nombra lo que dice la
    frase; si no, la fase se genera. Una fase que el bloque no trae, también. Sin bloque, sin ninguna línea legible o
    sin ninguna fase que generar, None: van las 7. Si hay fases «ya lo tiene», el texto termina con la línea que
    lee la propuesta, para que la arquitectura las integre y los costes no las presupuesten como nuevas."""
    lineas = (texto or "").splitlines()
    inicio = next((n for n in range(len(lineas) - 1, -1, -1)
                   if FASES_TITULO.lower() in _normalize_text(lineas[n]).replace("*", "").replace("#", "")), None)
    leidas: dict[int, tuple[str, str]] = {}
    if inicio is not None:
        for linea in lineas[inicio + 1:]:
            celdas = [c.strip(" *`") for c in linea.strip().strip("-*• ").strip("|").split("|")]
            if len(celdas) < 2:
                if leidas and linea.strip():
                    break
                continue
            palabras = set(re.findall(r"[a-z]+", _normalize_text(celdas[0])))
            fase = next((n for n, claves in enumerate(_CLAVES_FASE) if palabras & set(claves)), None)
            respuesta = re.sub(r"\s+", " ", _normalize_text(celdas[1])).strip(" .")
            estado = (SI if respuesta in ("si", "yes") else NO if respuesta == "no"
                      else YA if respuesta in ("ya lo tiene", "ya la tiene", "ya lo tienen", "ya la tienen") else None)
            if fase is not None and estado is not None and fase not in leidas:
                leidas[fase] = (estado, celdas[2] if len(celdas) > 2 else "")
    sin_bloque = "\n".join(lineas[:inicio]).rstrip() if inicio is not None else texto
    if not leidas:
        logger.warning("[fases] La estructura no trae el bloque %s o no se lee: van las 7 fases", FASES_TITULO)
        return sin_bloque, None
    stack = _texto_stack(problem_case)
    fases = {}
    for n, div_id in enumerate(_ROADMAP_IDS):
        estado, frase = leidas.get(n, (SI, ""))
        if n not in leidas:
            logger.warning("[fases] El bloque %s no dice nada de %s: se genera", FASES_TITULO, NOMBRES_FASES[n])
        elif estado == YA and (trabajo := _trabajo_por_hacer(frase)):  # fallo 131 (parte 2)
            logger.warning("[fases] %s: «ya lo tiene» («%s»), pero la frase habla de trabajo por hacer («%s»): se "
                           "genera", NOMBRES_FASES[n], frase, trabajo)
            estado = SI
        elif estado == YA and not _herramienta_en_stack(frase, stack):
            logger.warning("[fases] %s: «ya lo tiene» («%s»), pero ni stack_tecnologico_actual ni "
                           "madurez_analitica_actual lo nombran: se genera", NOMBRES_FASES[n], frase)
            estado = SI
        fases[div_id] = (estado, frase)
    if all(estado != SI for estado, _ in fases.values()):
        logger.warning("[fases] El bloque %s no deja ninguna fase que generar: van las 7 fases", FASES_TITULO)
        return sin_bloque, None
    de = lambda estado: [f"{NOMBRES_FASES[n]} ({fases[d][1] or 'sin motivo'})" for n, d in enumerate(_ROADMAP_IDS)
                         if fases[d][0] == estado]
    print(f"[fases] Roadmap con {len(de(SI))} de 7 fases" + (f"; no aplican: {'; '.join(de(NO))}" if de(NO) else "")
          + (f"; ya implantadas: {'; '.join(de(YA))}" if de(YA) else ""))
    if de(YA):
        sin_bloque += ("\n\nYa implantado en el cliente (se reutiliza y se integra; no se presupuesta como nuevo): "
                       + "; ".join(de(YA)) + ".")
    return sin_bloque, fases


def _referencia(fases: int) -> str:
    """CÓMO ENCAJA TU FASE, con el número de fases que se escriben (fallo 131: pueden ser menos de 7)."""
    return REFERENCIA.replace("las 7 fases", f"las {fases} fases")


REFERENCIA = (
    "CÓMO ENCAJA TU FASE: las 7 fases del roadmap se escriben a la vez y cada una desarrolla su parte de la misma "
    "propuesta de arquitectura. Usa sus servicios, sus capas, su región y sus decisiones tal cual: no propongas "
    "otros ni cambies lo que ya está decidido. Si algo te parece un riesgo, dilo como riesgo y cómo mitigarlo, sin "
    "cambiar el diseño. No des por hecho lo que digan las otras fases: cuéntalo desde la propuesta.\n\n"
)

# Fallo 88: con el inventario de la arquitectura, las fases usan solo esos servicios, y la orquestación es la suya
SOLO_INVENTARIO = ("Usa solo los servicios de este inventario, con la fase que dice cada uno. La orquestación es la del "
                   "inventario: no propongas otra. Si echas en falta un servicio, dilo como riesgo, sin añadirlo.")
# Fallo 88: la sección de la propuesta que desarrolla cada fase (lo que antes le llegaba a través del resumen)
SECCION_DE_CADA_FASE = {
    0: ("data_ai_strategy", "ESTRATEGIA DE DATOS E IA"),   # fuentes: las de cada funcionalidad
    2: ("governance", "GOBERNANZA DEL DATO E IA"),          # almacenamiento: retención, acceso, clasificación
    3: ("governance", "GOBERNANZA DEL DATO E IA"),          # ETL: reglas de calidad
    4: ("bi_visualization", "ESTRATEGIA DE VISUALIZACIÓN"), # modelado: KPIs y métricas
    5: ("data_ai_strategy", "ESTRATEGIA DE DATOS E IA"),   # ML: casos de uso y KPIs
    6: ("bi_visualization", "ESTRATEGIA DE VISUALIZACIÓN"), # BI: cuadros de mando y usuarios
}

# Fallo 74: lo que tarda cada fase depende sobre todo de lo que escribe. El 12 nov, cada una escribió entre 14.000 y
# 20.000 caracteres (5.700–8.300 tokens) y las dos más largas tardaron casi 4 min. Con None, sin tope.
PALABRAS_POR_FASE = None  # Fallo 74 (13 nov): sin tope otra vez, con las fases a la vez; se mide en la próxima ejecución
EXTENSION = (f"\nExtensión: {PALABRAS_POR_FASE} palabras como máximo. Ve a lo concreto de este cliente, sin introducción "
             "ni repetir lo que ya dice la propuesta." if PALABRAS_POR_FASE else "")


# Fallo 102: las etiquetas del flujo del roadmap (el nodo de cada fase y el chip de su tarjeta) salían fijas de la
# plantilla, con servicios de Azure («Azure Data Factory», «ADLS Gen2», «Azure ML»…) aunque la solución fuera en AWS.
# Ahora salen del inventario de servicios, por la capa de cada uno; la fase sin servicios lleva la plataforma.
_CAPAS_DE_FASE = [  # por orden: «analítica / IA» es de Ops (antes ML), no de modelado
    ("roadmap-sources", ("fuente", "origen")),
    ("roadmap-ingestion", ("ingesta", "ingestion")),
    ("roadmap-storage", ("almacenamiento", "storage", "lago")),
    # Fallo 130: Ops es la operación y la monitorización y, si hay modelos, MLOps
    ("roadmap-ml", ("ia", "ml", "ai", "mlops", "inteligencia artificial", "machine learning", "monitorizacion",
                    "observabilidad", "operacion", "ops")),
    ("roadmap-etl", ("procesamiento", "orquestacion", "transformacion", "etl", "calidad")),
    ("roadmap-modeling", ("analitica", "modelado", "semantica", "consulta")),
    ("roadmap-bi", ("visualizacion", "bi", "informes", "dashboards")),
]
_SIN_NUBE = re.compile(r"^(?:amazon|aws|azure|microsoft|google cloud|google|oci|oracle|ibm)\s+", re.IGNORECASE)


def _fase_de_capa(capa: str) -> str | None:
    palabras = set(re.findall(r"[a-z]+", _normalize_text(capa or "")))
    for div_id, claves in _CAPAS_DE_FASE:
        if any((c in palabras) if " " not in c else (c in _normalize_text(capa)) for c in claves):
            return div_id
    return None


def etiquetas_del_flujo(filas: list[dict], problem_case: dict | None) -> dict[str, str]:
    """Fallo 102: la etiqueta de cada fase del roadmap ({div_id: texto}): sus servicios del inventario, sin el nombre
    de la nube ni los paréntesis, dos como mucho. La fase sin servicios lleva la plataforma objetivo (las fuentes, «los
    sistemas del cliente»)."""
    servicios: dict[str, list[str]] = {div_id: [] for div_id in _ROADMAP_IDS}
    for f in filas or []:
        div_id = _fase_de_capa(f.get("capa", ""))
        nombre = _SIN_NUBE.sub("", re.sub(r"\s*\([^)]*\)", "", f.get("servicio", ""))).strip()
        if div_id and nombre and nombre not in servicios[div_id]:
            servicios[div_id].append(nombre)
    plataforma = (problem_case or {}).get(PLATAFORMA_OBJETIVO)
    nube = plataforma.get("value", "") if isinstance(plataforma, dict) else ""
    return {div_id: " · ".join(nombres[:2]) if nombres
            else ("Sistemas del cliente" if div_id == "roadmap-sources" else nube)
            for div_id, nombres in servicios.items()}


def _poner_etiquetas(soup, etiquetas: dict[str, str]) -> None:
    """Fallo 102: cambia en el HTML la etiqueta del nodo del flujo, el chip de la tarjeta y, si nombra la herramienta
    («Estrategia y pipelines con Azure Data Factory»), el subtítulo de la tarjeta. Un chip vacío se oculta.
    Fallo 116: cada ejecución parte de la plantilla, así que el subtítulo trae siempre la herramienta de la plantilla."""
    for div_id, texto in etiquetas.items():
        nodo = soup.find("div", class_="rdm-pipe-node", onclick=re.compile(re.escape(f"'{div_id}'")))
        tech = nodo.find("span", class_="rdm-pipe-tech") if nodo else None
        if tech is not None:
            tech.string = texto
        tarjeta = soup.find("div", id=f"card-{div_id}")
        if tarjeta is None:
            continue
        chip = tarjeta.find("span", class_="rdm-tech-chip")
        subtitulo = tarjeta.select_one(".section-header-text > span")
        viejo = chip.get_text(strip=True) if chip is not None else ""
        if subtitulo is not None and viejo and f" con {viejo}" in subtitulo.get_text():
            subtitulo.string = subtitulo.get_text().replace(f" con {viejo}", f" con {texto}" if texto else "")
        if chip is not None:
            chip.string = texto
            if texto:
                chip.attrs.pop("hidden", None)
            else:
                chip["hidden"] = ""


def _quitar_fases(soup, no_aplican: dict[str, str], ya_implantadas: dict[str, str]) -> None:
    """Fallo 131: quita del flujo el nodo de cada fase que no aplica (con su flecha); el de cada fase que el cliente
    ya tiene se queda atenuado, con «Ya implantado». Las tarjetas de las dos se quitan y los pasos que quedan se
    renumeran (01, 02…). Bajo el flujo, «No aplica a este proyecto: Ops (motivo)» y «Se reutiliza: ETL (lo que tiene
    el cliente)». Solo nombres, sin ids."""
    for div_id in list(no_aplican) + list(ya_implantadas):
        nodo = soup.find("div", class_="rdm-pipe-node", onclick=re.compile(re.escape(f"'{div_id}'")))
        if nodo is not None and div_id in ya_implantadas:
            nodo["class"] = nodo.get("class", []) + ["rdm-pipe-implantado"]
            del nodo["onclick"]  # sin tarjeta a la que ir
            tech = nodo.find("span", class_="rdm-pipe-tech")
            if tech is not None:
                tech.string = "Ya implantado"
        elif nodo is not None:
            flecha = nodo.find_previous_sibling() or nodo.find_next_sibling()
            if flecha is not None and "rdm-pipe-arrow" in (flecha.get("class") or []):
                flecha.decompose()
            nodo.decompose()
        tarjeta = soup.find("div", id=f"card-{div_id}")
        if tarjeta is not None:
            tarjeta.decompose()
    for n, paso in enumerate(soup.select("#rdm-stages-container .rdm-step"), start=1):
        paso.string = f"{n:02d}"
    flujo = soup.find("div", class_="rdm-nav-card")
    for clase, inicio, fases in (("rdm-ya-implantado", "Se reutiliza", ya_implantadas),
                                 ("rdm-no-aplica", "No aplica a este proyecto", no_aplican)):
        if not fases or flujo is None:
            continue
        lista = "; ".join(f"{NOMBRES_FASES[_ROADMAP_IDS.index(d)]} ({m})" if m else NOMBRES_FASES[_ROADMAP_IDS.index(d)]
                          for d, m in fases.items())
        linea = soup.new_tag("p", attrs={"class": f"rdm-linea-fases {clase}"})
        linea.string = f"{inicio}: {lista}"
        flujo.insert_after(linea)


def inject_roadmap_into_html(
    data_sources_output: str,
    ingestion_output: str,
    storage_output: str,
    etl_output: str,
    modeling_output: str,
    ml_output: str,
    bi_output: str,
    etiquetas: dict[str, str] | None = None,
    no_aplican: dict[str, str] | None = None,
    ya_implantadas: dict[str, str] | None = None,
) -> None:
    """Inyecta los 7 outputs individuales del roadmap en sus respectivos divs en el HTML.
    Fallo 102: y, con `etiquetas`, las del flujo y las de cada tarjeta (las de etiquetas_del_flujo).
    Fallo 131: las fases de `no_aplican` ({div_id: motivo}) no salen; las de `ya_implantadas` ({div_id: lo que tiene
    el cliente}) salen en el flujo atenuadas, como «Ya implantado», y sin tarjeta."""
    phase_outputs = [data_sources_output, ingestion_output, storage_output, etl_output, modeling_output, ml_output, bi_output]
    html = html_del_informe(REPORT_PATH)  # fallo 116: el informe en curso; si aún no hay, la plantilla
    soup = parse_html(html)
    no_aplican, ya_implantadas = no_aplican or {}, ya_implantadas or {}

    for div_id, raw_text in zip(_ROADMAP_IDS, phase_outputs):
        if div_id in no_aplican or div_id in ya_implantadas:
            continue
        target = soup.find("div", id=div_id)
        if target is None:
            print(f"[roadmap_builder] WARN: div id='{div_id}' no encontrado en el HTML")
            continue
        target.clear()
        fragment = parse_html(md_to_html(raw_text))
        body = fragment.find("body") or fragment
        for child in list(body.children):
            target.append(child.__copy__())
    if etiquetas:
        _poner_etiquetas(soup, etiquetas)
    if no_aplican or ya_implantadas:
        _quitar_fases(soup, no_aplican, ya_implantadas)

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




def roadmap_builder(combined_response: dict, problem_case: dict | None = None,
                    fases: dict[str, tuple[str, str]] | None = None) -> dict:
    """fases (fallo 131): las de leer_fases(); solo se generan las «sí». None: las 7."""
    agents_config = load_agents_config()
    estados = {div_id: (fases or {}).get(div_id, (SI, "")) for div_id in _ROADMAP_IDS}
    aplican = [n for n, div_id in enumerate(_ROADMAP_IDS) if estados[div_id][0] == SI]
    no_aplican = {d: frase for d, (estado, frase) in estados.items() if estado == NO}
    ya_implantadas = {d: frase for d, (estado, frase) in estados.items() if estado == YA}
    # Gateway de modelos: cada fase pide el suyo (data_structures/modelos.yaml)
    fuentes_de_datos = _make_agent("data_sources_agent", agents_config, gateway.modelo("roadmap_fuentes"))
    ingesta_de_datos = _make_agent("ingestion_agent", agents_config, gateway.modelo("roadmap_ingesta"))
    data_storage = _make_agent("storage_agent", agents_config, gateway.modelo("roadmap_almacenamiento"))
    procesamiento_etl = _make_agent("etl_processing_agent", agents_config, gateway.modelo("roadmap_etl"))
    modelado_y_serving = _make_agent("modeling_serving_agent", agents_config, gateway.modelo("roadmap_modelado"))
    ml_mlops = _make_agent("ml_mlops_agent", agents_config, gateway.modelo("roadmap_ml"))
    visualizacion = _make_agent("bi_visualization_agent", agents_config, gateway.modelo("roadmap_bi"))

    # Fallo 88: con el inventario de servicios de la arquitectura, el resumen ya no se llama (una llamada menos): cada
    # fase recibe el inventario, con la orquestación, y la sección de la propuesta que desarrolla. Sin inventario, el
    # resumen, como antes
    propuesta = combined_response if isinstance(combined_response, dict) else {}
    filas = propuesta.get("inventario") or []
    if filas:
        resumen = ""
        progreso.avanzar("Inventario de servicios para el roadmap")  # fallo 94: el mismo paso que el resumen
    else:
        resumen = resumir_solucion(combined_response, agents_config)
        progreso.avanzar("Resumen de la propuesta")  # fallo 94

    # Las 7 tareas reciben, además del resumen, los datos del cliente del discovery.
    if problem_case is None:
        problem_case = load_problem_case()
    # Fallo 73: la arquitectura de la propuesta va entera, como referencia común de las 7 fases (ya sin importes)
    arquitectura = propuesta.get("architecture", "")
    contexto = ((f"{resumen}\n\n" if resumen else "")
                + (f"{inventario.para_agentes(filas)}\n{SOLO_INVENTARIO}\n\n" if filas else "")
                + (f"PROPUESTA DE ARQUITECTURA (la referencia común de las {len(aplican)} fases):\n"
                   f"{sin_emojis(arquitectura)}\n\n" if arquitectura else "")
                + f"{contexto_cliente(problem_case)}\n{PLATAFORMA}{_referencia(len(aplican))}")  # fallo 131
    # Fallo 88: lo que cada fase leía del resumen, ahora de su sección de la propuesta
    extra = {n: f"\n{titulo} (la sección de la propuesta que desarrolla tu fase):\n{sin_emojis(propuesta[campo])}\n\n"
             for n, (campo, titulo) in SECCION_DE_CADA_FASE.items() if filas and propuesta.get(campo)}

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
            # Fallo 130: Modelado / Análisis es el modelado dimensional y la analítica, no solo lo que sirve al ML
            "Basándote en los datos procesados de la última capa del Data Lake de la propuesta y en los "
            "casos de uso de negocio del cliente, diseña el modelado y el análisis de los datos. Define si "
            "aplica un modelo dimensional (esquema en estrella), un modelo más flexible o una capa semántica "
            "en la herramienta de BI del cliente o en un servicio de modelo semántico. Especifica las entidades principales "
            "(tablas de hechos y dimensiones o equivalentes), las métricas de negocio clave que "
            "deben estar precalculadas, los análisis que pide el cliente (descriptivos, comparativos, de tendencia o, "
            "si el caso los pide, predictivos), la estrategia de actualización de datos en esta capa y "
            "cómo se expondrán los datos para consumo por BI, APIs y, si los hay, modelos de ML. "
            "Prioriza la comprensibilidad del modelo para el equipo de negocio del cliente sobre "
            "la elegancia técnica."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de modelado y análisis con las siguientes secciones:\n"
            "1. Decisión sobre el tipo de modelo analítico y justificación\n"
            "2. Entidades principales del modelo (hechos, dimensiones o equivalentes)\n"
            "3. Métricas de negocio clave y cálculos precalculados\n"
            "4. Análisis que se harán sobre el modelo y para qué\n"
            "5. Estrategia de actualización y latencia de datos\n"
            "6. Exposición de datos para BI, APIs y, si los hay, modelos de ML\n"
            "7. Consideraciones de rendimiento y escalabilidad"
        ) + EXTENSION,
        agent=modelado_y_serving,
    )

    ml_mlops_task = Task(
        description=(
            "PROPUESTAS DE SOLUCIÓN GENERADAS POR EL EQUIPO:\n"
            f"{contexto}"
            # Fallo 130: Ops es la operación y la monitorización de toda la solución y, si hay modelos, su MLOps
            "Basándote en la propuesta de arquitectura y en el equipo del cliente, diseña cómo se opera la "
            "solución en producción: monitorización y alertas de los pipelines, del almacenamiento y de los "
            "cuadros de mando con los servicios de la plataforma (por ejemplo, Azure Monitor en Azure, Amazon "
            "CloudWatch en AWS o Cloud Monitoring en Google Cloud), gestión de incidencias, despliegues y "
            "entornos, copias de seguridad y control del gasto. Si la propuesta incluye modelos de IA o ML, "
            "añade su MLOps: para cada caso de uso, el tipo de modelo y su justificación (no siempre el más "
            "complejo es el mejor), los datos de entrenamiento y su disponibilidad, la infraestructura de ML "
            "de la plataforma (por ejemplo, Azure Machine Learning en Azure, Amazon SageMaker en AWS o Vertex AI en "
            "Google Cloud), el ciclo "
            "de vida del modelo (entrenamiento, validación, despliegue, monitorización y reentrenamiento) y "
            "sus métricas técnicas y de negocio. Si no incluye modelos, no los inventes. "
            "Sé explícito sobre qué puede operar el equipo actual del cliente "
            "y qué requeriría madurez adicional o soporte externo."
        ),
        expected_output=(
            "Una propuesta, sin tablas, de operación (Ops) con las siguientes secciones:\n"
            "1. Monitorización y alertas de la solución\n"
            "2. Operación diaria: incidencias, despliegues, entornos y copias de seguridad\n"
            "3. Control del gasto y del rendimiento\n"
            "4. MLOps, solo si la propuesta incluye modelos: casos de uso, modelo por caso, datos, infraestructura, "
            "ciclo de vida y métricas\n"
            "5. Quién opera cada parte y qué requiere madurez adicional o una segunda fase"
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
    for n, texto in extra.items():  # fallo 88: la sección de la propuesta de cada fase, detrás del contexto común
        tareas[n].description = tareas[n].description.replace(contexto, contexto + texto, 1)
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

    # Fallo 131: las fases que no aplican o que el cliente ya tiene no se generan; la barra avanza igual por ellas
    for n in range(len(tareas)):
        if n not in aplican:
            estado, frase = estados[_ROADMAP_IDS[n]]
            cual = "no aplica a este proyecto" if estado == NO else "ya implantada, se reutiliza"
            print(f"[roadmap_builder] {TITULOS[n]}: {cual} ({frase or 'sin motivo'})")
            progreso.avanzar(f"Roadmap: {TITULOS[n]} ({'no aplica' if estado == NO else 'ya implantado'})")
    print(f"\n\n[roadmap_builder] Ejecutando las {len(aplican)} fases del roadmap"
          + (" a la vez" if HILOS > 1 else " en orden") + "...\n")
    inicio = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, HILOS), thread_name_prefix="roadmap") as hilos:
        # Fallo 94: el hilo de la sesión espera a las fases pintando la barra cada vez que acaba una
        progreso.esperar([hilos.submit(ejecutar, n, tareas[n]) for n in aplican])
    vacias = [TITULOS[n] for n in aplican if not salidas[n].strip()]
    print(f"\n[roadmap_builder] Roadmap: {len(aplican) - len(vacias)} de {len(aplican)} fases en "
          f"{time.monotonic() - inicio:.0f} s" + (f"; sin texto: {', '.join(vacias)}" if vacias else "") + "\n")
    captured_outputs = salidas

    # Fallo 44: los importes salen solo en la sección de costes, donde se suman en Python
    captured_outputs[:] = [sin_importes(texto, problem_case, f"roadmap {n}") for n, texto in enumerate(captured_outputs, 1)]
    # Fallo 83: la fase que nombre un servicio que no está en la región o está retirado lleva una nota visible
    captured_outputs[:] = [con_nota_de_servicios(texto, problem_case, f"roadmap: {TITULOS[n]}") if texto.strip() else texto
                           for n, texto in enumerate(captured_outputs)]

    if len(captured_outputs) == 7:
        inject_roadmap_into_html(*captured_outputs, etiquetas=etiquetas_del_flujo(filas, problem_case),  # fallo 102
                                 no_aplican=no_aplican, ya_implantadas=ya_implantadas)  # fallo 131
        print("[roadmap_builder] Roadmap inyectado en HTML exitosamente.\n")
    else:
        print(f"[roadmap_builder] WARN: Se esperaban 7 outputs pero se capturaron {len(captured_outputs)}. No se inyecta HTML.\n")

    combined_output = "\n".join(captured_outputs)
    return combined_output

