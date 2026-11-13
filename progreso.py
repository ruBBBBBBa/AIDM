"""Fallo 94: la barra de progreso del informe en Streamlit.

Cada paso que termina llama a avanzar(). Las 7 fases del roadmap y las crews de costes van en hilos, y Streamlit solo
deja pintar desde el hilo de la sesión: un hilo cualquiera solo anota el avance, y lo pinta el hilo que empezó la
barra (el de la sesión), al acabar cada paso del grafo o mientras espera a los hilos con esperar().

Fuera de Streamlit (tests, scripts) no hay función de pintar y todo esto no hace nada.
"""
import threading
from concurrent.futures import FIRST_COMPLETED, wait

_bloqueo = threading.Lock()
_estado = {"pintar": None, "hilo": None, "total": 1, "hechos": 0, "texto": "", "pintado": None}


def empezar(pintar, total: int, texto: str = "Preparando el informe…") -> None:
    """La barra empieza en 0 con `total` pasos. `pintar(fraccion, texto)` solo se llama desde este hilo."""
    with _bloqueo:
        _estado.update(pintar=pintar, hilo=threading.get_ident(), total=max(total, 1), hechos=0, texto=texto,
                       pintado=None)


def terminar() -> None:
    with _bloqueo:
        _estado.update(pintar=None, hilo=None, pintado=None)


def avanzar(hecho: str) -> None:
    """Un paso más, desde cualquier hilo. Si es el hilo de la sesión, se pinta ya; si no, al pintar el siguiente."""
    with _bloqueo:
        if _estado["pintar"] is None:
            return
        _estado["hechos"] = min(_estado["hechos"] + 1, _estado["total"])
        _estado["texto"] = f"{hecho} · {_estado['hechos']} de {_estado['total']} pasos"
    pintar_pendiente()


def pintar_pendiente() -> None:
    """Pinta lo que han anotado los hilos, si hay algo nuevo. Desde otro hilo que no sea el de la sesión no hace nada."""
    with _bloqueo:
        pintar = _estado["pintar"]
        if pintar is None or threading.get_ident() != _estado["hilo"]:
            return
        ahora = (_estado["hechos"], _estado["texto"])
        if ahora == _estado["pintado"]:
            return
        _estado["pintado"] = ahora
        fraccion = _estado["hechos"] / _estado["total"]
    try:
        pintar(fraccion, ahora[1])
    except Exception:
        pass  # la barra es informativa: si falla, el informe sigue


def esperar(futuros, cada: float = 0.5) -> None:
    """Espera a que acaben los hilos pintando su avance, y luego devuelve sus resultados (o su excepción)."""
    pendientes = set(futuros)
    while pendientes:
        _, pendientes = wait(pendientes, timeout=cada, return_when=FIRST_COMPLETED)
        pintar_pendiente()
    for futuro in futuros:
        futuro.result()
