"""Gateway de modelos: cada tarea de AIDM pide aquí su modelo.

Cada tarea tiene un perfil según el trabajo que hace (extracción, cálculo, redacción…), y cada perfil es una lista
de modelos en orden: se usa el primero y los demás son sus reservas, de otros proveedores, por si falla, se queda
sin cupo gratuito o tarda más de lo que permite el yaml. Todo está en data_structures/modelos.yaml; en el .env solo
van las claves de los proveedores.

Para probar otro modelo en una tarea sin tocar el yaml, ponlo en el .env como MODELO_<TAREA>: va primero y las
reservas del perfil siguen detrás.
    MODELO_COSTES=gemini/gemini-3.6-flash

Cada llamada deja una línea «[gateway]» en el log, con la tarea, el modelo que ha respondido (o la reserva), los
tokens y los segundos, y avisa si la respuesta se ha cortado por el límite de tokens. Al acabar el informe, la
línea «[gateway] Consumo de la ejecución» suma los tokens de cada modelo, lo que costarían pagando y lo que se ha
gastado de los cupos gratuitos (precios y cupos, en el yaml). Con GRABAR_LLAMADAS=1 en el
.env, además se guardan los prompts y las respuestas en pruebas_modelos/grabaciones/, para compararlos con
pruebas_modelos/comparar.py. Llevan datos del cliente: esa carpeta no se sube.
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import litellm
import yaml
from crewai import LLM
from dotenv import load_dotenv

load_dotenv(".env")

MODELOS_PATH = Path("data_structures/modelos.yaml")
GRABACIONES = Path("pruebas_modelos/grabaciones")
TIMEOUT = 300  # segundos por llamada si el yaml no dice otra cosa; el 3 nov, el equipo esperó 2 × 600 s a Gemini
_VARIABLE = re.compile(r"\$\{(\w+)\}")


class ModeloDeTarea(str):
    """El nombre del modelo, que se usa como un texto normal, pero que recuerda su tarea y sus reservas: así
    build_llm() y parametros() les ponen las reservas del perfil y la etiqueta con que se registra cada llamada."""

    def __new__(cls, nombre: str, tarea: str = "", reservas: tuple[str, ...] = ()):
        modelo = super().__new__(cls, nombre)
        modelo.tarea = tarea
        modelo.reservas = tuple(reservas)
        return modelo


def _config() -> dict:
    """Se lee en cada llamada: un cambio en el yaml vale para la siguiente ejecución, sin reiniciar Streamlit."""
    with open(MODELOS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolver(valor) -> str:
    """«${VARIABLE}» → su valor en el .env; un nombre de modelo se queda igual."""
    return _VARIABLE.sub(lambda m: os.getenv(m.group(1), ""), str(valor or "")).strip()


def _escribir(linea: str) -> None:
    """Una línea entera de una vez y sin esperar: con el log en un archivo, print() las dejaba en el búfer y salían
    desordenadas respecto a los avisos de LiteLLM."""
    sys.stdout.write(linea + "\n")
    sys.stdout.flush()


def modelos_de(tarea: str, config: dict | None = None) -> tuple[str, list[str]]:
    """El perfil de la tarea y su lista de modelos, con MODELO_<TAREA> delante si está en el .env. Una tarea que no
    está en el yaml usa el perfil de «por_defecto»; un «perfil» con «/» se toma como un modelo suelto."""
    config = config if config is not None else _config()
    perfiles, tareas = config.get("perfiles") or {}, config.get("tareas") or {}
    if tarea not in tareas:
        _escribir(f"[gateway] Ojo: la tarea «{tarea}» no está en {MODELOS_PATH}; se usa el perfil por defecto")
    perfil = str(tareas.get(tarea) or config.get("por_defecto") or "")
    lista = perfiles.get(perfil) or ([perfil] if "/" in perfil else [])
    lista = [m for m in (_resolver(x) for x in lista) if m]
    propio = os.getenv(f"MODELO_{tarea.upper()}", "").strip()
    if propio:
        lista = [propio] + [m for m in lista if m != propio]
    return perfil, lista


def modelo(tarea: str) -> ModeloDeTarea:
    """El modelo de una tarea (el primero de su perfil), con sus reservas. Lo escribe en el log."""
    perfil, lista = modelos_de(tarea)
    if not lista:
        _escribir(f"[gateway] {tarea}: sin modelo (perfil «{perfil}»); revisa {MODELOS_PATH}")
        return ModeloDeTarea("", tarea)
    origen = f"MODELO_{tarea.upper()} del .env" if os.getenv(f"MODELO_{tarea.upper()}", "").strip() else f"perfil {perfil}"
    reservas = f"; reservas: {', '.join(lista[1:])}" if lista[1:] else ""
    _escribir(f"[gateway] {tarea}: {lista[0]} ({origen}{reservas})")
    return ModeloDeTarea(lista[0], tarea, tuple(lista[1:]))


def _reglas(model_name: str, config: dict | None = None) -> dict:
    """Reintentos, tope de salida y tope de tiempo de un modelo, según el yaml: las de su proveedor («proveedores:»)
    y, encima, las suyas («modelos:»). Cloudflare sin regla lleva 4096 de tope de salida, porque si no corta a 256."""
    config = config if config is not None else _config()
    proveedor = str(model_name).partition("/")[0]
    reglas = {**((config.get("proveedores") or {}).get(proveedor) or {}),
              **((config.get("modelos") or {}).get(str(model_name)) or {})}
    params = {"num_retries": reglas.get("num_retries", 2),
              "timeout": reglas.get("timeout") or config.get("timeout_por_defecto") or TIMEOUT}
    max_tokens = reglas.get("max_tokens") or (4096 if str(model_name).startswith("cloudflare/") else None)
    if max_tokens:
        params["max_tokens"] = max_tokens
    return params


def parametros(model_name: str, **extra) -> dict:
    """Para el LLM de CrewAI o litellm.completion: las reglas del modelo y, si viene de modelo(), las reservas de su
    perfil (cada una con sus reglas) y la tarea, que se usa al registrar la llamada. `extra` manda sobre las reglas."""
    config = _config()
    params = _reglas(model_name, config)
    reservas = getattr(model_name, "reservas", ())
    if reservas:
        params["fallbacks"] = [{"model": r, **_reglas(r, config)} for r in reservas]
    tarea = getattr(model_name, "tarea", "")
    if tarea:
        params["metadata"] = {"aidm_tarea": tarea}
    return {**params, **extra}


def build_llm(model_name: str):
    """LLM para CrewAI con las reglas del modelo, sus reservas y su tarea."""
    params = parametros(model_name)
    if str(model_name).startswith("gemini/"):
        # Gemini también por LiteLLM: el cliente nativo de CrewAI no admite reservas y pide instalar google-genai
        params["provider"] = "litellm"
    return LLM(model=str(model_name), **params)


def completar(model: str, messages: list[dict], **kwargs):
    """litellm.completion con los parámetros del gateway (discovery, Terraform). Queda registrada como las demás."""
    return litellm.completion(model=str(model), messages=messages, **parametros(model, **kwargs))


# --- Registro de cada llamada: una línea en el log y, con GRABAR_LLAMADAS=1, el prompt y la respuesta ---

_bloqueo = threading.Lock()
_archivo: Path | None = None  # uno por arranque del programa, al grabar la primera llamada
_consumo: dict[str, dict[str, int]] = {}  # llamadas y tokens de cada modelo que ha respondido, desde el último resumen

def _miles(n) -> str:
    return f"{int(n or 0):,}".replace(",", ".")


def _registrar(tarea: str, modelo_usado: str, mensajes, respuesta: str, uso: dict | None, segundos: float | None,
               error: str = "", cortada: bool = False) -> None:
    """`cortada`: la respuesta ha llegado al límite de tokens (el tope de salida o la ventana del modelo) y le falta
    el final. Un JSON cortado lo detecta su lector, pero un texto cortado llegaría al informe sin aviso."""
    uso = uso or {}
    entrada, salida = uso.get("prompt_tokens"), uso.get("completion_tokens")
    tiempo = f" · {segundos:.1f} s".replace(".", ",") if segundos is not None else ""
    if error:
        _escribir(f"[gateway] {tarea or '¿tarea?'} · {modelo_usado} falló{tiempo}: {error[:300]}")
    else:
        tokens = f" · {_miles(entrada)} tokens de entrada y {_miles(salida)} de salida" if entrada is not None else ""
        corte = " · cortada: ha llegado al límite de tokens" if cortada else ""
        _escribir(f"[gateway] {tarea or '¿tarea?'} · {modelo_usado}{tokens}{tiempo}{corte}")
        _sumar(modelo_usado, entrada, salida)
    if os.getenv("GRABAR_LLAMADAS", "").strip().lower() not in ("1", "si", "sí", "true"):
        return
    global _archivo
    with _bloqueo:
        if _archivo is None:
            GRABACIONES.mkdir(parents=True, exist_ok=True)
            _archivo = GRABACIONES / f"{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        if isinstance(mensajes, str):
            mensajes = [{"role": "user", "content": mensajes}]
        registro = {"fecha": datetime.now().isoformat(timespec="seconds"), "tarea": tarea, "modelo": modelo_usado,
                    "mensajes": mensajes or [], "respuesta": respuesta or "", "tokens_entrada": entrada,
                    "tokens_salida": salida, "segundos": segundos, "error": error or None, "cortada": cortada}
        with open(_archivo, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")



def _sumar(modelo_usado: str, entrada, salida) -> None:
    """Suma la llamada al modelo que de verdad ha respondido: si fue una reserva, el de «→ reserva X»."""
    modelo = modelo_usado.rpartition(" → reserva ")[2]
    with _bloqueo:
        cuenta = _consumo.setdefault(modelo, {"llamadas": 0, "entrada": 0, "salida": 0})
        cuenta["llamadas"] += 1
        cuenta["entrada"] += int(entrada or 0)
        cuenta["salida"] += int(salida or 0)


def _llamadas(n: int) -> str:
    return "1 llamada" if n == 1 else f"{_miles(n)} llamadas"


def _dolares(cantidad: float) -> str:
    return f"{cantidad:.4f} $".replace(".", ",")


def _por_ciento(parte: float, total: float) -> str:
    return f"{100 * parte / total:.1f} %".replace(".", ",")


def _coste(cuenta: dict, precio: dict) -> float:
    """Con el precio en dólares por millón de tokens, de entrada y de salida."""
    return (cuenta["entrada"] * precio.get("entrada", 0) + cuenta["salida"] * precio.get("salida", 0)) / 1_000_000


def resumen_consumo(reiniciar: bool = True) -> None:
    """Escribe en el log lo que ha gastado la ejecución: las llamadas y los tokens de cada modelo, lo que costarían
    pagando por tokens («precios:» del yaml), lo mismo con cada modelo de pago de «candidatos:» y qué parte de los
    cupos gratuitos del día se ha usado («cupos:»). Lo llama run_graph() al acabar el informe; con `reiniciar`, la
    cuenta vuelve a cero para la siguiente ejecución. Cuenta desde el resumen anterior: incluye el discovery."""
    with _bloqueo:
        consumo = {modelo: dict(cuenta) for modelo, cuenta in _consumo.items()}
        if reiniciar:
            _consumo.clear()
    if not consumo:
        return
    config = _config()
    precios, cupos = config.get("precios") or {}, config.get("cupos") or {}
    total = {"llamadas": 0, "entrada": 0, "salida": 0}
    pagando, sin_precio, neuronas, lineas = 0.0, [], {}, []
    for modelo, cuenta in sorted(consumo.items(), key=lambda m: -(m[1]["entrada"] + m[1]["salida"])):
        for clave in total:
            total[clave] += cuenta[clave]
        precio = precios.get(modelo)
        if precio:
            pagando += _coste(cuenta, precio)
            pago = f" · pagando, {_dolares(_coste(cuenta, precio))} ({precio.get('como', modelo)})"
        else:
            sin_precio.append(modelo)
            pago = " · sin precio en el yaml"
        proveedor, _, nombre = modelo.partition("/")
        cupo = cupos.get(proveedor) or {}
        gratis = []
        if cupo.get("peticiones_dia"):
            gratis.append(f"{_por_ciento(cuenta['llamadas'], cupo['peticiones_dia'])} de sus "
                          f"{_miles(cupo['peticiones_dia'])} peticiones al día")
        if cupo.get("tokens_dia"):
            gratis.append(f"{_por_ciento(cuenta['entrada'] + cuenta['salida'], cupo['tokens_dia'])} de sus "
                          f"{_miles(cupo['tokens_dia'])} tokens al día")
        tasa = (cupo.get("neuronas_por_millon") or {}).get(nombre)
        if tasa:
            neuronas[proveedor] = neuronas.get(proveedor, 0) + _coste(cuenta, {"entrada": tasa[0], "salida": tasa[1]})
        gratis = f" · gratis: {', '.join(gratis)}" if gratis else ""
        lineas.append(f"  {modelo}: {_llamadas(cuenta['llamadas'])}, {_miles(cuenta['entrada'])} tokens de entrada y "
                      f"{_miles(cuenta['salida'])} de salida{pago}{gratis}")
    for proveedor, gastadas in neuronas.items():
        al_dia = (cupos.get(proveedor) or {}).get("neuronas_dia")
        if al_dia:
            lineas.append(f"  {proveedor}: {_miles(round(gastadas))} de sus {_miles(al_dia)} neuronas al día "
                          f"({_por_ciento(gastadas, al_dia)})")
    for nombre, precio in (config.get("candidatos") or {}).items():
        lineas.append(f"  Todo con {nombre}: {_dolares(_coste(total, precio))}")
    falta = f" (sin contar {', '.join(sin_precio)}, que no tiene precio)" if sin_precio else ""
    lineas.insert(0, f"[gateway] Consumo de la ejecución: {_llamadas(total['llamadas'])}, {_miles(total['entrada'])} "
                     f"tokens de entrada y {_miles(total['salida'])} de salida; pagando, {_dolares(pagando)}{falta}")
    _escribir("\n".join(lineas))

def _modelo_usado(respuesta, kwargs: dict) -> str:
    """El modelo pedido o, si respondió una reserva, «pedido → reserva X». LiteLLM dice en la respuesta qué modelo y
    qué proveedor han respondido; se busca entre las reservas de la llamada."""
    pedido = str(kwargs.get("model", ""))
    usado = str(getattr(respuesta, "model", "") or "")
    proveedor = str((getattr(respuesta, "_hidden_params", None) or {}).get("custom_llm_provider") or "")
    reservas = [str(r.get("model", "") if isinstance(r, dict) else r) for r in kwargs.get("fallbacks") or []]
    for reserva in reservas:
        if usado and reserva.endswith(usado) and (not proveedor or reserva.startswith(f"{proveedor}/")):
            return f"{pedido} → reserva {reserva}"
    if proveedor and not pedido.startswith(f"{proveedor}/"):
        otra = next((r for r in reservas if r.startswith(f"{proveedor}/")), f"{proveedor}/{usado}")
        return f"{pedido} → reserva {otra}"
    return pedido


def _completion_registrada(*args, **kwargs):
    """litellm.completion, y si la llamada viene del gateway (lleva su tarea), se registra. La usan tanto CrewAI como
    completar(), así que sale una línea por llamada, con el modelo que de verdad ha respondido. Antes se registraba
    con los eventos de CrewAI, que solo saben el primer modelo: el 3 nov, el log decía Gemini 3.8 Flash en las 7 fases
    del roadmap y no había respondido en ninguna."""
    original = _completion_registrada.original
    metadata = kwargs.get("metadata")
    tarea = metadata.get("aidm_tarea") if isinstance(metadata, dict) else None
    if not tarea:
        return original(*args, **kwargs)
    inicio = time.monotonic()
    try:
        respuesta = original(*args, **kwargs)
    except Exception as exc:
        try:
            _registrar(tarea, str(kwargs.get("model", "")), kwargs.get("messages"), "", None,
                       time.monotonic() - inicio, f"{type(exc).__name__}: {exc}")
        except Exception:
            pass  # el registro nunca puede romper la llamada
        raise
    try:
        uso = getattr(respuesta, "usage", None)
        uso = {"prompt_tokens": getattr(uso, "prompt_tokens", None),
               "completion_tokens": getattr(uso, "completion_tokens", None)} if uso else None
        texto = respuesta.choices[0].message.content or ""
        cortada = getattr(respuesta.choices[0], "finish_reason", None) == "length"
        _registrar(tarea, _modelo_usado(respuesta, kwargs), kwargs.get("messages"), texto, uso,
                   time.monotonic() - inicio, cortada=cortada)
    except Exception:
        pass  # una respuesta en streaming, por ejemplo: se devuelve igual, sin registrar
    return respuesta


if not getattr(litellm.completion, "aidm_gateway", False):  # una sola vez, aunque el módulo se importe de nuevo
    _completion_registrada.original = litellm.completion
    _completion_registrada.aidm_gateway = True
    litellm.completion = _completion_registrada