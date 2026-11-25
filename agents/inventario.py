"""Fallo 88: el inventario de servicios de la arquitectura.

La arquitectura termina con una lista cerrada, una línea por servicio: servicio | capa | fase | alternativa. Python la
lee y, con ella, costes valora exactamente esos servicios desde su fase, perfiles y equipo los tienen en cuenta en
cada alcance, y las 7 fases del roadmap trabajan sobre la misma lista (con la orquestación) en lugar del resumen. El 12
nov la ingesta orquestaba con Glue Workflows y el ETL con Step Functions, y aparecían API Gateway y Glue Schema
Registry, que no estaban en la arquitectura ni en los costes. Si la arquitectura no trae la lista, todo sigue como
antes y el log lo avisa.
"""
import logging
import re

from utils import (INTEGRATION_RUNTIME, PUERTA_DE_ENLACE, _normalize_text, es_valor_de_relleno,
                   necesita_integration_runtime, necesita_puerta_de_enlace, revisar_servicios)

logger = logging.getLogger("aidm")

TITULO = "INVENTARIO DE SERVICIOS"

# Lo que se pide al agente de arquitectura al final de su respuesta
INSTRUCCION = (
    f"Termina la respuesta con un bloque que empiece por la línea «{TITULO}» y siga con una línea por cada servicio, "
    "herramienta o licencia que entra en la solución, sin dejarte ninguno (también la orquestación, la seguridad, la "
    "monitorización y la herramienta de BI), con este formato:\n"
    "- servicio | capa | fase | alternativa\n"
    "Servicio: el nombre exacto de un solo servicio, el que eliges. Capa: fuentes, ingesta, almacenamiento, "
    "procesamiento, orquestación, analítica, IA, visualización, seguridad o gobierno. Fase: MVP si se usa desde el "
    "MVP, completo si solo entra después del MVP (o MVP si el cliente no pide un MVP). Alternativa: la opción que "
    "descartas para ese papel, o «—». Ningún otro texto después del bloque."
)

_FASES = {"mvp": "mvp", "piloto": "mvp", "fase 1": "mvp", "completo": "proyecto_completo",
          "proyecto completo": "proyecto_completo", "fase 2": "proyecto_completo", "resto": "proyecto_completo"}
_PREFIJOS = ("amazon ", "aws ", "azure ", "microsoft ", "google cloud ", "google ", "oci ", "oracle ", "ibm ")


def _fase(texto: str) -> str:
    normal = _normalize_text(texto or "")
    return next((v for k, v in _FASES.items() if k in normal), "mvp")


def leer(texto: str) -> tuple[str, list[dict]]:
    """El texto de la arquitectura sin el bloque del inventario, y sus filas ({servicio, capa, fase, alternativa}).
    Sin bloque, el texto tal cual y una lista vacía."""
    lineas = (texto or "").splitlines()
    inicio = next((n for n in range(len(lineas) - 1, -1, -1)
                   if TITULO.lower() in _normalize_text(lineas[n]).replace("*", "").replace("#", "")), None)
    if inicio is None:
        return texto, []
    filas = []
    for linea in lineas[inicio + 1:]:
        limpia = linea.strip()
        if not limpia or limpia.startswith("```"):
            if filas:
                break
            continue
        if "|" not in limpia:
            if filas:
                break
            continue
        celdas = [c.strip(" *`") for c in limpia.strip("-*• ").strip("|").split("|")]
        if len(celdas) < 3 or set("".join(celdas)) <= set("-: ") or _normalize_text(celdas[0]) == "servicio":
            continue  # la cabecera o la fila de guiones de una tabla
        alternativa = celdas[3] if len(celdas) > 3 else "—"
        filas.append({"servicio": celdas[0], "capa": celdas[1], "fase": _fase(celdas[2]),
                      "alternativa": "" if alternativa in ("—", "-", "") else alternativa})
    sin_bloque = "\n".join(lineas[:inicio]).rstrip()
    return sin_bloque, filas


# Fallo 108: los guiones y rayas Unicode (‐ ‑ ‒ – — ―, y el signo menos) cuentan como el guion normal
_GUIONES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")


def _clave(nombre: str) -> str:
    """'Amazon S3 (capas Bronze y Gold)' → 's3': sin paréntesis ni el nombre de la nube, para comparar.
    Fallo 108: los paréntesis se quitan antes de normalizar, que se comía el «)» final y dejaba «s3 (capas…»."""
    texto = re.sub(r"\([^)]*\)?", " ", (nombre or "").translate(_GUIONES))
    normal = re.sub(r"\s+", " ", _normalize_text(texto)).strip()
    for prefijo in _PREFIJOS:
        if normal.startswith(prefijo):
            normal = normal[len(prefijo):]
    return normal.strip()


def _base(nombre: str) -> str:
    """Fallo 108: la clave sin lo que va detrás de « - », « – » o una coma: 'AWS DMS (DMS) - instancia t3.medium' →
    'dms'. Es el nombre del servicio; lo demás es la instancia, el uso o las licencias."""
    return re.split(r"\s-\s|,", _clave(nombre))[0].strip()


# Fallo 132 (parte 3) y 136: lo que el cliente ya usa según sus datos, y si un servicio está entre ello
CAMPOS_DEL_CLIENTE = ("stack_tecnologico_actual", "madurez_analitica_actual", "herramienta_de_visualizacion")
_VACIAS = {"de", "del", "la", "el", "los", "las", "en", "para", "y", "e", "con", "por", "a", "al", "su", "sus", "un",
           "una", "-"}
_PALABRA = re.compile(r"[a-z0-9][a-z0-9.+-]*")


def trozos_del_cliente(problem_case: dict | None, campos: tuple[str, ...] = CAMPOS_DEL_CLIENTE) -> list[str]:
    """Lo que el cliente ya usa según sus datos, normalizado y en trozos (por comas, puntos y coma, dos puntos,
    paréntesis o « y »), sin los rellenos: 'Microsoft Dynamics 365 Business Central, SQL Server en local para los TPV y
    SharePoint' → ['microsoft dynamics 365 business central', 'sql server en local para los tpv', 'sharepoint']."""
    textos = []
    for campo in campos:
        valor = (problem_case or {}).get(campo)
        valor = valor.get("value") if isinstance(valor, dict) else valor
        textos += [v for v in (valor if isinstance(valor, list) else [valor])
                   if isinstance(v, str) and not es_valor_de_relleno(v)]
    trozos = re.split(r"[,;:()\[\]·]|\s+(?:y|e|and)\s+", _normalize_text(" , ".join(textos)))
    return [t.strip() for t in trozos if t.strip()]


def lo_tiene_el_cliente(nombre: str, trozos: list[str]) -> bool:
    """Todas las palabras significativas del nombre (sin paréntesis ni palabras vacías) están en un mismo trozo de
    trozos_del_cliente(), aunque el trozo sea largo; sin la nube delante, si quedan al menos dos. 'TPV SQL Server' casa
    con 'sql server en local para los tpv'; 'Power BI Premium' no casa con 'power bi': lo nuevo no sale como suyo."""
    entero = re.sub(r"\s+", " ", _normalize_text(re.sub(r"\([^)]*\)?", " ", (nombre or "").translate(_GUIONES))))
    candidatos = [set(_PALABRA.findall(entero)) - _VACIAS]
    sin_nube = set(_PALABRA.findall(_clave(nombre))) - _VACIAS
    if len(sin_nube) >= 2:
        candidatos.append(sin_nube)
    de_cada_trozo = [set(_PALABRA.findall(t)) for t in trozos]
    return any(palabras and any(palabras <= t for t in de_cada_trozo) for palabras in candidatos)


def corresponde(servicio: str, componente: str) -> bool:
    """Un componente de costes es de este servicio del inventario (o al revés).
    Fallo 108: se comparan las bases y por palabras enteras, para que «Power BI (Microsoft 365)» y «Power BI Pro -
    licencias» sean el mismo, pero «Kinesis Data Streams» y «Kinesis Data Firehose» no. Antes, con la cadena entera,
    «Amazon S3» también casaba con «Glue Data Catalog sobre S3»."""
    # Fallo 108 (parte 2): la puerta de enlace de Power BI con el nombre de su máquina. El 21 nov, «Amazon EC2 Windows
    # t3.medium (Power BI gateway)» no casaba con la fila de la puerta de enlace: la base quita los paréntesis
    if all(_TEXTO_PUERTA.search(_normalize_text(n or "")) for n in (servicio, componente)):
        return True
    a, b = _base(servicio), _base(componente)
    if not a or not b:
        return False
    dentro = lambda corto, largo: re.search(rf"(?<![\w-]){re.escape(corto)}(?![\w-])", largo) is not None
    return dentro(a, b) or dentro(b, a)


def tabla(filas: list[dict]) -> str:
    """El inventario en markdown, para el final de la sección de arquitectura."""
    fases = {"mvp": "MVP", "proyecto_completo": "Proyecto completo"}
    lineas = ["#### Inventario de servicios", "", "| Servicio | Capa | Desde | Alternativa descartada |",
              "| --- | --- | --- | --- |"]
    lineas += [f"| {f['servicio']} | {f['capa']} | {fases[f['fase']]} | {f['alternativa'] or '—'} |" for f in filas]
    return "\n".join(lineas)


def para_agentes(filas: list[dict]) -> str:
    """El inventario para los prompts de costes, perfiles y roadmap."""
    fases = {"mvp": "desde el MVP", "proyecto_completo": "solo después del MVP"}
    return (f"{TITULO} (la lista cerrada de la arquitectura: usa estos y ningún otro)\n"
            + "\n".join(f"- {f['servicio']} · {f['capa']} · {fases[f['fase']]}" for f in filas))


# Fallo 89: el 12 nov la arquitectura dejaba 7 elecciones sin decidir («Delta Lake (o Apache Iceberg)», «Kinesis Data
# Streams o Firehose», «Rekognition o SageMaker»…). El prompt obliga a elegir una y dejar la otra como alternativa en el
# inventario; Python busca en el texto «la elegida o su alternativa» (o al revés) y, si sale, lo avisa.
REGLA_ELEGIR = (
    "Elige una sola opción para cada papel de la solución (por ejemplo, el formato de tabla: Delta Lake o Apache "
    "Iceberg; la ingesta en streaming: Kinesis Data Streams o Data Firehose) y escribe solo la elegida, con su "
    "motivo. La que descartas va únicamente en la columna alternativa del inventario. No escribas «X o Y», «X/Y» ni "
    "«X (o Y)» para servicios o formatos."
)


def sin_decidir(texto: str, filas: list[dict]) -> list[str]:
    """Fallo 89: las elecciones que el texto deja abiertas entre un servicio del inventario y su alternativa."""
    normal = re.sub(r"[*_`()]", " ", _normalize_text(texto or ""))
    normal = re.sub(r"\s+", " ", normal)
    abiertas = []
    for f in filas:
        if not f.get("alternativa"):
            continue
        a, b = _clave(f["servicio"]), _clave(f["alternativa"])
        if not a or not b:
            continue
        patron = rf"\b{re.escape(a)}\b[^.;\n]{{0,25}}?\s(?:o|u|or|/)\s[^.;\n]{{0,25}}?\b{re.escape(b)}\b"
        inverso = rf"\b{re.escape(b)}\b[^.;\n]{{0,25}}?\s(?:o|u|or|/)\s[^.;\n]{{0,25}}?\b{re.escape(a)}\b"
        if re.search(patron, normal) or re.search(inverso, normal):
            abiertas.append(f"{f['servicio']} o {f['alternativa']}")
            logger.warning("[inventario] La arquitectura deja sin decidir %s o %s (el inventario elige %s)",
                           f["servicio"], f["alternativa"], f["servicio"])
    return abiertas


def nota_sin_decidir(abiertas: list[str]) -> str:
    """Fallo 89: la nota visible, en markdown, para el principio de la arquitectura."""
    if not abiertas:
        return ""
    return ("> **Ojo: la arquitectura deja elecciones sin decidir** (" + "; ".join(abiertas) + "). Vale la primera de "
            "cada par, la del inventario de servicios: la otra es la alternativa descartada.\n\n")


# Fallo 90 (parte 4): la puerta de enlace de datos de Power BI nombrada en un texto (no la de IoT ni API Gateway)
_TEXTO_PUERTA = re.compile(r"\b(?:on-premises data gateway|data gateway|power bi gateway|gateway de (?:datos|power bi)"
                           r"|puerta de enlace de (?:datos|power bi))\b")
_MAQUINA = re.compile(r"\b(?:ec2|vm|maquina virtual|virtual machine|instancia)\b")


def con_puerta_de_enlace(filas: list[dict], problem_case: dict | None,
                         arquitectura: str = "") -> tuple[list[dict], str]:
    """Fallo 90: si hace falta la puerta de enlace de Power BI y el inventario no la trae, se añade (desde el MVP), y
    la nota para la arquitectura lo dice. Así la valoran los costes.
    Fallo 90 (parte 4): una máquina (EC2, VM…) en la capa de visualización, o la arquitectura que describe la puerta de
    enlace, cuentan como que ya está. El 21 nov se añadió con «Amazon EC2» en visualización."""
    if not filas or not necesita_puerta_de_enlace(problem_case):
        return filas, ""
    if any(re.search(r"gateway|puerta de enlace", _normalize_text(f["servicio"])) for f in filas):
        return filas, ""
    if any(_normalize_text(f["capa"]).startswith("visualiz") and _MAQUINA.search(_normalize_text(f["servicio"]))
           for f in filas) or _TEXTO_PUERTA.search(_normalize_text(arquitectura or "")):
        return filas, ""
    logger.warning("[inventario] Power BI con los datos fuera de Azure y sin puerta de enlace en el inventario: se añade")
    fila = {"servicio": PUERTA_DE_ENLACE, "capa": "visualización", "fase": "mvp", "alternativa": ""}
    return filas + [fila], ("> **Ojo: falta la puerta de enlace de Power BI.** Con los datos fuera de Azure, Power BI "
                            "Service solo llega a ellos a través de una puerta de enlace de datos local en una máquina "
                            "Windows: se ha añadido al inventario y a los costes.\n\n")


def con_integration_runtime(filas: list[dict], problem_case: dict | None) -> tuple[list[dict], str]:
    """Fallo 105: si el inventario trae Data Factory, hay datos en los servidores del cliente y falta el self-hosted
    integration runtime, se añade (desde la fase de Data Factory), y la nota para la arquitectura lo dice."""
    if not filas or not necesita_integration_runtime(problem_case):
        return filas, ""
    factory = next((f for f in filas if "data factory" in _normalize_text(f["servicio"])), None)
    if factory is None or any(re.search(r"integration runtime|\bshir\b", _normalize_text(f["servicio"])) for f in filas):
        return filas, ""
    logger.warning("[inventario] Data Factory con datos en los servidores del cliente y sin self-hosted integration "
                   "runtime en el inventario: se añade")
    fila = {"servicio": INTEGRATION_RUNTIME, "capa": "ingesta", "fase": factory["fase"], "alternativa": ""}
    return filas + [fila], ("> **Ojo: falta el self-hosted integration runtime de Data Factory.** Con datos en los "
                            "servidores del cliente, Data Factory solo llega a ellos a través de él, en una máquina "
                            "Windows con acceso a su red: se ha añadido al inventario y a los costes.\n\n")


# Fallo 125: el 21 nov las secciones daban a varios servicios otra fase que el inventario: Step Functions, del MVP, iba
# «post-MVP» en la estrategia, y servicios del proyecto completo salían en el MVP de la estrategia y la gobernanza
REGLA_FASES = (
    f"La fase de cada servicio es la del {TITULO} con que termina la arquitectura: un servicio que allí entra solo "
    "después del MVP no va en el MVP ni en su plan, y uno del MVP no se deja para después. Si crees que otra fase "
    "sería mejor, dilo como recomendación, sin cambiar la del inventario."
)
_DESPUES = re.compile(r"\b(?:post-?mvp|(?:despues del|tras el|fuera del) (?:mvp|piloto)|proyecto completo|fase [2-9]"
                      r"|after the mvp)\b")
_ANTES = re.compile(r"\b(?:mvp|piloto|fase 1)\b")


def fases_que_no_cuadran(texto: str, filas: list[dict], seccion: str) -> list[str]:
    """Fallo 125: los servicios del inventario que una frase de la sección pone en otra fase (en el MVP uno del proyecto
    completo, o después del MVP uno del MVP). Solo lo dice el log ([fases]); el informe no lleva nota."""
    frases = [_normalize_text(f) for f in re.split(r"(?<=[.;!?])\s+|\n+", texto or "") if f.strip()]
    distintas = []
    for fila in filas:
        nombre = _base(fila["servicio"])
        if len(nombre) < 3:
            continue
        patron = re.compile(rf"(?<![\w-]){re.escape(nombre)}(?![\w-])")
        for frase in frases:
            if not patron.search(frase):
                continue
            despues = bool(_DESPUES.search(frase))
            antes = bool(_ANTES.search(_DESPUES.sub(" ", frase)))
            if despues == antes or antes == (fila["fase"] == "mvp"):
                continue  # la frase no dice la fase, dice las dos o dice la del inventario
            logger.warning("[fases] %s pone %s %s; el inventario, %s", seccion, fila["servicio"],
                           "después del MVP" if despues else "en el MVP",
                           "desde el MVP" if fila["fase"] == "mvp" else "solo después del MVP")
            distintas.append(fila["servicio"])
            break
    return distintas


def comprobar(filas: list[dict], problem_case: dict | None) -> list[dict]:
    """Los servicios del inventario que no se pueden usar en la nube elegida (fallo 83). Lo dice el log."""
    if not filas:
        logger.warning("[inventario] La arquitectura no trae el inventario de servicios: costes y roadmap siguen "
                       "como antes (con el resumen)")
        return []
    print(f"[inventario] {len(filas)} servicios en la arquitectura ({sum(f['fase'] == 'mvp' for f in filas)} desde "
          f"el MVP): {', '.join(f['servicio'] for f in filas)}")
    return revisar_servicios("\n".join(f["servicio"] for f in filas), problem_case, "inventario")
