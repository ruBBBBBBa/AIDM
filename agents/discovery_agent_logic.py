import json
import os
import re
from typing import Any

import litellm
from dotenv import load_dotenv

import logging
from pydantic import ValidationError
import gateway

from utils import (
    FieldUpdate,
    LIST_FIELDS,
    ProblemScopeUpdateRequest,
    TEXT_FIELDS,
    update_problem_scope_tool,
    load_problem_case,
    _recent_chat_transcript,
    es_valor_de_relleno,
)

load_dotenv(".env")
logger = logging.getLogger("aidm")



def _get_pending_fields(problem_case: dict[str, Any]) -> list[str]:
    pending: list[str] = []
    for field_key, field_data in problem_case.items():
        if field_key == "status":
            continue
        if not isinstance(field_data, dict) or "value" not in field_data:
            continue

        value = field_data["value"]
        # Vacío o relleno tipo "no mencionado": el campo sigue pendiente.
        if isinstance(value, str) and es_valor_de_relleno(value):
            pending.append(field_key)
        elif isinstance(value, list) and len(value) == 0:
            pending.append(field_key)
        elif value is None:
            pending.append(field_key)

    dynamic_missing: list[str] = []
    for field_key in pending:
        field_data = problem_case.get(field_key, {})
        description = (field_data.get("description") or "").strip() if isinstance(field_data, dict) else ""
        if description:
            dynamic_missing.append(f"{field_key}: {description}")
        else:
            dynamic_missing.append(field_key)

    return dynamic_missing  


def _extract_json_object(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return "{}"
    return cleaned[start : end + 1]


def _parse_plan(raw_output: str, allowed_fields: set[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {"updates": [], "assistant_reply": "", "update_status": None}
    try:
        payload = json.loads(_extract_json_object(raw_output), strict =False)
    except Exception:
        return parsed

    updates = payload.get("updates")
    if isinstance(updates, list):
        safe_updates: list[dict[str, Any]] = []
        for upd in updates:
            if not isinstance(upd, dict):
                continue
            field_key = upd.get("field_key")
            operation = upd.get("operation", "replace")
            value = upd.get("value")
            if field_key not in allowed_fields:
                continue
            if operation not in {"replace", "append", "merge"}:
                continue
            if value is None:
                continue
            safe_updates.append({"field_key": field_key, "value": value, "operation": operation})
        parsed["updates"] = safe_updates

    assistant_reply = payload.get("assistant_reply")
    if isinstance(assistant_reply, dict):
        # Algunos modelos devuelven {"text": "..."} en vez del texto directamente
        assistant_reply = assistant_reply.get("text") or ""
    if isinstance(assistant_reply, str):
        parsed["assistant_reply"] = assistant_reply.strip()

    update_status = payload.get("update_status")
    if update_status in {"pending fields", "completed"}:
        parsed["update_status"] = update_status

    return parsed

#RED DE SEGURIDAD PARA QUE EL DISCOVERY NO SE INVENTE DATOS QUE NO HA DADO EL USUARIO O INFLE LOS DADOS
_CIFRA = re.compile(r"\d+(?:[.,]\d+)*")
_ENUMERACION = re.compile(r"\(\d{1,2}\)|(?:^|(?<=\s))\d{1,2}[.)](?=\s)")  # (1)  1.  2)
_TODO = re.compile(r"\b100\s?%")  # «100 %» es otra forma de decir «todo»

def _cifras(texto: str) -> set[str]:
    """Números que aparecen en un texto, sin separadores: '90.000' y '90000' cuentan igual.
    No cuentan los de una enumeración ni el «100 %»."""
    limpio = _TODO.sub("", _ENUMERACION.sub("", texto or ""))
    return {c.replace(".", "").replace(",", "") for c in _CIFRA.findall(limpio)}


def _sin_cifras_inventadas(updates: list[dict[str, Any]], textos_usuario: str) -> list[dict[str, Any]]:
    """Quita los valores con cifras que el usuario no ha dicho: suelen ser estimaciones del modelo."""
    dichas = _cifras(textos_usuario)
    limpios: list[dict[str, Any]] = []
    for upd in updates:
        valor = upd.get("value")
        if isinstance(valor, list):  # evidencias: se quitan solo las frases con cifras inventadas
            buenas = [v for v in valor if not isinstance(v, str) or _cifras(v) <= dichas]
            if len(buenas) < len(valor):
                logger.warning("Evidencias descartadas en '%s': %d con cifras que el usuario no ha dicho",
                               upd.get("field_key"), len(valor) - len(buenas))
            if buenas:
                limpios.append({**upd, "value": buenas})
            continue
        nuevas = _cifras(valor) - dichas if isinstance(valor, str) else set()
        if nuevas:
            logger.warning("Cambio descartado en '%s': lleva cifras que el usuario no ha dicho (%s)",
                           upd.get("field_key"), ", ".join(sorted(nuevas)))
            continue
        limpios.append(upd)
    return limpios

def _apply_plan(plan: dict[str, Any]) -> dict:
# Cada cambio se construye por separado: si uno no tiene el formato esperado, se descarta solo ese.
    updates: list[FieldUpdate] = []
    for upd in plan.get("updates", []):
        try:
            updates.append(FieldUpdate(**upd))
        except ValidationError as exc:
            logger.warning("Cambio descartado en '%s': formato no válido (%s)",
                           upd.get("field_key"), exc.errors()[0]["msg"])

    request = ProblemScopeUpdateRequest(
        updates=updates,
        update_status=plan.get("update_status"),
    )
    return update_problem_scope_tool(request)

# Fallback
def _sync_status_with_content() -> None:
    problem_case = load_problem_case()
    pending = _get_pending_fields(problem_case)
    expected_status = "completed" if not pending else "pending fields"
    if problem_case.get("status") == expected_status:
        return

    request = ProblemScopeUpdateRequest(updates=[], update_status=expected_status)
    update_problem_scope_tool(request)


def _build_assistant_reply(
    problem_case_before: dict[str, Any],
    problem_case_after: dict[str, Any],
    llm_reply: str = "",
) -> str:
    pending = _get_pending_fields(problem_case_after)
    completed_now = problem_case_after.get("status") == "completed" and not pending

    # Cierre determinista: cuando el caso está completo no dependemos del texto del LLM.
    if completed_now:
        return "Perfecto, ya tengo todo lo que necesitaba. El discovery ha quedado completado. Ahora voy a analizar toda la información para proponerte una estructura de solución."

    if llm_reply.strip():
        return llm_reply.strip()

    # Fallback basado en contenido: si el LLM ha fallado dando una reply, indicamos qué campos han cambiado y qué falta.
    # Este fallback no sirve de mucho, habria que quitarlo
    changed_fields: list[str] = []
    for field_key, field_data in problem_case_after.items():
        if field_key == "status" or not isinstance(field_data, dict):
            continue

        before_value = problem_case_before.get(field_key, {}).get("value")
        after_value = field_data.get("value")
        if before_value != after_value:
            changed_fields.append(field_key)

    if not pending:
        if changed_fields:
            return (
                "Perfecto. He actualizado el contexto del caso y ya tenemos todos los campos "
                "necesarios para pasar a la siguiente fase de análisis."
            )
        return "El contexto ya estaba completo. Si quieres, pasamos al diseño de solución."

    if changed_fields:
        updated_list = ", ".join(changed_fields[:4])
        return (
            f"He actualizado la información en: {updated_list}. "
            "Para terminar el alcance, necesito que me confirmes algunos datos pendientes."
        )

    first_pending = pending[0]
    field_meta = problem_case_after.get(first_pending, {})
    next_question = field_meta.get("description") or f"¿Puedes aportar más detalle sobre {first_pending}?"
    return f"Gracias, ya tengo ese contexto. Para avanzar, necesito este dato: {next_question}"


def _propose_plan_with_agent(user_prompt: str, chat_history: list[dict[str, str]] | None) -> dict[str, Any]:
    allowed_fields = set(TEXT_FIELDS) | set(LIST_FIELDS)
    model_name = gateway.modelo("discovery")
    if not model_name:
        return {"updates": [], "assistant_reply": "", "update_status": None}

    problem_case = load_problem_case()
    pending = _get_pending_fields(problem_case)
    print(f"Campos pendientes detectados: {pending}")
    transcript = _recent_chat_transcript(chat_history)
    current_json = json.dumps(problem_case, ensure_ascii=False, indent=2)

    system_prompt = (
        "Eres un agente de discovery para proyectos de Data & AI de Inetum. "
        "Tu objetivo es recoger los datos que da el cliente y guardarlos en el caso, campo a campo.\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. Guarda SOLO lo que el usuario ha dicho explícitamente, en este mensaje o en el historial. "
        "No deduzcas, no estimes ni completes nada por tu cuenta: si un dato no se ha dicho, el campo se queda pendiente. "
        "Nunca escribas cifras (volúmenes, porcentajes, importes, plazos) que el usuario no haya dado.\n"
        "2. Si el usuario da varios datos a la vez, guárdalos todos en este turno, cada uno en su campo, "
        "aunque no sea el campo por el que preguntaste.\n"
        "3. Valores breves y literales: una frase como máximo, con las palabras y cifras del usuario. "
        "Sin markdown (ni ** ni listas) y sin análisis ni recomendaciones: la solución la diseñan otros agentes después.\n"
        "4. En 'evidencias' (lista, operation 'merge') anota frases cortas de lo que ha dicho el usuario, nunca conclusiones tuyas, "
        "y sin cambiarles el sentido. Anota también ahí los detalles que no encajan en ningún campo.\n"
        "5. Campos sin dato: no los incluyas en 'updates' (nada de 'no mencionado', 'desconocido', 'N/A' ni similares). "
        "Una respuesta negativa SÍ es un dato y se guarda tal cual: si dice «no tenemos equipo de datos», escribe "
        "'No tiene equipo de datos'. El prefijo 'Confirmado por el usuario' es solo para dos casos: si dice que el tema "
        "no aplica a su proyecto ('Confirmado por el usuario: no aplica') o que no lo sabe "
        "('Confirmado por el usuario: no lo sabe'). No lo uses en ningún otro caso.\n"
        "6. Haz UNA sola pregunta, clara y breve, sobre el campo pendiente más relevante.\n"
        "7. Regla de cierre: si tras tus updates no quedan campos pendientes, usa update_status='completed' "
        "y un assistant_reply sin preguntas.\n"
        "8. Devuelve EXCLUSIVAMENTE un JSON válido, sin markdown ni texto extra, con este esquema "
        "(assistant_reply es un texto, no un objeto; un salto de línea dentro de un texto se escribe \\n):\n"
        '{"updates": [{"field_key": "...", "value": "...", "operation": "replace"}], '
        '"assistant_reply": "...", "update_status": "pending fields" | "completed" | null}\n\n'
        "EJEMPLO. Pregunta: «¿Tenéis equipo de datos?». Respuesta: «No, IT son 5 personas y una sabe Python. "
        "Queremos que lo montéis vosotros y luego nos forméis.» Updates correctos: "
        "tiene_equipo_datos='No tiene equipo de datos'; tamano_equipo_tecnico='5 personas de IT, una sabe Python'; "
        "tipo_de_servicio='Que Inetum lo implemente y después forme al equipo del cliente'. Incorrecto: "
        "tiene_equipo_datos='Confirmado por el usuario: no aplica', o dejar el tipo de servicio solo en evidencias.\n\n"
        f"Campos válidos (únicos permitidos en field_key): {sorted(allowed_fields)}"
    )

    user_message = (
        f"Estado actual del JSON:\n{current_json}\n\n"
        f"Campos pendientes: {pending}\n\n"
        f"Historial reciente:\n{transcript}\n\n"
        f"Mensaje actual del usuario: {user_prompt}\n\n"
        "Devuelve el JSON con las actualizaciones y la siguiente pregunta al usuario."
    )

    print(f"Ejecutando discovery con modelo {model_name}...")
    response = litellm.completion(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.1,
        **gateway.parametros(model_name, num_retries=3),  # el tope y la reserva del modelo, si los tiene
    )

    raw_output = response.choices[0].message.content or ""
    print(f"Raw output del agente de discovery:\n{raw_output}\n")
    parsed_output = _parse_plan(raw_output, allowed_fields)
    print(f"Parsed output del agente de discovery:\n{parsed_output}\n")

    return parsed_output


def run_agent(user_prompt, chat_history: list[dict[str, str]] | None = None):
    problem_case_before = load_problem_case()
    
    if problem_case_before.get("status") == "completed":
        return _build_assistant_reply(problem_case_before, problem_case_before, "")

    plan: dict[str, Any] = {"updates": [], "assistant_reply": "", "update_status": None}
    try:
        plan = _propose_plan_with_agent(user_prompt, chat_history)
    except Exception as exc:
       # Falló la llamada al LLM (o leer su respuesta): se sigue con el plan vacío, pero queda registrado.
        logger.error("Falló la llamada al LLM del discovery: %s", exc)

     # Red de seguridad del prompt: un valor con cifras que el usuario no ha dicho es una estimación del modelo.
    textos_usuario = " ".join(
        m.get("content", "") for m in (chat_history or []) if m.get("role") == "user"
    ) + " " + str(user_prompt)
    plan["updates"] = _sin_cifras_inventadas(plan.get("updates", []), textos_usuario)


    if plan.get("updates") or plan.get("update_status") is not None:
        try:
            _apply_plan(plan)
        except Exception as exc:
            # Los cambios inválidos ya se descartan uno a uno; aquí solo llega un fallo inesperado
            # (p. ej. no se puede leer o escribir problem_case.json). Se registra en vez de ocultarlo.
            logger.error("No se pudo aplicar el plan del discovery: %s", exc)

    # Actualiza el status del caso según los campos pendientes, para mantener consistencia aunque el LLM no lo haya hecho bien.
    _sync_status_with_content()
    problem_case_after = load_problem_case()
    
    just_completed = (
        problem_case_before.get("status") != "completed"
        and problem_case_after.get("status") == "completed"
    )
    llm_reply = "" if just_completed else plan.get("assistant_reply", "")

    # Si el LLM da el caso por cerrado pero quedan campos sin dato real, su despedida no vale: se pregunta por lo que falta.
    if plan.get("update_status") == "completed" and problem_case_after.get("status") != "completed":
        pending = _get_pending_fields(problem_case_after)
        if pending:
            primero = pending[0].split(": ", 1)[-1]
            llm_reply = f"Aún me faltan {len(pending)} datos para cerrar el caso. Empecemos por este: {primero}"
    
    return _build_assistant_reply(problem_case_before, problem_case_after, llm_reply)
