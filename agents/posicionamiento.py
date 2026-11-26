"""Fallo 135: el cuadrante de posicionamiento de la pantalla de espera.

Coloca al cliente (hoy y con el proyecto) frente a 2-4 competidores reales, solo en el área del proyecto. Es una sola
llamada, en un hilo aparte que lanza graph.plataforma_node al empezar el informe y que nadie espera: nunca retrasa ni
rompe el informe. Si sale bien, deja data_structures/posicionamiento.json (fuera de git), que la web lee con
GET /api/posicionamiento; si falla, una línea [posicionamiento] en el log y no hay cuadrante.

En los tests, AIDM_SIN_POSICIONAMIENTO=1: lanzar() no hace nada (ni hilo ni llamada).
"""
import json
import logging
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

from utils import es_valor_de_relleno

logger = logging.getLogger("aidm")

RUTA = Path("data_structures/posicionamiento.json")
TAREA = "posicionamiento"
# Groq gratis: 8.000 tokens por minuto, contando la entrada y el max_tokens pedido. El prompt no pasa de PROMPT_MAX
MAX_TOKENS = 3500
PROMPT_MAX = 3000
CONFIANZAS = ("alta", "media", "baja")
MAX_COMPETIDORES = 4
AVISO = "Posición orientativa estimada por IA a partir de información pública general. No es un estudio de mercado."

CAMPOS = ("sector", "tamaño_empresa", "objetivo_del_proyecto", "plazo_objetivo", "enfoque_ai",
          "herramienta_de_visualizacion", "madurez_analitica_actual", "stack_tecnologico_actual",
          "tiene_equipo_datos", "tamano_equipo_tecnico")
IMPRESCINDIBLES = ("sector", "objetivo_del_proyecto")  # sin ellos no se llama al modelo
MAX_CAMPO = 300        # caracteres de cada dato
MAX_EVIDENCIAS = 1500  # caracteres de las evidencias juntas (de ahí sale la zona: el caso no tiene ese campo)

INSTRUCCIONES = """Eres analista de mercado. Sitúa al cliente frente a sus competidores en un cuadrante, solo en el área de su proyecto de datos.

Responde solo con un JSON, sin texto alrededor:
{"area": "<el área del proyecto, en pocas palabras>",
 "cliente": {"hoy": {"x": <0-10>, "y": <0-10>, "motivo": "<frase corta>"},
             "con_proyecto": {"x": <0-10>, "y": <0-10>, "motivo": "<frase corta>"}},
 "competidores": [{"nombre": "<empresa>", "x": <0-10>, "y": <0-10>, "motivo": "<frase corta>", "confianza": "alta|media|baja"}]}

- x = madurez actual en el área; y = ambición e inversión en el área. De 0 a 10.
- «hoy» cuadra con la madurez analítica que dice el cliente. «con_proyecto» sale del objetivo y del plazo: dónde estará al acabarlo.
- Competidores: 3, como mucho 4, empresas reales del mismo sector, mejor con presencia en España y en su zona. Si no conoces bastantes, pon 2: mejor 2 que una inventada. Nunca inventes una empresa.
- «confianza»: lo segura que es la posición de ese competidor según lo que se sabe de él públicamente.
- Motivos cualitativos, de una frase: sin cifras, sin importes y sin porcentajes. No escribas el nombre del cliente en ningún motivo.
- Escribe en el idioma de los datos del cliente."""


def _normalizar(texto) -> str:
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", texto)).strip()


_FORMA_JURIDICA = re.compile(r"\b(?:s ?l ?u?|s ?a ?u?|s ?l ?l|s ?coop|sociedad limitada|sociedad anonima)\s*$")


def _nombre_cliente(problem_case: dict) -> str:
    """El nombre del cliente sin la forma jurídica, normalizado; '' si no lo hay."""
    valor = ((problem_case or {}).get("nombre_del_cliente") or {}).get("value")
    if es_valor_de_relleno(valor):
        return ""
    return _FORMA_JURIDICA.sub("", _normalizar(valor)).strip()


def _corto(valor, tope: int) -> str:
    texto = " ".join(str(valor).split())
    return texto if len(texto) <= tope else texto[:tope].rsplit(" ", 1)[0] + "…"


def tokens_aproximados(texto: str) -> int:
    """Por lo alto: un token cada 3 caracteres."""
    return len(texto) // 3 + 1


def mensajes(problem_case: dict) -> list[dict]:
    """El prompt: las instrucciones y los datos del cliente que sirven para situarlo. Nunca pasa de PROMPT_MAX."""
    caso = problem_case or {}
    lineas = []
    nombre = (caso.get("nombre_del_cliente") or {}).get("value")
    if not es_valor_de_relleno(nombre):
        lineas.append(f"- nombre_del_cliente (solo para saber quién es; no lo escribas en los motivos): {_corto(nombre, 120)}")
    for campo in CAMPOS:
        valor = (caso.get(campo) or {}).get("value")
        if isinstance(valor, list):
            valor = ", ".join(str(v) for v in valor if not es_valor_de_relleno(v))
        if not es_valor_de_relleno(valor) and str(valor).strip():
            lineas.append(f"- {campo}: {_corto(valor, MAX_CAMPO)}")
    evidencias = [str(v).strip() for v in (caso.get("evidencias") or {}).get("value") or [] if str(v).strip()]
    if evidencias:
        lineas.append("- evidencias (de aquí sale su zona): " + _corto(" | ".join(evidencias), MAX_EVIDENCIAS))
    usuario = "DATOS DEL CLIENTE\n" + "\n".join(lineas)
    while tokens_aproximados(INSTRUCCIONES + usuario) > PROMPT_MAX and len(usuario) > 200:
        usuario = _corto(usuario, len(usuario) - 300)  # no debería pasar con los topes de arriba
    return [{"role": "system", "content": INSTRUCCIONES}, {"role": "user", "content": usuario}]


def faltan(problem_case: dict) -> list[str]:
    """Los campos imprescindibles que el caso no tiene (vacíos o de relleno)."""
    return [c for c in IMPRESCINDIBLES
            if es_valor_de_relleno(((problem_case or {}).get(c) or {}).get("value"))
            or not str(((problem_case or {}).get(c) or {}).get("value") or "").strip()]


def _numero(valor) -> float | None:
    if isinstance(valor, bool):
        return None
    try:
        n = float(str(valor).replace(",", ".")) if isinstance(valor, str) else float(valor)
    except (TypeError, ValueError):
        return None
    return round(min(10.0, max(0.0, n)), 1) if n == n else None  # n == n: fuera los NaN


_ANIO = re.compile(r"\b(?:19|20)\d{2}\b")


def _motivo(texto, cliente: str, quien: str) -> str:
    """El motivo, o '' si lleva cifras (los años sí valen) o el nombre del cliente."""
    motivo = " ".join(str(texto or "").split())
    if re.search(r"\d", _ANIO.sub("", motivo)):
        logger.warning("[posicionamiento] Motivo de %s con cifras: se quita («%s»)", quien, _corto(motivo, 80))
        return ""
    if cliente and f" {cliente} " in f" {_normalizar(motivo)} ":
        logger.warning("[posicionamiento] Motivo de %s con el nombre del cliente: se quita", quien)
        return ""
    return motivo


def limpiar(datos, problem_case: dict) -> dict | None:
    """Las guardias: x e y entre 0 y 10, como mucho 4 competidores con nombre, fuera los motivos con cifras o con el
    nombre del cliente y la confianza en alta, media o baja. None si no queda cuadrante (sin área, sin las dos
    posiciones del cliente o sin competidores)."""
    if not isinstance(datos, dict):
        return None
    cliente = _nombre_cliente(problem_case)
    area = " ".join(str(datos.get("area") or "").split())
    posiciones = datos.get("cliente")
    posiciones = posiciones if isinstance(posiciones, dict) else {}
    limpio_cliente = {}
    for momento in ("hoy", "con_proyecto"):
        punto = posiciones.get(momento)
        punto = punto if isinstance(punto, dict) else {}
        x, y = _numero(punto.get("x")), _numero(punto.get("y"))
        if x is None or y is None:
            logger.warning("[posicionamiento] Sin la posición «%s» del cliente: no hay cuadrante", momento)
            return None
        limpio_cliente[momento] = {"x": x, "y": y, "motivo": _motivo(punto.get("motivo"), cliente, f"«{momento}»")}
    competidores, vistos = [], set()
    lista = datos.get("competidores")
    for c in lista if isinstance(lista, list) else []:
        if not isinstance(c, dict):
            continue
        nombre = " ".join(str(c.get("nombre") or "").split())
        x, y = _numero(c.get("x")), _numero(c.get("y"))
        clave = _FORMA_JURIDICA.sub("", _normalizar(nombre)).strip()
        if not clave or x is None or y is None or clave in vistos:
            continue
        if cliente and (clave == cliente or f" {cliente} " in f" {clave} "):
            logger.warning("[posicionamiento] El cliente salía como competidor: se quita")
            continue
        vistos.add(clave)
        confianza = str(c.get("confianza") or "").strip().lower()
        competidores.append({"nombre": nombre, "x": x, "y": y, "motivo": _motivo(c.get("motivo"), cliente, nombre),
                             "confianza": confianza if confianza in CONFIANZAS else "baja"})
    if len(competidores) > MAX_COMPETIDORES:
        logger.warning("[posicionamiento] %d competidores: se quedan los %d primeros", len(competidores), MAX_COMPETIDORES)
        competidores = competidores[:MAX_COMPETIDORES]
    if not area or not competidores:
        logger.warning("[posicionamiento] %s: no hay cuadrante", "Sin área" if not area else "Sin competidores")
        return None
    en = area[0].lower() + area[1:]
    return {"area": area, "ejes": {"x": f"Madurez actual en {en}", "y": f"Ambición e inversión en {en}"},
            "cliente": limpio_cliente, "competidores": competidores, "aviso": AVISO}


# El id de la ejecución que puede escribir: cada lanzar() y cada «Nueva sesión» (olvidar) lo cambian, y un hilo
# que termina después ya no escribe.
_cerrojo = threading.Lock()
_actual = 0


def _borrar(ruta: Path) -> None:
    try:
        Path(ruta).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("[posicionamiento] No se ha podido borrar %s: %s", ruta, exc)


def olvidar(ruta: Path | None = None) -> None:
    """«Nueva sesión»: borra el cuadrante y ningún hilo que siga en marcha lo vuelve a escribir."""
    global _actual
    with _cerrojo:
        _actual += 1
        _borrar(ruta or RUTA)


def pedir(problem_case: dict, modelo=None) -> str:
    """La llamada al modelo (Groq y Cloudflare de reserva, sin Gemini). reasoning_effort bajo; drop_params, para que
    LiteLLM no falle con un proveedor que no lo admita. `modelo`: otro, sin reservas (pruebas_modelos/)."""
    import gateway
    modelo = gateway.ModeloDeTarea(modelo, TAREA) if modelo else gateway.modelo("posicionamiento")
    respuesta = gateway.completar(modelo, mensajes(problem_case), max_tokens=MAX_TOKENS,
                                  reasoning_effort="low", drop_params=True)
    return respuesta.choices[0].message.content or ""


def trabajar(problem_case: dict, mio: int, ruta: Path | None = None) -> dict | None:
    """Lo que hace el hilo: pide el JSON, lo pasa por las guardias y lo guarda si su ejecución sigue siendo la actual.
    Nunca lanza una excepción."""
    inicio = time.monotonic()
    try:
        from agents.discovery_agent_logic import _leer_json
        datos = limpiar(_leer_json(pedir(problem_case), "posicionamiento", "no hay cuadrante"),
                        problem_case)
        if datos is None:
            return None
        with _cerrojo:
            if mio != _actual:
                logger.warning("[posicionamiento] La ejecución que lo pidió ya no es la actual: no se guarda")
                return None
            ruta = Path(ruta or RUTA)
            temporal = ruta.with_suffix(".tmp")
            temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporal, ruta)  # la web nunca lee un archivo a medias
        logger.info("[posicionamiento] Cuadrante listo en %.0f s: %d competidores", time.monotonic() - inicio,
                    len(datos["competidores"]))
        return datos
    except Exception as exc:
        logger.warning("[posicionamiento] Sin cuadrante (%s: %s)", type(exc).__name__, _corto(exc, 200))
        return None


def lanzar(problem_case: dict, ruta: Path | None = None) -> None:
    """Borra el cuadrante anterior y lanza el hilo sin esperarlo. En los tests (AIDM_SIN_POSICIONAMIENTO=1), nada.
    Sin sector u objetivo no se llama al modelo: el 26 nov, con el caso vacío, se gastó una llamada para nada."""
    global _actual
    if os.getenv("AIDM_SIN_POSICIONAMIENTO", "").strip() == "1":
        return
    try:
        with _cerrojo:
            _actual += 1
            mio = _actual
            ruta = Path(ruta or RUTA)
            _borrar(ruta)
        if falta := faltan(problem_case):
            logger.warning("[posicionamiento] Caso sin datos (falta %s): no se llama al modelo", ", ".join(falta))
            return
        threading.Thread(target=trabajar, args=(problem_case, mio, ruta), name="posicionamiento", daemon=True).start()
    except Exception as exc:
        logger.warning("[posicionamiento] No se ha podido lanzar (%s: %s): el informe sigue", type(exc).__name__, exc)
