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

from utils import _normalize_text, revisar_servicios

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


def _clave(nombre: str) -> str:
    """'Amazon S3 (capas Bronze y Gold)' → 's3': sin paréntesis ni el nombre de la nube, para comparar."""
    normal = re.sub(r"\([^)]*\)", " ", _normalize_text(nombre or ""))
    normal = re.sub(r"\s+", " ", normal).strip()
    for prefijo in _PREFIJOS:
        if normal.startswith(prefijo):
            normal = normal[len(prefijo):]
    return normal.strip()


def corresponde(servicio: str, componente: str) -> bool:
    """Un componente de costes es de este servicio del inventario (o al revés)."""
    a, b = _clave(servicio), _clave(componente)
    return bool(a and b) and (a in b or b in a)


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


def comprobar(filas: list[dict], problem_case: dict | None) -> list[dict]:
    """Los servicios del inventario que no se pueden usar en la nube elegida (fallo 83). Lo dice el log."""
    if not filas:
        logger.warning("[inventario] La arquitectura no trae el inventario de servicios: costes y roadmap siguen "
                       "como antes (con el resumen)")
        return []
    print(f"[inventario] {len(filas)} servicios en la arquitectura ({sum(f['fase'] == 'mvp' for f in filas)} desde "
          f"el MVP): {', '.join(f['servicio'] for f in filas)}")
    return revisar_servicios("\n".join(f["servicio"] for f in filas), problem_case, "inventario")
