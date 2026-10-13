import json
import os
import re
from typing import Any

import litellm
from dotenv import load_dotenv

import logging
from pydantic import ValidationError

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
        payload = json.loads(_extract_json_object(raw_output))
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
    if isinstance(assistant_reply, str):
        parsed["assistant_reply"] = assistant_reply.strip()

    update_status = payload.get("update_status")
    if update_status in {"pending fields", "completed"}:
        parsed["update_status"] = update_status

    return parsed


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
    model_name = os.getenv("BIG_MODEL", "").strip()
    if not model_name:
        return {"updates": [], "assistant_reply": "", "update_status": None}

    problem_case = load_problem_case()
    pending = _get_pending_fields(problem_case)
    print(f"Campos pendientes detectados: {pending}")
    transcript = _recent_chat_transcript(chat_history)
    current_json = json.dumps(problem_case, ensure_ascii=False, indent=2)

    system_prompt = (
        "Eres un agente de discovery para proyectos de Data & AI de Inetum. "
        "Tu objetivo es extraer información del cliente en cada mensaje y actualizar el caso de problema campo a campo.\n\n"
        "REGLAS ESTRICTAS:\n"
        "1. En CADA turno, extrae y actualiza TODOS los campos que puedas deducir del mensaje actual o del historial.\n"
        "2. No acumules información: si el usuario menciona algo relevante, escríbelo YA en 'updates'.\n"
        "3. Haz UNA sola pregunta clara y concreta sobre el campo pendiente más relevante.\n"
        "4. Devuelve EXCLUSIVAMENTE un JSON válido sin markdown ni texto extra, con este esquema:\n"
        '{"updates": [{"field_key": "...", "value": "...", "operation": "replace"}], '
        '"assistant_reply": "...", "update_status": "pending fields" | "completed" | null}\n\n'
        f"Campos válidos (únicos permitidos en field_key): {sorted(allowed_fields)}\n\n"
        "Para el campo 'evidencias' (lista), usa operation 'merge' y value como array de strings.\n"
        "Regla de cierre: si tras tus updates no quedan campos pendientes, usa update_status='completed' "
        "y assistant_reply sin preguntas de seguimiento.\n"
        "Sé descriptivo en los values: evita palabras genéricas o vacías.\n"
        "Campos sin dato: si el usuario no ha dado un dato, NO incluyas ese campo en 'updates' "
        "(nada de 'no mencionado', 'desconocido', 'N/A' ni similares); se queda pendiente y se pregunta después. "
        "Solo si el usuario dice explícitamente que no aplica o que no lo sabe, escribe "
        "'Confirmado por el usuario: no aplica' o 'Confirmado por el usuario: no lo sabe'."
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
        num_retries=3,
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
        # Los cambios inválidos ya se descartan uno a uno; aquí solo llega algo inesperado.
        logger.error("No se pudo aplicar el plan del discovery: %s", exc)

    if plan.get("updates") or plan.get("update_status") is not None:
        try:
            _apply_plan(plan)
        except Exception:
            # Evita romper la conversación por un update inválido.
            pass

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
