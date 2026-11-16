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
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# Fallo 66: nada de red al arrancar ni en cada ejecución. Van antes de importar LiteLLM y CrewAI, que los leen al cargarse.
# LiteLLM bajaba de GitHub su tabla de modelos en cada arranque (hasta 5 s, más con el proxy de la empresa); la que trae
# instalada tiene todos los del yaml. CrewAI mandaba sus trazas a telemetry.crewai.com en cada crew.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

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


# --- Fallo 95: un modelo que no está en el plan del proveedor se salta ---
# El 14 nov, Mistral Large 4 contestó 403 «This model is not available in your subscription tier» (tier_not_allowed)
# en las 7 llamadas de redacción; LiteLLM reintentó cada una dos veces antes de pasar a Gemini. Ese error no se arregla
# repitiendo: no se reintenta, el modelo se salta durante el resto del proceso y el log lo dice una sola vez.
_FUERA_DEL_PLAN = re.compile(r"tier_not_allowed|not available in your subscription tier", re.IGNORECASE)
_saltados: dict[str, str] = {}  # modelo → por qué se salta
_bloqueo_saltados = threading.Lock()


def fuera_del_plan(error) -> bool:
    return bool(_FUERA_DEL_PLAN.search(str(error)))


def _saltar(modelo, error) -> None:
    """Fallo 95: apunta el modelo como fuera del plan y lo dice en el log, solo la primera vez."""
    modelo = str(modelo or "")
    if not modelo:
        return
    with _bloqueo_saltados:
        if modelo in _saltados:
            return
        _saltados[modelo] = "403 tier_not_allowed"
    _escribir(f"[gateway] {modelo} no está en el plan de su proveedor (403 tier_not_allowed: «not available in your "
              "subscription tier»): no se reintenta y se salta durante el resto del proceso")


def _sin_saltados(kwargs: dict) -> dict:
    """Fallo 95: la llamada sin los modelos saltados; si el primero lo está, va la primera reserva que no lo esté, con
    sus reglas. Si lo están todos, la llamada se queda como está y el error se verá."""
    if not _saltados:
        return kwargs
    reservas = [r if isinstance(r, dict) else {"model": str(r)} for r in kwargs.get("fallbacks") or []]
    quedan = [r for r in reservas if str(r.get("model")) not in _saltados]
    if str(kwargs.get("model", "")) not in _saltados:
        return {**kwargs, "fallbacks": quedan} if len(quedan) != len(reservas) else kwargs
    if not quedan:
        return kwargs
    nuevos = {k: v for k, v in kwargs.items() if k not in ("num_retries", "timeout", "max_tokens", "fallbacks")}
    nuevos.update(quedan[0])
    if quedan[1:]:
        nuevos["fallbacks"] = quedan[1:]
    return nuevos


def modelos_de(tarea: str, config: dict | None = None, con_saltados: bool = False) -> tuple[str, list[str]]:
    """El perfil de la tarea y su lista de modelos, con MODELO_<TAREA> delante si está en el .env. Una tarea que no
    está en el yaml usa el perfil de «por_defecto»; un «perfil» con «/» se toma como un modelo suelto. Fallo 95: sin
    los modelos que el proveedor ha dicho que no están en el plan (salvo que no quede ninguno)."""
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
    if con_saltados:
        return perfil, lista
    return perfil, [m for m in lista if m not in _saltados] or lista


def modelo(tarea: str) -> ModeloDeTarea:
    """El modelo de una tarea (el primero de su perfil), con sus reservas. Lo escribe en el log."""
    perfil, lista = modelos_de(tarea)
    if not lista:
        _escribir(f"[gateway] {tarea}: sin modelo (perfil «{perfil}»); revisa {MODELOS_PATH}")
        return ModeloDeTarea("", tarea)
    origen = f"MODELO_{tarea.upper()} del .env" if os.getenv(f"MODELO_{tarea.upper()}", "").strip() else f"perfil {perfil}"
    reservas = f"; reservas: {', '.join(lista[1:])}" if lista[1:] else ""
    saltados = [m for m in modelos_de(tarea, con_saltados=True)[1] if m not in lista] if _saltados else []  # fallo 95
    saltados = f"; se salta {', '.join(saltados)}, fuera del plan" if saltados else ""
    _escribir(f"[gateway] {tarea}: {lista[0]} ({origen}{reservas}{saltados})")
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



# --- Fallo 92: las peticiones de hoy a cada modelo de Gemini, entre ejecuciones ---
# El plan gratuito da 20 al día por modelo, y el 12 nov se acabaron a mitad del roadmap sin que el log lo anunciara:
# cada ejecución gasta unas 10. Se cuentan las respuestas de Gemini (no las simuladas de los tests) y, si Google
# contesta que no queda cupo (RESOURCE_EXHAUSTED), el modelo queda agotado hasta el reinicio.
CUPOS_PATH = Path("data_structures/cupos_gemini.json")
_bloqueo_cupos = threading.Lock()
_SIN_CUPO = re.compile(r"RESOURCE_EXHAUSTED|free_tier_requests|exceeded your current quota", re.IGNORECASE)
# Fallo 92 (16 nov): el 14 nov, un 429 por minuto (quotaId GenerateRequestsPerMinutePerProjectPerModel-FreeTier, 5 por
# minuto) dejó Gemini «agotado hasta las 09:00 (15 de 20)» cuando quedaban 5. Solo el cupo diario (quotaId …PerDay…,
# «requests per day limit») lo deja agotado; el de por minuto lo espera el limitador del fallo 96.
_CUPO_DIARIO = re.compile(r"PerDay|per day limit", re.IGNORECASE)


def sin_cupo_diario(error) -> bool:
    """Fallo 92: el error es el del cupo diario de Gemini, no el de por minuto (ni otro 429)."""
    texto = str(error)
    return bool(_SIN_CUPO.search(texto) and _CUPO_DIARIO.search(texto))


def _cupo_gemini(config: dict | None = None) -> tuple[int, str]:
    cupo = ((config if config is not None else _config()).get("cupos") or {}).get("gemini") or {}
    return int(cupo.get("peticiones_dia") or 0), str(cupo.get("reinicio") or "09:00")


def _dia_de_cupo(reinicio: str, ahora: datetime | None = None) -> str:
    """El día del cupo: el de hoy desde la hora de reinicio; antes, el de ayer (a las 08:59 aún cuenta el de ayer)."""
    ahora = ahora or datetime.now()
    hora, _, minuto = reinicio.partition(":")
    corte = ahora.replace(hour=int(hora), minute=int(minuto or 0), second=0, microsecond=0)
    return (ahora.date() if ahora >= corte else ahora.date() - timedelta(days=1)).isoformat()


def _leer_cupos(reinicio: str, ahora: datetime | None = None) -> dict:
    """Los contadores del día de cupo; si el archivo es de otro día (o no está o no se lee), empiezan de cero."""
    dia = _dia_de_cupo(reinicio, ahora)
    try:
        datos = json.loads(CUPOS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        datos = {}
    if not isinstance(datos, dict) or datos.get("dia") != dia or not isinstance(datos.get("modelos"), dict):
        datos = {"dia": dia, "modelos": {}}
    return datos


def _anotar_gemini(modelo: str, agotado: bool = False, ahora: datetime | None = None) -> None:
    """Una petición más a un modelo de Gemini, o (agotado) el aviso de Google de que no queda cupo hoy."""
    if not str(modelo).startswith("gemini/"):
        return
    try:
        _, reinicio = _cupo_gemini()
        with _bloqueo_cupos:
            datos = _leer_cupos(reinicio, ahora)
            cuenta = datos["modelos"].setdefault(str(modelo), {"peticiones": 0, "agotado": False})
            if agotado:
                cuenta["agotado"] = True
            else:
                cuenta["peticiones"] = int(cuenta.get("peticiones", 0)) + 1
            CUPOS_PATH.parent.mkdir(parents=True, exist_ok=True)
            CUPOS_PATH.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass  # el contador nunca puede romper una llamada


def cupos_gemini(config: dict | None = None, ahora: datetime | None = None) -> list[tuple[str, int, int, bool]]:
    """(modelo, peticiones de hoy, límite, agotado) de cada modelo de Gemini del yaml y de los que ya se han usado hoy."""
    config = config if config is not None else _config()
    limite, reinicio = _cupo_gemini(config)
    if not limite:
        return []
    with _bloqueo_cupos:
        usados = _leer_cupos(reinicio, ahora)["modelos"]
    del_yaml = [_resolver(m) for lista in (config.get("perfiles") or {}).values() for m in lista or []]
    modelos = list(dict.fromkeys(m for m in del_yaml + list(usados) if str(m).startswith("gemini/")))
    return [(m, int((usados.get(m) or {}).get("peticiones", 0)), limite, bool((usados.get(m) or {}).get("agotado")))
            for m in modelos]


def _texto_cupo(peticiones: int, limite: int, agotado: bool, reinicio: str) -> str:
    if agotado or peticiones >= limite:
        return f"agotado hasta las {reinicio} ({peticiones} de {limite} peticiones hoy)"
    return f"quedan {limite - peticiones} de {limite} peticiones hoy"


def escribir_cupos(momento: str = "Al empezar el informe") -> None:
    """Fallo 92: una línea en el log con lo que queda hoy de cada modelo de Gemini. La llama el grafo al empezar."""
    try:
        config = _config()
        _, reinicio = _cupo_gemini(config)
        partes = [f"{m}: {_texto_cupo(n, limite, agotado, reinicio)}" for m, n, limite, agotado in cupos_gemini(config)]
    except Exception:
        return
    if partes:
        _escribir(f"[gateway] {momento}, cupo de Gemini (vuelve a empezar a las {reinicio}): " + "; ".join(partes))


class _AvisoDeCupo(logging.Handler):
    """Fallo 92: LiteLLM escribe «Fallback attempt failed for model gemini/…: … RESOURCE_EXHAUSTED …» cuando falla un
    modelo de la lista; ese error no llega al gateway si responde una reserva. Así se sabe que el cupo está agotado."""

    _FALLO = re.compile(r"Fallback attempt failed for model (gemini/\S+?): (.*)", re.DOTALL)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # El handler de LiteLLM, que va antes, deja el mensaje ya formateado (sin args): se lee del texto. El error
            # entero, si viene (exc_info), por si el mensaje llega recortado
            m = self._FALLO.match(record.getMessage())
            error = str(record.exc_info[1]) if record.exc_info and record.exc_info[1] else ""
            if m and sin_cupo_diario(f"{m.group(2)} {error}"):  # fallo 92: solo el diario
                _anotar_gemini(m.group(1), agotado=True)
        except Exception:
            pass


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
    # Fallo 92: lo que queda hoy de cada modelo de Gemini, contando también las ejecuciones anteriores
    _, reinicio = _cupo_gemini(config)
    for modelo, peticiones, limite, agotado in cupos_gemini(config):
        if peticiones or agotado or modelo in consumo:
            lineas.append(f"  {modelo}, cupo de hoy: {_texto_cupo(peticiones, limite, agotado, reinicio)}")
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


# Fallo 82: el 12 nov, la fase de BI del roadmap fueron 8.192 «!» (gpt-oss-120b en Cloudflare, reserva de Gemini sin
# cupo), y se dio por buena: solo se avisaba de que estaba cortada. Ahora una respuesta degenerada cuenta como fallo y
# se pide a la siguiente reserva. Es degenerada si un mismo trozo de 1–30 caracteres se repite 20 veces o más seguidas
# y ocupa 400 caracteres o más, o si un texto largo usa casi un solo carácter. Las tablas markdown, los separadores
# (|---|, ----) y las listas no cuentan: un trozo hecho solo de esos signos tiene que ocupar 3.000 caracteres.
_SIGNOS_MARKDOWN = set("|-:=*_#~`+.>/\\ \t\r\n")
_TROZO_REPETIDO = re.compile(r"(.{1,30}?)\1{19,}", re.DOTALL)
AVISO_DEGENERADA = ("**No se ha podido generar esta sección.** Los modelos disponibles han devuelto un texto no "
                    "válido (por ejemplo, un mismo carácter repetido miles de veces). Hay que volver a generarla.")


def motivo_degenerada(texto: str) -> str:
    """Fallo 82: por qué una respuesta es degenerada («"!" repetido 8.192 veces seguidas»), o "" si es normal."""
    texto = (texto or "").strip()
    if len(texto) < 200:
        return ""
    for m in _TROZO_REPETIDO.finditer(texto):
        trozo, largo = m.group(1), len(m.group(0))
        if not trozo.strip():
            continue  # espacios y saltos de línea
        if largo >= (3000 if set(trozo) <= _SIGNOS_MARKDOWN else 400):
            return f"«{trozo.strip()[:20]}» repetido {_miles(largo // len(trozo))} veces seguidas"
    if len(texto) >= 1000:
        visibles = [c for c in texto if not c.isspace()]
        distintos = set(visibles)
        if len(distintos) <= 10:
            return f"solo {len(distintos)} caracteres distintos en {_miles(len(texto))}"
        letras = Counter(c for c in visibles if c not in _SIGNOS_MARKDOWN)
        if letras:
            caracter, veces = letras.most_common(1)[0]
            if veces > 0.5 * len(visibles):
                return f"«{caracter}» es el {round(100 * veces / len(visibles))} % del texto"
    return ""


def _reservas_que_quedan(respuesta, kwargs: dict) -> tuple[str, list[dict]]:
    """Fallo 82: el modelo que ha respondido y las reservas que van detrás de él en la lista de la llamada."""
    usado = _modelo_usado(respuesta, kwargs).rpartition(" → reserva ")[2]
    reservas = [r if isinstance(r, dict) else {"model": str(r)} for r in kwargs.get("fallbacks") or []]
    nombres = [str(kwargs.get("model", ""))] + [str(r.get("model", "")) for r in reservas]
    return usado, reservas[nombres.index(usado):] if usado in nombres else reservas


# --- Fallo 96: como mucho N peticiones por minuto móvil a cada modelo ---
# Gemini gratis admite 5 por minuto y modelo (quotaId GenerateRequestsPerMinutePerProjectPerModel-FreeTier). El 14 nov,
# con las 7 fases del roadmap a la vez, la de BI recibió un 429 y pasó a Cloudflare. Ahora la llamada que llegaría al
# límite espera su turno. Cada modelo guarda las horas reservadas de sus peticiones; una nueva toma la primera hora que
# deje como mucho N en cualquier minuto. Con un bloqueo, vale entre los hilos del roadmap y de costes ∥ perfiles.
VENTANA = 60.0  # segundos
MARGEN = 1.0    # segundos de más, para no rozar el límite
_reservas_por_minuto: dict[str, list[float]] = {}
_bloqueo_minuto = threading.Lock()


def _limite_por_minuto(modelo: str, config: dict | None = None) -> int:
    """Las peticiones por minuto del yaml para un modelo (las suyas o las de su proveedor); 0 si no tiene límite."""
    config = config if config is not None else _config()
    proveedor = str(modelo).partition("/")[0]
    reglas = {**((config.get("proveedores") or {}).get(proveedor) or {}),
              **((config.get("modelos") or {}).get(str(modelo)) or {})}
    return int(reglas.get("peticiones_por_minuto") or 0)


def _reservar_hueco(modelo: str, limite: int, ahora: float | None = None) -> float:
    """Fallo 96: reserva la hora de la siguiente petición a `modelo` y devuelve cuántos segundos hay que esperar."""
    if limite <= 0:
        return 0.0
    ahora = time.monotonic() if ahora is None else ahora
    with _bloqueo_minuto:
        horas = [h for h in _reservas_por_minuto.get(modelo, []) if h > ahora - VENTANA - MARGEN]
        hora = ahora if len(horas) < limite else max(ahora, horas[-limite] + VENTANA + MARGEN)
        horas.append(hora)
        _reservas_por_minuto[modelo] = horas
    return hora - ahora


def _esperar_turno(kwargs: dict) -> float:
    """Fallo 96: reserva el hueco del modelo de la llamada y dice en el log cuánto va a esperar. Devuelve los segundos."""
    modelo = str(kwargs.get("model", ""))
    if "mock_response" in kwargs:
        return 0.0  # las respuestas simuladas de los tests no van al proveedor
    try:
        limite = _limite_por_minuto(modelo)
        espera = _reservar_hueco(modelo, limite)
    except Exception:
        return 0.0  # el limitador nunca puede romper una llamada
    if espera > 0:
        metadata = kwargs.get("metadata")
        tarea = metadata.get("aidm_tarea") if isinstance(metadata, dict) else ""
        segundos = f"{espera:.1f}".replace(".", ",")
        _escribir(f"[gateway] {tarea or '¿tarea?'} · {modelo} espera {segundos} s para no pasar de {limite} "
                  "peticiones por minuto")
    return espera


async def _acompletion_con_turno(*args, **kwargs):
    """Fallo 96: cada modelo de la lista de reservas va por litellm.acompletion: aquí espera su turno si hace falta."""
    espera = _esperar_turno(kwargs)
    if espera > 0:
        await asyncio.sleep(espera)
    return await _acompletion_con_turno.original(*args, **kwargs)


def _llamar(original, args, kwargs):
    """Fallo 95: sin reservas, LiteLLM reintenta en la misma llamada aunque el error sea de plan. Aquí el primer intento
    va sin reintentos y, si el error no es de plan, se repite con los que quedan (los mismos intentos en total que
    antes). Con reservas, los reintentos los corta _num_reintentos()."""
    reintentos = int(kwargs.get("num_retries") or 0)
    if kwargs.get("fallbacks"):
        return original(*args, **kwargs)  # cada modelo de la lista espera su turno en _acompletion_con_turno
    espera = _esperar_turno(kwargs)  # fallo 96: sin reservas, la llamada va directa al modelo
    if espera > 0:
        time.sleep(espera)
    if not reintentos:
        return original(*args, **kwargs)
    try:
        return original(*args, **{**kwargs, "num_retries": 0})
    except Exception as exc:
        if fuera_del_plan(exc):
            raise
    return original(*args, **{**kwargs, "num_retries": reintentos - 1})


def _num_reintentos(kwargs=None, exception=None, **resto):
    """Fallo 95: los reintentos que decide LiteLLM tras un error dentro de la lista de reservas (cada modelo va por
    acompletion). Un error de plan no se reintenta: se salta el modelo y LiteLLM pasa a la siguiente reserva."""
    if fuera_del_plan(exception):
        _saltar((kwargs or {}).get("model"), exception)
        return 0, kwargs
    return _num_reintentos.original(kwargs=kwargs, exception=exception, **resto)


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
    kwargs = _sin_saltados(kwargs)  # fallo 95: los modelos fuera del plan no se llaman
    inicio = time.monotonic()
    try:
        respuesta = _llamar(original, args, kwargs)
    except Exception as exc:
        if fuera_del_plan(exc):  # fallo 95: sin reservas que respondan
            _saltar(kwargs.get("model"), exc)
        try:
            _registrar(tarea, str(kwargs.get("model", "")), kwargs.get("messages"), "", None,
                       time.monotonic() - inicio, f"{type(exc).__name__}: {exc}")
            if sin_cupo_diario(exc):  # fallo 92: Gemini sin cupo del día y sin reservas que respondan
                _anotar_gemini(str(kwargs.get("model", "")), agotado=True)
        except Exception:
            pass  # el registro nunca puede romper la llamada
        raise
    if "mock_response" not in kwargs:  # fallo 92: una petición más del cupo de Gemini (las de los tests no cuentan)
        _anotar_gemini(_modelo_usado(respuesta, kwargs).rpartition(" → reserva ")[2])
    texto = ""
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
    # Fallo 82: una respuesta degenerada no llega al informe. Se pide a la siguiente reserva (con sus reglas y las que
    # quedan detrás); si no queda ninguna, se devuelve un aviso en lugar de la basura. No se lanza una excepción:
    # CrewAI repetiría la tarea (más peticiones del cupo) y, en la propuesta, tiraría las cuatro secciones.
    try:
        motivo = motivo_degenerada(texto) if isinstance(texto, str) else ""
    except Exception:
        motivo = ""  # la comprobación nunca puede romper la llamada
    if not motivo:
        return respuesta
    usado, quedan = _reservas_que_quedan(respuesta, kwargs)
    if not quedan:
        _escribir(f"[gateway] {tarea} · {usado} ha dado una respuesta degenerada ({motivo}) y no quedan reservas: "
                  "la sección sale vacía, con un aviso")
        respuesta.choices[0].message.content = AVISO_DEGENERADA
        return respuesta
    siguiente, resto = quedan[0], quedan[1:]
    _escribir(f"[gateway] {tarea} · {usado} ha dado una respuesta degenerada ({motivo}); pasa a la reserva "
              f"{siguiente.get('model')}")
    nuevos = {k: v for k, v in kwargs.items() if k not in ("num_retries", "timeout", "max_tokens", "fallbacks")}
    nuevos.update(siguiente)  # el modelo de la reserva y sus reglas
    if resto:
        nuevos["fallbacks"] = resto
    return _completion_registrada(*args, **nuevos)


if not getattr(litellm.completion, "aidm_gateway", False):  # una sola vez, aunque el módulo se importe de nuevo
    _completion_registrada.original = litellm.completion
    _completion_registrada.aidm_gateway = True
    litellm.completion = _completion_registrada
    logging.getLogger("LiteLLM").addHandler(_AvisoDeCupo())  # fallo 92
    _num_reintentos.original = litellm.utils._get_wrapper_num_retries  # fallo 95
    litellm.utils._get_wrapper_num_retries = _num_reintentos
    _acompletion_con_turno.original = litellm.acompletion  # fallo 96
    litellm.acompletion = _acompletion_con_turno