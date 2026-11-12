import json
import base64
import yaml
from pathlib import Path
from typing import Any
from typing import List, Literal, Optional

import litellm
from bs4 import BeautifulSoup, FeatureNotFound
from crewai import Agent, LLM
from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import BaseModel, Field
from gateway import build_llm # gateway de modelos: el tope y la reserva de cada modelo, en data_structures/modelos.yaml

import logging
import re
import unicodedata




litellm.drop_params = True

# CrewAI 1.14.x añade {"cache_breakpoint": True} a todos los mensajes para el
# prompt caching de Anthropic, pero el proveedor de Groq no lo filtra y la API
# lo rechaza. Se reemplaza la función por un no-op para evitar el error.
try:
    import crewai.llms.cache as _crewai_cache
    _crewai_cache.mark_cache_breakpoint = lambda msg: msg
except Exception:
    pass


LIST_FIELDS = {"evidencias"}
TEXT_FIELDS = {
    "nombre_del_cliente",
    "tamaño_empresa",
    "sector",
    "tiene_equipo_datos",
    "tipo_de_servicio",
    "objetivo_del_proyecto",
    "modelo_de_despliegue",
    "cloud_provider",
    "tipo_de_arquitectura",
    "herramienta_de_visualizacion",
    "enfoque_ai",
    "integration_scope",
    "volumen_de_datos",
    "madurez_analitica_actual",
    "tamano_equipo_tecnico",
    "stack_tecnologico_actual",
    "restriccion_presupuestaria",
    "plazo_objetivo",
    "regulacion_y_compliance",
    "kpis_de_exito",
}
VALID_STATUSES = {"pending fields", "completed"}
# Fallo 67: la nube en la que se diseña la solución. La pone el agente de plataforma después del discovery; no es un
# campo que pregunte el discovery, así que no está en TEXT_FIELDS.
PLATAFORMA_OBJETIVO = "plataforma_objetivo"
PROBLEM_CASE_PATH = Path("data_structures/problem_case.json")
AGENTS_FILE = Path("data_structures/agents.yaml")
logger = logging.getLogger("aidm")

#Prueba para fallback por si uno de los modelos no llega a terminar, se puede usar otro modelo de reserva con mas tokens
'''
~cambiado, ahora el gateway se encarga de todo esto~

MODEL_RULES = {
    "groq/openai/gpt-oss-20b":  {"max_tokens": 4096, "fallback": "cloudflare/@cf/openai/gpt-oss-20b"},
    "groq/openai/gpt-oss-120b": {"max_tokens": 6000, "fallback": "cloudflare/@cf/openai/gpt-oss-120b"},
}
FALLBACK_MAX_TOKENS = 6000
'''

def load_problem_case() -> dict[str, Any]:
    with open(PROBLEM_CASE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

'''
~cambiado, ahora el gateway se encarga de todo esto~
def build_llm(model_name: str):
    """LLM para CrewAI con reintentos, tope de salida explícito y modelo de reserva."""
    if model_name.startswith("gemini/"):
        return model_name  # cliente nativo de CrewAI, no admite estos parámetros
    rules = MODEL_RULES.get(model_name, {})
    params = {"num_retries": 2}
    # Cloudflare corta a 256 tokens si no se le indica un tope
    max_tokens = rules.get("max_tokens") or (4096 if model_name.startswith("cloudflare/") else None)
    if max_tokens:
        params["max_tokens"] = max_tokens
    if rules.get("fallback"):
        params["fallbacks"] = [{"model": rules["fallback"], "max_tokens": FALLBACK_MAX_TOKENS}]
    return LLM(model=model_name, **params)
'''
    
def _make_agent(key: str, agents_config, model_name) -> Agent:
    data = agents_config.get(key, {})
    return Agent(
        role=data.get("role"),
        goal=data.get("goal"),
        backstory=data.get("backstory"),
        llm=build_llm(model_name),
        verbose=False,
        allow_delegation=False,
    )

# --------------- Utils para el Discovery Agent ---------------

class FieldUpdate(BaseModel):
    field_key: str = Field(
        description="Clave del campo a actualizar en el JSON problem_case; debe existir y no se pueden crear campos nuevos."
    )
    value: str | List[str] = Field(
        description="Texto o lista de textos a usar para la actualización del campo."
    )
    operation: Literal["replace", "append", "merge"] = Field(
        default="replace",
        description="Tipo de operación sobre el campo.",
    )


class ProblemScopeUpdateRequest(BaseModel):
    name: str = "update_problem_scope_tool"
    description: str = (
        "Actualiza campos específicos en problem_case.json relacionados con el alcance del problema. "
        "Argumentos: updates (lista de objetos con field_key, value y operation), "
        "update_status (nuevo valor del campo status si se desea actualizar). "
        "IMPORTANTE: Solo actualiza campos relacionados con el alcance del problema, "
        "no modifiques otros campos ni la estructura general del JSON."
    )
    updates: List[FieldUpdate] = Field(
        description="Lista de actualizaciones a aplicar sobre problem_case.json."
    )
    update_status: Optional[Literal["pending fields", "completed"]] = Field(
        default=None,
        description="Nuevo valor de status cuando proceda.",
    )


# Valores con los que el LLM "rellena" un campo del que no sabe nada.
PLACEHOLDER_EXACT = {
    "desconocido", "desconocida", "pendiente", "por determinar", "por definir",
    "sin datos", "sin informacion", "sin especificar", "n/a", "na", "n.a", "n/d", "nd",
    "-", "--", "?", "unknown", "tbd", "none", "null",
}
PLACEHOLDER_PREFIXES = (
    "no mencionad", "no se menciona", "no especificad", "no indicad", "no proporcionad",
    "no facilitad", "not mentioned", "not specified", "not provided",
)
# Una respuesta explícita del usuario ("no aplica", "no lo sé") se guarda con este prefijo.
CONFIRMED_PREFIX = "confirmado por el usuario"


def _normalize_text(value: str) -> str:
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    texto = " ".join(sin_tildes.lower().split())
    return texto.strip(" .,;:\"'()[]¿¡")

# Fallo 36: un «no lo sé» o un «no aplica» se guarda con el prefijo, para que los agentes no lo lean como un dato.
VALORES_CERRADOS = {
    "no_lo_sabe": "Confirmado por el usuario: no lo sabe",
    "no_aplica": "Confirmado por el usuario: no aplica",
}
_FRASES_CERRADAS = {
    "no_lo_sabe": {
        "no lo se", "no se", "nose", "ni idea", "no tengo ni idea", "desconozco", "lo desconozco", "no lo sabemos",
        "no sabemos", "no sabria decirte", "no sabria decir", "no te sabria decir", "no estoy seguro",
        "no estoy segura", "no lo tengo claro", "no lo sabe", "no sabe",
        "i don't know", "i dont know", "don't know", "dont know", "i do not know", "no idea", "not sure",
        "i'm not sure", "im not sure", "we don't know", "no clue",
    },
    "no_aplica": {
        "no aplica", "no nos aplica", "no aplica a nuestro proyecto", "no aplica al proyecto", "no aplica en nuestro caso",
        "no procede", "no es aplicable", "n/a", "not applicable", "does not apply", "doesn't apply",
    },
}
_RELLENO_INICIO = re.compile(r"^(?:pues|la verdad|sinceramente|uf+|mm+|hmm+)[\s,.]+")
_RELLENO_FINAL = re.compile(r"[\s,.]+(?:la verdad|sinceramente|todavia|aun|de momento|sorry)$")


def respuesta_cerrada(texto: Any) -> str | None:
    """'no_lo_sabe' o 'no_aplica' si todo el texto es esa respuesta («No lo sé.», «Ni idea, la verdad»); si no, None.

    Solo frases enteras: «No sé, unos 2 TB» lleva un dato y no cuenta.
    """
    if not isinstance(texto, str):
        return None
    normalizado = _normalize_text(texto.replace("’", "'"))
    normalizado = _RELLENO_FINAL.sub("", _RELLENO_INICIO.sub("", normalizado)).strip(" .,;:!?…")
    for estado, frases in _FRASES_CERRADAS.items():
        if normalizado in frases:
            return estado
    return None


def es_valor_de_relleno(value: Any) -> bool:
    """True si el valor no es un dato real: vacío o un relleno tipo 'no mencionado'."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    texto = _normalize_text(value)
    if not texto:
        return True
    if texto.startswith(CONFIRMED_PREFIX):
        return False
    return texto in PLACEHOLDER_EXACT or texto.startswith(PLACEHOLDER_PREFIXES)

#Necesario para que no se pierda contexto entre las 7 tareas ni entre las crews.
#MODIFICADO, para que no se pierdan los datos del cliente y no se guarden valores de relleno
REGLAS_DATOS_CLIENTE = (
    "CÓMO USAR ESTOS DATOS:\n"
    "- Solo lo confirmado y las notas del discovery son hechos sobre el cliente. No le atribuyas herramientas, "
    "licencias, sistemas, cifras ni preferencias que no estén aquí.\n"
    "- Si necesitas un dato que no está, no lo inventes: escribe «Por confirmar: …» si hay que preguntárselo al "
    "cliente, o «Se asume: …» si es una suposición tuya para poder avanzar, y di de qué depende.\n"
    "- No escribas cifras del cliente que no estén aquí: porcentajes actuales, volúmenes, ahorros, costes por "
    "incidencia, fechas o número de usuarios.\n"
)

REGLA_PRESUPUESTO = (
    "- Las cifras de presupuesto y coste salen solo de la sección de costes: no hagas presupuestos ni repartos de "
    "importes, ni pongas precios o porcentajes de contingencia. Puedes comparar opciones por coste sin dar cifras.\n"
)

def _estado_del_valor(valor: Any) -> tuple[str, str]:
    """('confirmado' | 'no_lo_sabe' | 'no_aplica' | 'sin_dato', valor limpio) de un campo de texto del caso."""
    if not isinstance(valor, str) or es_valor_de_relleno(valor):
        return "sin_dato", ""
    texto = valor.strip()
    normalizado = _normalize_text(texto)
    if normalizado.startswith(CONFIRMED_PREFIX):
        resto = normalizado[len(CONFIRMED_PREFIX):].strip(" :.-")
        if resto.startswith(("no lo sabe", "no sabe", "no lo se")):
            return "no_lo_sabe", ""
        if resto.startswith("no aplica"):
            return "no_aplica", ""
        texto = texto[len(CONFIRMED_PREFIX):].strip(" :.-") or texto  # «Confirmado por el usuario: X» → X
    return "confirmado", texto


def contexto_cliente(problem_case: dict, con_costes: bool = False) -> str:
    """Los datos del cliente para los agentes, cada campo con su estado, y las reglas para usarlos.

    Todos los agentes reciben lo mismo, así que ninguno tiene que rellenar huecos por su cuenta.
    """
    grupos: dict[str, list[str]] = {"confirmado": [], "no_lo_sabe": [], "no_aplica": [], "sin_dato": []}
    notas: list[str] = []
    for campo, datos in (problem_case or {}).items():
        if campo in ("status", PLATAFORMA_OBJETIVO) or not isinstance(datos, dict):
            continue
        valor = datos.get("value")
        if campo == "evidencias":
            notas = list(dict.fromkeys(v.strip() for v in valor or [] if isinstance(v, str) and v.strip()))
            continue
        estado, limpio = _estado_del_valor(valor)
        grupos[estado].append(f"- {campo}: {limpio}" if estado == "confirmado" else f"- {campo}")

    titulos = {
        "confirmado": "Confirmados por el cliente:",
        "no_lo_sabe": "El cliente no lo sabe (si hace falta, propón una opción y márcala como «Se asume»):",
        "no_aplica": "El cliente dice que no aplica a su proyecto:",
        "sin_dato": "Sin dato (no se preguntó o no se respondió):",
    }
    partes = ["DATOS DEL CLIENTE (caso del discovery)"]
    plataforma = (problem_case or {}).get(PLATAFORMA_OBJETIVO)
    if isinstance(plataforma, dict) and plataforma.get("value"):  # fallo 67: manda sobre cloud_provider
        despliegue = plataforma.get("despliegue")  # fallo 70: lo decide Inetum, aunque modelo_de_despliegue diga otro
        if despliegue == "on_premises":
            partes.append("PLATAFORMA OBJETIVO: los servidores del cliente (on-premises), por decisión de Inetum. "
                          "Diseña la solución, sus servicios y sus costes para sus servidores, con software que pueda "
                          f"instalar él mismo; si algo solo existe en la nube, propón {plataforma['value']}.")
        else:
            partes.append(f"PLATAFORMA OBJETIVO: {plataforma['value']} ({plataforma.get('motivo', 'elegida por Inetum')}). "
                          "Diseña la solución, sus servicios y sus costes en esta nube, aunque cloud_provider diga otra.")
        for x in plataforma.get("existentes") or []:  # fallo 71: lo que ya tiene en otra nube
            servicios = f" ({', '.join(x['servicios'])})" if x.get("servicios") else ""
            if x.get("decision") == "migrar":
                partes.append(f"SE MIGRA A {plataforma['value']}: lo que el cliente tiene en {x['nube']}{servicios}. "
                              "Inclúyelo en la arquitectura, en el plan y en los costes (unos "
                              f"{x.get('coste_migracion_eur', 0):,} € de migración).".replace(",", "."))
            else:
                partes.append(f"SE MANTIENE: lo que el cliente ya tiene en {x['nube']}{servicios}. No lo migres ni lo "
                              f"rehagas: intégralo con {plataforma['value']} y presupuesta esa integración (conectores y "
                              "transferencia de datos entre nubes), aunque la solución nueva vaya en la plataforma objetivo.")
        if despliegue == "hibrido":
            partes.append("MODELO DE DESPLIEGUE: híbrido, por decisión de Inetum. Lo que no puede salir de sus servidores "
                          "(sistemas que no se pueden mover, datos que no pueden salir, equipos de planta) se queda en "
                          f"ellos y el resto va a {plataforma['value']}; diséñalo así aunque modelo_de_despliegue diga otro.")
    for estado, lineas in grupos.items():
        if lineas:
            partes.append(titulos[estado] + "\n" + "\n".join(lineas))
    if notas:
        partes.append("Notas del discovery (frases del cliente; si alguna choca con un dato confirmado, manda el "
                      "dato confirmado):\n" + "\n".join(f"- {n}" for n in notas))
    if len(partes) == 1:
        partes.append("(sin datos del cliente)")
    reglas = REGLAS_DATOS_CLIENTE if con_costes else REGLAS_DATOS_CLIENTE + REGLA_PRESUPUESTO
    return "\n\n".join(partes) + "\n\n" + reglas

# Fallo 44: los importes salen solo en la sección de costes, donde se suman en Python. Arquitectura, BI y el
# roadmap los ponían igualmente («~500 €/mes», «6.000 €/año») aunque REGLA_PRESUPUESTO se lo prohíbe
_CANTIDAD = r"\d(?:[\d.,]*\d)?(?:\s*(?:mil(?:lones)?(?:\s+de)?|[km])(?![a-záéíóúñ]))?"
_CANTIDAD_RE = re.compile(_CANTIDAD, re.IGNORECASE)
_IMPORTE = re.compile(
    rf"(?:~\s*)?(?:(?:€|\$|\beur\b|\busd\b)\s*{_CANTIDAD}"
    rf"|{_CANTIDAD}(?:(?:\s*[–-]\s*|\s+a\s+){_CANTIDAD})?\s*(?:€|\$|eur(?:os)?\b|usd\b|d[óo]lares\b))"
    rf"(?:\s*/\s*[a-záéíóúñ]+)*",
    re.IGNORECASE,
)
NOTA_IMPORTES = "*Los importes de la solución están en la sección de costes.*"


def _valor_cantidad(texto: str) -> float | None:
    """'8.000' -> 8000; '1,5 millones' -> 1500000; '20K' -> 20000; '9,40' -> 9.4."""
    m = re.match(r"(\d(?:[\d.,]*\d)?)\s*(mil(?:lones)?|[km])?", texto.strip(), re.IGNORECASE)
    if not m:
        return None
    numero = m.group(1)
    if "." in numero and "," in numero:  # el último separador es el decimal
        decimal = "," if numero.rfind(",") > numero.rfind(".") else "."
        numero = numero.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    else:
        sep = "." if "." in numero else ","
        grupos = numero.split(sep)
        numero = "".join(grupos) if all(len(g) == 3 for g in grupos[1:]) else numero.replace(sep, ".")
    try:
        valor = float(numero)
    except ValueError:
        return None
    return valor * {"k": 1e3, "mil": 1e3, "m": 1e6, "millones": 1e6}.get((m.group(2) or "").lower(), 1)


def _importes_del_cliente(problem_case: dict) -> set[float]:
    """Las cifras que dio el cliente, que sí se pueden citar: las del presupuesto y los importes de sus respuestas."""
    permitidos: set[float] = set()
    for campo, datos in (problem_case or {}).items():
        if not isinstance(datos, dict):
            continue
        valores = datos.get("value")
        for valor in valores if isinstance(valores, list) else [valores]:
            if not isinstance(valor, str):
                continue
            # En el presupuesto vale cualquier número («90.000»); en el resto, solo los que llevan moneda
            trozos = [valor] if campo == "restriccion_presupuestaria" else [m.group(0) for m in _IMPORTE.finditer(valor)]
            for trozo in trozos:
                permitidos.update(v for q in _CANTIDAD_RE.findall(trozo) if (v := _valor_cantidad(q)))
    return permitidos


def sin_importes(texto: str, problem_case: dict | None = None, seccion: str = "") -> str:
    """Quita los importes de una sección que no es la de costes (fallo 44), salvo los que dio el cliente.

    En una tabla, el importe se cambia por «—». En el texto, se quita el paréntesis que lo lleva o, si no
    está entre paréntesis, la frase. Los bloques de código no se tocan.
    """
    if not texto:
        return texto
    permitidos = _importes_del_cliente(problem_case or {})

    def ajeno(importe: str) -> bool:
        valores = [_valor_cantidad(q) for q in _CANTIDAD_RE.findall(importe)]
        return not all(v and any(abs(v - p) < 0.5 for p in permitidos) for v in valores)

    def con_importe(trozo: str) -> bool:
        return any(ajeno(m.group(0)) for m in _IMPORTE.finditer(trozo))

    lineas, tocadas, en_codigo = [], 0, False
    for linea in texto.splitlines():
        if linea.lstrip().startswith("```"):
            en_codigo = not en_codigo
            lineas.append(linea)
            continue
        if en_codigo or not con_importe(linea):
            lineas.append(linea)
            continue
        tocadas += 1
        linea = re.sub(r"\s*\([^()]*\)", lambda m: "" if con_importe(m.group(0)) else m.group(0), linea)
        if linea.lstrip().startswith("|"):  # fila de tabla: la celda se queda con «—»
            linea = _IMPORTE.sub(lambda m: "—" if ajeno(m.group(0)) else m.group(0), linea)
            lineas.append(re.sub(r"—(?:\s*[–-]\s*—)+", "—", linea))
            continue
        prefijo = re.match(r"\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*|#+\s+)?", linea).group(0)
        frases = re.split(r"(?:(?<=[.!?])|(?<=[.!?]\*\*)|(?<=[.!?][\"»”]))\s+", linea[len(prefijo):])
        cuerpo = " ".join(f for f in frases if not con_importe(f)).strip()
        if cuerpo:
            lineas.append(prefijo + cuerpo)
    if not tocadas:
        return texto
    logger.warning("Sección %s: se quitan los importes de %d línea(s); van en la de costes", seccion or "?", tocadas)
    return "\n".join(lineas).rstrip() + "\n\n" + NOTA_IMPORTES

#MODIFICADO: _normalize_update_value y _apply_field_update para que no se guarden valores de relleno
def _normalize_update_value(field_key: str, value: str | List[str], operation: str) -> str | List[str]:
    if field_key in TEXT_FIELDS:
        if operation != "replace":
            raise ValueError(f"El campo {field_key} es de texto y solo permite operation='replace'.")
        if isinstance(value, list):
            # Algunos modelos mandan un campo de texto como lista: se une en un texto en vez de perder el dato
            value = "; ".join(str(item).strip() for item in value if str(item).strip())
        elif not isinstance(value, str):
            raise ValueError(f"El campo {field_key} debe actualizarse con texto.")

        normalized = value.strip()
        if not normalized:
            raise ValueError(f"El campo {field_key} no puede actualizarse con texto vacío.")
        if es_valor_de_relleno(normalized):
            raise ValueError(f"El campo {field_key} no se guarda: es un valor de relleno, no un dato del cliente.")
        # Fallo 36: un «No lo sé» literal se leía como un dato confirmado
        cerrada = respuesta_cerrada(normalized)
        return VALORES_CERRADOS[cerrada] if cerrada else normalized

    if field_key in LIST_FIELDS:
        items = value if isinstance(value, list) else [value]
        normalized_items = [item.strip() for item in items if item.strip()]
        if not normalized_items:
            raise ValueError(f"El campo {field_key} no puede actualizarse con una lista vacía.")
        return normalized_items

    raise ValueError(f"El campo {field_key} no está contemplado en la configuración de la tool.")

# AÑADIDO: apply_field_update y
# SUSTITUIDA: update_problem_scope_tool

def _apply_field_update(problem_case: dict, upd: FieldUpdate) -> None:
    """Valida UN cambio y, si es válido, lo aplica. Si lanza ValueError, no ha tocado nada."""
    field_key = upd.field_key

    if field_key not in problem_case:
        raise ValueError(f"Campo no válido: {field_key}")
    if field_key == "status":
        raise ValueError("El campo 'status' no puede actualizarse dentro de 'updates'; usa 'update_status'.")

    field_data = problem_case[field_key]
    if not isinstance(field_data, dict) or "value" not in field_data:
        raise ValueError(f"El campo {field_key} no sigue la estructura esperada con 'value'.")

    normalized_value = _normalize_update_value(field_key, upd.value, upd.operation)
    current_value = field_data["value"]

    if upd.operation == "replace":
        field_data["value"] = normalized_value
        return

    if not isinstance(current_value, list):
        raise ValueError(f"El campo {field_key} no es una lista.")

    new_items = normalized_value if isinstance(normalized_value, list) else [normalized_value]
    if upd.operation == "append":
        field_data["value"] = current_value + new_items
    else:  # merge: añade solo lo que no estaba
        merged = current_value[:]
        for item in new_items:
            if item not in merged:
                merged.append(item)
        field_data["value"] = merged


def update_problem_scope_tool(request: ProblemScopeUpdateRequest) -> dict:
    with open(PROBLEM_CASE_PATH, "r", encoding="utf-8") as f:
        problem_case = json.load(f)

    #DONE: cambiar la funcion y las listas por una base de datos???
    updated_fields: list[str] = []
    rejected: list[dict[str, str]] = []

    # Cada cambio se valida por separado: uno inválido se descarta y los demás se guardan.
    for upd in request.updates:
        try:
            _apply_field_update(problem_case, upd)
            updated_fields.append(upd.field_key)
        except ValueError as exc:
            rejected.append({"field_key": upd.field_key, "reason": str(exc)})
            # Solo el campo y el motivo, nunca el valor: puede llevar datos del cliente.
            logger.warning("Cambio descartado en '%s': %s", upd.field_key, exc)

    if request.update_status is not None:
        if request.update_status not in VALID_STATUSES:
            raise ValueError(f"Estado no válido: {request.update_status}")
        problem_case["status"] = request.update_status

    with open(PROBLEM_CASE_PATH, "w", encoding="utf-8") as f:
        json.dump(problem_case, f, ensure_ascii=False, indent=2)

    return {
        "message": "Problem scope actualizado" + (f" ({len(rejected)} cambio(s) descartado(s))" if rejected else ""),
        "updated_fields": updated_fields,
        "rejected": rejected,
        "status": problem_case["status"],
    }



def _recent_chat_transcript(chat_history: list[dict[str, str]] | None, max_messages: int = 6) -> str:
    if not chat_history:
        return ""
    recent_messages = chat_history[-max_messages:]
    lines: list[str] = []
    for message in recent_messages:
        role = message.get("role", "user")
        content = (message.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def load_agents_config() -> dict:
    with open(AGENTS_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_html(markup: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(markup, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(markup, "html.parser")


# Conversor Markdown → HTML: CommonMark + tablas. Con html=False, el HTML que escriba el LLM
# se muestra como texto en vez de ejecutarse; breaks=True convierte cada salto de línea en <br>.
_MD = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])
_BR = re.compile(r"</?br\s*/?>", re.IGNORECASE)  # <br>, <br/>, <br /> y también </br>, que el navegador lee como <br>

def _br_como_salto(inline: Token) -> None:
    """Los agentes escriben <br> dentro de las celdas para partir líneas. Con el HTML desactivado
    saldría como texto, así que se cambia por un salto de línea real. Solo en texto normal:
    un `<br>` entre comillas de código se queda como código, y el resto del HTML sigue escapado."""
    hijos = []
    for hijo in inline.children or []:
        if hijo.type != "text" or not _BR.search(hijo.content):
            hijos.append(hijo)
            continue
        for i, trozo in enumerate(_BR.split(hijo.content)):
            if i:
                hijos.append(Token("hardbreak", "br", 0))
            if trozo:
                texto = Token("text", "", 0)
                texto.content = trozo
                hijos.append(texto)
    inline.children = hijos

# Fallo 65: los modelos (Gemini sobre todo) adornan títulos y celdas con emojis («## 🧭 Panorama general», «📊 Bizkaia -
# Operaciones»), que no pegan en un informe para un cliente. ✅ y ❌ pasan a ser ✓ y ✗, para que una tabla de sí y no
# conserve su sentido; los demás pictogramas y símbolos de emoji (⚠️ ⭐ 🚀…) se quitan, con el espacio que los sigue, y
# un número de tecla («1️⃣») se queda en «1.». Las flechas (→ ▼), las estrellas (★ ☆) y ✓ ✔ ✗ ✘ no se tocan.
_MARCAS = str.maketrans({"\u2705": "\u2713", "\u2611": "\u2713",                    # ✅ ☑ → ✓
                         "\u274C": "\u2717", "\u274E": "\u2717", "\u2716": "\u2717"})  # ❌ ❎ ✖ → ✗
_TECLA = re.compile("([0-9])\uFE0F?\u20E3")
_EMOJI = re.compile(
    "(?:[\U0001F000-\U0001FAFF\u2600-\u2604\u2607-\u26FF\u2B05-\u2B07\u2B1B\u2B1C\u2B50\u2B55\u231A\u231B"
    "\u23E9-\u23FA\u2702\u2708-\u270D\u270F\u2712\u271D\u2721\u2728\u2733\u2734\u2744\u2747\u2753-\u2755"
    "\u2757\u2763\u2764\u2795-\u2797\u27A1\u27B0\u27BF\u200D\u20E3])+[ \t]*")
_VARIANTE = re.compile("[\uFE0E\uFE0F]")  # el selector de «dibújalo como emoji»: fuera, y el espacio se queda


def sin_emojis(texto: str) -> str:
    """El texto de un LLM sin emojis (fallo 65). Lo usan md_to_html(), para todo lo que va al informe, y el resumen
    de la propuesta, que llega como contexto a las 7 fases del roadmap."""
    texto = _VARIANTE.sub("", _TECLA.sub(r"\1.", (texto or "").translate(_MARCAS)))
    return _EMOJI.sub("", texto)

# Fallo 66: la fuente del informe (Space Mono) va dentro del propio HTML, desde assets/fonts/, en vez de pedirla a
# Google Fonts cada vez que se abre: el informe se ve igual sin conexión y no avisa a Google de quién lo abre.
FUENTES = Path("assets/fonts")
_GOOGLE_FONTS = re.compile(r'<link[^>]*fonts\.(?:googleapis|gstatic)\.com[^>]*>\s*')


def fuentes_locales(html: str) -> str:
    """Cambia los <link> a Google Fonts por un @font-face con la fuente en base64. Si falta algún archivo de la
    fuente, el HTML se queda como estaba (con Google Fonts)."""
    archivos = {peso: FUENTES / f"space-mono-{peso}.woff2" for peso in (400, 700)}
    if not _GOOGLE_FONTS.search(html or "") or not all(a.exists() for a in archivos.values()):
        return html
    caras = "".join(
        "@font-face{font-family:'Space Mono';font-style:normal;font-display:swap;"
        f"font-weight:{peso};src:url(data:font/woff2;base64,{base64.b64encode(a.read_bytes()).decode('ascii')})"
        " format('woff2');}" for peso, a in archivos.items())
    html = _GOOGLE_FONTS.sub("", html)
    return html.replace("</head>", f"<style>{caras}</style>\n</head>", 1)

def md_to_html(text: str) -> str:
    text = sin_emojis(text)
    if not text or not text.strip():
        return "<p>No se ha generado contenido.</p>"
    tokens = _MD.parse(text)
    # Los títulos del LLM no deben competir con los del informe: # y ## → h3, el resto → h4 (como antes)
    for token in tokens:
        if token.type in ("heading_open", "heading_close"):
            token.tag = "h3" if token.tag in ("h1", "h2") else "h4"
        elif token.type == "inline":
            _br_como_salto(token)
    return _MD.renderer.render(tokens, _MD.options, {})