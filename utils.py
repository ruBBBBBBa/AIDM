import json
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
PROBLEM_CASE_PATH = Path("data_structures/problem_case.json")
AGENTS_FILE = Path("data_structures/agents.yaml")
logger = logging.getLogger("aidm")

#Prueba para fallback por si uno de los modelos no llega a terminar, se puede usar otro modelo de reserva con mas tokens
MODEL_RULES = {
    "groq/openai/gpt-oss-20b":  {"max_tokens": 4096, "fallback": "cloudflare/@cf/openai/gpt-oss-20b"},
    "groq/openai/gpt-oss-120b": {"max_tokens": 6000, "fallback": "cloudflare/@cf/openai/gpt-oss-120b"},
}
FALLBACK_MAX_TOKENS = 6000

def load_problem_case() -> dict[str, Any]:
    with open(PROBLEM_CASE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

    
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
def caso_compacto(problem_case: dict) -> str:
    """Los datos del cliente en pocas líneas: solo los campos con un valor real, sin descripciones."""
    lineas = []
    for campo, datos in problem_case.items():
        if campo in ("status", "evidencias") or not isinstance(datos, dict):
            continue
        valor = datos.get("value")
        if isinstance(valor, str) and not es_valor_de_relleno(valor):
            lineas.append(f"- {campo}: {valor.strip()}")
    return "\n".join(lineas) or "(sin datos del cliente)"

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
        return normalized

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

    #TODO: cambiar la funcion y las listas por una base de datos???
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

def md_to_html(text: str) -> str:
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