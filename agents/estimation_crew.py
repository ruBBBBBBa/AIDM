import csv
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from html import escape
from pathlib import Path
from typing import Any, Literal, get_args

import litellm
from crewai import Agent, Crew, Process, Task
from pydantic import BaseModel, Field, field_validator

from utils import _make_agent, _normalize_text, build_llm, contexto_cliente, load_agents_config

logger = logging.getLogger("aidm")


def _patch_crewai_union_schema() -> None:
    """
    crewai <=1.14.7 no maneja tipos JSON Schema en formato lista (ej. ['null', 'array']).
    Este patch normaliza esos tipos antes de que lleguen al parser de crewai.
    """
    try:
        import crewai.utilities.pydantic_schema_utils as _su
        _orig = _su._json_schema_to_pydantic_type

        def _patched(json_schema, root_schema, name_=None, enrich_descriptions=True, in_progress=None):
            type_ = json_schema.get("type")
            if isinstance(type_, list):
                non_null = [t for t in type_ if t != "null"]
                if len(non_null) == 1:
                    json_schema = {**json_schema, "type": non_null[0]}
                elif len(non_null) == 0:
                    return type(None)
                else:
                    return Any
            return _orig(json_schema, root_schema, name_=name_,
                         enrich_descriptions=enrich_descriptions, in_progress=in_progress)

        _su._json_schema_to_pydantic_type = _patched
    except Exception:
        pass


_patch_crewai_union_schema()


def _patch_crewai_force_required() -> None:
    """
    crewai <=1.14.7: force_additional_properties_false añade 'required: []'
    aunque 'properties' esté vacío. Groq rechaza ese schema.
    Este patch elimina required=[] de cualquier subschema donde properties esté vacío.
    """
    try:
        import crewai.utilities.pydantic_schema_utils as _su
        _orig = _su.force_additional_properties_false

        def _clean(d: object) -> None:
            if isinstance(d, dict):
                if d.get("required") == [] and not d.get("properties"):
                    d.pop("required")
                for v in d.values():
                    _clean(v)
            elif isinstance(d, list):
                for item in d:
                    _clean(item)

        def _patched(schema: object, _seen: set | None = None) -> object:
            result = _orig(schema, _seen)
            _clean(result)
            return result

        _su.force_additional_properties_false = _patched
    except Exception:
        pass


_patch_crewai_force_required()


class ResumenEjecutivo(BaseModel):
    # El LLM valora cada componente; los totales, el horizonte y el encaje con el presupuesto se calculan en Python.
    meses_proyecto: int = Field(
        description="Meses del alcance que se presupuesta (el piloto, si lo hay), según el plazo del cliente")
    presupuesto_cliente_max_eur: int = Field(
        description="Máximo del presupuesto que indica el caso del cliente, en euros; 0 si no lo indica")
    contingencia_pct: int = Field(
        default=0,
        description="Porcentaje de contingencia sobre infraestructura, licencias, formación y soporte, que se suma "
                    "en el escenario alto; 0 si no hay")
    justificacion: str = Field(
        description="De dónde salen los importes de los componentes y qué los haría variar. "
                    "Sin valorar si caben en el presupuesto: eso se calcula después, con el equipo incluido")


Categoria = Literal["infraestructura", "licencias", "formacion", "mantenimiento"]
Periodicidad = Literal["mensual", "anual", "unico"]


def _a_numero(valor: Any) -> Any:
    """'1.250 €', '1,250.50 EUR' o '32,50 €/mes' → número. Lo demás (como 'estimated') se deja tal cual y
    la validación lo rechaza, así que el modelo tiene que repetirlo con un número."""
    if not isinstance(valor, str):
        return valor
    m = re.fullmatch(r"\s*(?:€|eur)?\s*(-?\d[\d.,]*)\s*(?:€|eur|euros)?\s*(?:/\s*\w+)?\s*", valor, re.IGNORECASE)
    if not m:
        return valor
    texto = m.group(1)
    if "," in texto and "." in texto:  # el separador que va al final es el decimal
        decimal = "," if texto.rfind(",") > texto.rfind(".") else "."
        texto = texto.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in texto:
        texto = texto.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+", texto) else texto.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):  # 15.000 → miles, 32.5 → decimales
        texto = texto.replace(".", "")
    try:
        return float(texto)
    except ValueError:
        return valor


def _opcion(valor: Any, opciones: tuple[str, ...]) -> Any:
    """'Único' → 'unico', 'Formación y onboarding' → 'formacion': la opción que empieza igual, sin tildes ni mayúsculas."""
    if not isinstance(valor, str):
        return valor
    texto = _normalize_text(valor)
    return next((o for o in opciones if texto.startswith(o[:5])), valor)


class ComponenteCoste(BaseModel):
    componente: str = Field(
        description="Componente de la propuesta que se valora: un servicio cloud, una licencia, una formación o el soporte")
    categoria: Categoria
    importe_eur: float = Field(ge=0, description="Importe en euros: solo el número, sin símbolo ni texto")
    periodicidad: Periodicidad = Field(
        description="mensual o anual si se paga cada mes o cada año; unico si se paga una sola vez")
    calculo: str = Field(description="De dónde sale el importe: precio unitario × unidades, o la referencia usada")

    @field_validator("importe_eur", mode="before")
    @classmethod
    def _importe_numerico(cls, valor: Any) -> Any:
        return _a_numero(valor)

    @field_validator("categoria", mode="before")
    @classmethod
    def _categoria_sin_tildes(cls, valor: Any) -> Any:
        return _opcion(valor, get_args(Categoria))

    @field_validator("periodicidad", mode="before")
    @classmethod
    def _periodicidad_sin_tildes(cls, valor: Any) -> Any:
        return _opcion(valor, get_args(Periodicidad))


class CostEstimationReport(BaseModel):
    # Los componentes van primero: la justificación explica unos importes que el modelo ya ha escrito
    componentes: list[ComponenteCoste] = Field(
        description="Un elemento por cada componente con coste de la propuesta, sin el equipo")
    resumen_ejecutivo: ResumenEjecutivo
    supuestos: list[str]


class EstimationResult(BaseModel):
    cost_estimation: str
    team_profiles: str



CSV_TARIFAS = Path(__file__).parent.parent / "bd" / "costes_perfil.csv"

def _clave_perfil(nombre: str) -> str:
    """'MLOps / AIOps engineer' -> 'mlops/aiops engineer': compara perfiles sin mayúsculas, tildes ni espacios."""
    return re.sub(r"\s*/\s*", "/", _normalize_text(nombre or ""))

def _perfiles_con_tarifa() -> list[str]:
    """Los perfiles de bd/costes_perfil.csv, tal cual: la lista cerrada que puede usar el agente."""
    if not CSV_TARIFAS.exists():
        return []
    with open(CSV_TARIFAS, encoding="utf-8-sig", newline="") as f:
        nombres = [(fila.get("Perfil") or "").strip() for fila in csv.DictReader(f, skipinitialspace=True)]
    return [n for n in nombres if n]

PERFILES_CON_TARIFA = _perfiles_con_tarifa()  # lista cerrada de perfiles que puede usar el agente; si no hay CSV, queda vacía


class PerfilEquipo(BaseModel):
    # El enum va solo en el esquema que ve el modelo. Python acepta cualquier nombre para no perder el
    # equipo entero por uno mal escrito: el que no tenga tarifa se marca y se avisa junto al total.
    perfil: str = Field(
        description="Nombre exacto del perfil, copiado de la primera columna de la tabla de tarifas",
        json_schema_extra={"enum": PERFILES_CON_TARIFA} if PERFILES_CON_TARIFA else None,
    )
    nivel: Literal["Junior", "Middle", "Senior"]
    horas_mes: int = Field(description="Horas de dedicación al mes en este proyecto (160 = jornada completa; menos si es parcial)")


class EquipoEstimado(BaseModel):
    perfiles: list[PerfilEquipo]
    meses_min: int = Field(description="Meses de trabajo del equipo según el plazo del cliente (escenario corto)")
    meses_max: int = Field(description="Meses de trabajo del equipo según el plazo del cliente (escenario largo)")
    funciones_sin_tarifa: list[str] = Field(
        default_factory=list,
        description="Funciones que hacen falta y no encajan en ningún perfil de la tabla de tarifas (por ejemplo, "
                    "la gestión del proyecto). No se presupuestan: se avisa de ellas junto al total")


def _eur(valor: float) -> str:
    """12345.6 -> '12.346 €'"""
    return f"{valor:,.0f} €".replace(",", ".")

def _rango(minimo: float, maximo: float) -> str:
    """'75.000 € – 90.000 €', o un solo valor si los dos coinciden ('214.400 €')."""
    return _eur(minimo) if _eur(minimo) == _eur(maximo) else f"{_eur(minimo)} – {_eur(maximo)}"

def _cargar_tarifas() -> dict[tuple[str, str], float]:
    """Tarifas de bd/costes_perfil.csv como {(perfil, nivel) en minúsculas: €/hora}."""
    tarifas: dict[tuple[str, str], float] = {}
    if not CSV_TARIFAS.exists():
        return tarifas
    with open(CSV_TARIFAS, encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f, skipinitialspace=True):
            perfil = _clave_perfil(fila.get("Perfil") or "")
            for nivel in ("Junior", "Middle", "Senior"):
                valor = (fila.get(f"{nivel} (€/h)") or "").strip()
                if perfil and valor:
                    tarifas[(perfil, nivel.lower())] = float(valor)
    return tarifas


def _calcular_equipo(equipo: EquipoEstimado, tarifas: dict[tuple[str, str], float]) -> dict:
    """Coste del equipo calculado en Python (horas × tarifa del CSV), nunca por el LLM."""
    filas, mensual, no_sumados = [], 0.0, []
    for p in equipo.perfiles:
        nombre = p.perfil.strip().replace("|", "/")  # una barra rompería la tabla
        tarifa = tarifas.get((_clave_perfil(p.perfil), p.nivel.lower()))
        if tarifa is None:
            logger.warning("Perfil sin tarifa en costes_perfil.csv: '%s' (%s); no se suma", p.perfil, p.nivel)
            filas.append(f"| {nombre} | {p.nivel} | {p.horas_mes} | sin tarifa | no se suma |")
            no_sumados.append(f"{nombre} ({p.nivel}, {p.horas_mes} h/mes)")
            continue
        coste = p.horas_mes * tarifa
        mensual += coste
        filas.append(f"| {nombre} | {p.nivel} | {p.horas_mes} | {_eur(tarifa)}/h | {_eur(coste)} |")

    meses_min, meses_max = sorted((max(equipo.meses_min, 1), max(equipo.meses_max, 1)))
    total_min, total_max = mensual * meses_min, mensual * meses_max
    meses = f"{meses_min}" if meses_min == meses_max else f"{meses_min}–{meses_max}"
    total = _eur(total_min) if meses_min == meses_max else f"{_eur(total_min)} – {_eur(total_max)}"
    lineas = [
        "| Perfil | Nivel | Horas/mes | Tarifa | Coste mensual |",
        "| --- | --- | ---: | ---: | ---: |",
        *filas,
        f"| **Total mensual** | | | | **{_eur(mensual)}** |",
        "",
        f"**Coste del equipo en {meses} meses: {total}**",
    ]
    # Funciones que el agente no pudo asignar a un perfil con tarifa: tampoco se suman
    no_sumados += [f.strip().replace("|", "/") for f in equipo.funciones_sin_tarifa if f.strip()]
    if no_sumados:
        lineas += ["", f"Sin presupuestar, porque no tienen tarifa en la tabla: {'; '.join(no_sumados)}."]
    return {"mensual": mensual, "total_min": total_min, "total_max": total_max,
            "meses_min": meses_min, "meses_max": meses_max, "no_sumados": no_sumados,
            "texto": "\n".join(lineas)}


def _encaje_presupuesto(total_min: float, total_max: float, presupuesto: int) -> tuple[str, str]:
    """Nivel ('alto', 'medio' o 'bajo') y motivo, comparando el total con el presupuesto del cliente."""
    if presupuesto <= 0:
        return "medio", "El cliente no ha indicado presupuesto, así que no se puede comprobar si el total cabe."
    if total_max <= presupuesto:
        return "alto", f"Incluso el escenario alto ({_eur(total_max)}) cabe en el presupuesto del cliente ({_eur(presupuesto)})."
    if total_min <= presupuesto:
        return "medio", (f"Solo el escenario bajo ({_eur(total_min)}) cabe en el presupuesto del cliente "
                         f"({_eur(presupuesto)}); el alto ({_eur(total_max)}) lo supera.")
    return "bajo", f"Incluso el escenario bajo ({_eur(total_min)}) supera el presupuesto del cliente ({_eur(presupuesto)})."

# Bar colors reuse the template icon-section palette
CATEGORIAS = {
    "infraestructura": ("Infraestructura cloud", "#0369a1"),       # sky
    "licencias":       ("Licencias y herramientas", "#1d4ed8"),    # blue
    "formacion":       ("Formación y onboarding", "#b45309"),      # amber
    "mantenimiento":   ("Mantenimiento y soporte", "#6d28d9"),     # purple
}
_UNIDAD = {"mensual": "€/mes", "anual": "€/año", "unico": "€, pago único"}


def _cifra(valor: float) -> str:
    """1250 -> '1.250'; los importes pequeños con céntimos conservan dos decimales: 32.5 -> '32,50'."""
    if valor < 100 and valor != int(valor):
        return f"{valor:.2f}".replace(".", ",")
    return f"{valor:,.0f}".replace(",", ".")


def _coste_en_proyecto(c: ComponenteCoste, meses: int) -> float:
    """Lo que cuesta un componente en `meses` meses: lo mensual, cada mes; lo anual, por año empezado; lo único, una vez."""
    if c.periodicidad == "mensual":
        return c.importe_eur * meses
    if c.periodicidad == "anual":
        return c.importe_eur * math.ceil(meses / 12)
    return c.importe_eur

def _sin_valorar_presupuesto(texto: str) -> str:
    """Quita de la justificación las frases que hablan del presupuesto. El encaje se calcula en Python con el
    equipo incluido, que el LLM no conoce; ministral lo valoraba igualmente aunque el prompt se lo prohíbe."""
    lineas, quitadas = [], 0
    for linea in texto.splitlines():
        frases = re.split(r"(?<=[.!?])\s+", linea)
        quedan = [f for f in frases if "presupuesto" not in _normalize_text(f)]
        quitadas += len(frases) - len(quedan)
        if quedan or not linea.strip():
            lineas.append(" ".join(quedan))
    if quitadas:
        logger.warning("Justificación de costes: se quitan %d frase(s) que valoran el presupuesto", quitadas)
    return "\n".join(lineas).strip()

def _format_cost_report(report: CostEstimationReport, infracost_raw: str = "", equipo: dict | None = None) -> str:
    r = report.resumen_ejecutivo

    justificacion = _sin_valorar_presupuesto(r.justificacion)

    # Fallo 32: el LLM da el importe de cada componente y si es mensual, anual o único; las sumas se hacen aquí, con los mismos meses que el equipo para que todo cubra el mismo periodo
    if equipo and "meses_min" in equipo:
        meses_min, meses_max = equipo["meses_min"], equipo["meses_max"]
    else:
        meses_min = meses_max = max(r.meses_proyecto, 1)
    meses = f"{meses_min}" if meses_min == meses_max else f"{meses_min}–{meses_max}"
    meses_txt = f"{meses} {'mes' if meses_max == 1 else 'meses'}"

    por_categoria = {cat: [0.0, 0.0] for cat in CATEGORIAS}
    for c in report.componentes:
        por_categoria[c.categoria][0] += _coste_en_proyecto(c, meses_min)
        por_categoria[c.categoria][1] += _coste_en_proyecto(c, meses_max)
    base_min = sum(v[0] for v in por_categoria.values())
    base_max = sum(v[1] for v in por_categoria.values())
    contingencia = base_max * max(r.contingencia_pct, 0) / 100  # solo en el escenario alto
    sin_equipo_min, sin_equipo_max = base_min, base_max + contingencia

    # Total = componentes (sin el equipo) + el equipo calculado en Python
    equipo_min = equipo["total_min"] if equipo else 0
    equipo_max = equipo["total_max"] if equipo else 0
    total_min = sin_equipo_min + equipo_min
    total_max = sin_equipo_max + equipo_max
    nivel, motivo = _encaje_presupuesto(total_min, total_max, r.presupuesto_cliente_max_eur)
    if not equipo:
        motivo += " Ojo: el total no incluye el equipo, que no se pudo calcular."

    # Lo que no está en el total se avisa al lado del total y del encaje
    avisos = []
    no_sumados = (equipo or {}).get("no_sumados") or []
    if no_sumados:
        avisos.append(f"Ojo: el total y el encaje no incluyen {escape('; '.join(no_sumados))}, porque no tienen "
                      f"tarifa en la tabla de la empresa. El coste real será mayor.")
    if not report.componentes:
        avisos.append("Ojo: el total no incluye infraestructura, licencias, formación ni soporte, porque el modelo "
                      "no ha valorado ningún componente de la propuesta.")
    aviso_html = "".join(
        f'<p style="color:#b45309;font-size:0.875rem;font-weight:600;margin:0 0 0.5rem;">{aviso}</p>' for aviso in avisos
    )

    # Semantic confidence colors, harmonized with the template warm palette
    confidence_map = {
        "bajo":  ("25%",  "#dc2626"),           # red — universally "low"
        "medio": ("60%",  "#b45309"),           # amber — matches template warm tones
        "alto":  ("100%", "var(--accent)"),     # template gold — clearly "good"
    }
    conf_pct, conf_color = confidence_map.get(nivel, ("50%", "var(--muted)"))

    # Desglose: (etiqueta, texto, valor de la barra, color), con los importes calculados arriba
    filas = [(CATEGORIAS[cat][0], _rango(vmin, vmax), vmax, CATEGORIAS[cat][1])
             for cat, (vmin, vmax) in por_categoria.items() if vmax]
    if contingencia:
        filas.append((f"Contingencia ({r.contingencia_pct} %, solo escenario alto)", _eur(contingencia),
                      contingencia, "#64748b"))  # slate
    if equipo:
        filas.append(("Desarrollo e implantación", f"Equipo: {_rango(equipo_min, equipo_max)}", equipo_max,
                      "#0f766e"))  # teal
    max_val = max((valor for _, _, valor, _ in filas), default=0) or 1

    bars_html = ""
    for label, texto, valor, color in filas:
        pct = max(round(valor / max_val * 100), 2)
        bars_html += (
            f'<div style="margin-bottom:0.875rem;">'
            f'<span style="font-size:0.78rem;color:var(--muted);">{label}</span>'
            f'<div style="background:var(--border);border-radius:4px;height:8px;overflow:hidden;margin:4px 0 2px;">'
            f'<div style="background:{color};width:{pct}%;height:100%;border-radius:4px;"></div></div>'
            f'<div style="font-size:0.72rem;color:var(--muted);opacity:0.8;">{texto}</div>'
            f'</div>'
        )
    if not bars_html:
        bars_html = '<div style="font-size:0.78rem;color:var(--muted);">Sin importes que desglosar.</div>'

    cards_html = ""
    for c in report.componentes:
        categoria, color = CATEGORIAS[c.categoria]
        en_proyecto = _rango(_coste_en_proyecto(c, meses_min), _coste_en_proyecto(c, meses_max))
        cards_html += (
            f'<div style="background:var(--surface);border:1px solid var(--border);border-top:3px solid {color};'
            f'border-radius:9px;padding:0.75rem;min-width:150px;flex:1;box-shadow:var(--shadow);">'
            f'<div style="font-size:0.75rem;color:var(--muted);margin-bottom:3px;">{escape(c.componente)}</div>'
            f'<div style="font-size:1rem;font-weight:700;color:var(--text);">{_cifra(c.importe_eur)} {_UNIDAD[c.periodicidad]}</div>'
            f'<div style="font-size:0.75rem;color:var(--text);margin-top:3px;">En {meses_txt}: {en_proyecto}</div>'
            f'<div style="font-size:0.7rem;color:var(--muted);margin-top:3px;opacity:0.75;">{escape(c.calculo)}</div>'
            f'<div style="font-size:0.7rem;color:var(--muted);opacity:0.5;">{categoria}</div>'
            f'</div>'
        )
    if not cards_html:
        cards_html = ('<p style="font-size:0.85rem;color:var(--muted);margin:0;">La propuesta no trae componentes '
                      'valorados: no hay importes de infraestructura, licencias, formación ni soporte.</p>')
    nota_precios = "" if infracost_raw else (
        '<p style="font-size:0.75rem;color:var(--muted);margin:0 0 0.5rem;">'
        'Importes estimados por el modelo, sin contrastar con Infracost.</p>'
    )

    supuestos_html = ""
    for s in report.supuestos:
        supuestos_html += (
            f'<div style="display:flex;align-items:flex-start;gap:0.5rem;padding:0.5rem 0.875rem;'
            f'background:var(--tag-bg);border-left:3px solid var(--accent);'
            f'border-radius:0 6px 6px 0;margin-bottom:0.5rem;">'
            f'<span style="color:var(--accent);font-weight:700;flex-shrink:0;">&#x2139;</span>'
            f'<span style="font-size:0.85rem;color:var(--text);">{s}</span>'
            f'</div>'
        )

    infracost_block = ""
    if infracost_raw:
        escaped = infracost_raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        infracost_block = (
            f'<div style="margin-top:1.5rem;">'
            f'<div style="font-size:0.75rem;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;'
            f'color:var(--muted);margin-bottom:0.5rem;padding-left:10px;'
            f'border-left:3px solid var(--accent);">Referencia Infracost</div>'
            f'<div style="background:#0f172a;border:1px solid var(--border-strong);border-radius:9px;'
            f'padding:1rem;overflow-x:auto;">'
            f'<pre style="color:#94a3b8;margin:0;font-size:0.78rem;white-space:pre;'
            f'font-family:\'Cascadia Code\',\'Fira Code\',\'Courier New\',monospace;line-height:1.5;">'
            f'{escaped}</pre>'
            f'</div></div>'
        )

    # Section label style matches .prose h4 from the template
    lbl = ('font-size:0.72rem;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;'
           'color:var(--muted);margin:0 0 0.875rem;padding-left:10px;border-left:3px solid var(--accent);')

    return (
        f'<div style="color:var(--text);">'

        # ── Resumen Ejecutivo ──
        f'<div style="background:var(--surface-alt);border:1px solid var(--border);'
        f'border-radius:12px;padding:1.5rem;margin-bottom:1.25rem;">'
        f'<div style="{lbl}">Resumen Ejecutivo</div>'
        f'<div style="display:flex;gap:1rem;margin-bottom:1.25rem;flex-wrap:wrap;">'
        f'<div style="flex:1;min-width:140px;background:var(--surface);border:1px solid var(--border);'
        f'border-radius:9px;padding:1.1rem;text-align:center;box-shadow:var(--shadow);">'
        f'<div style="font-size:0.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;">'
        f'{"Coste total (equipo incluido)" if equipo else "Coste estimado (sin equipo)"}</div>'
        f'<div style="font-size:1.5rem;font-weight:700;color:var(--text);">'
        f'{_rango(total_min, total_max)}</div></div>'
        f'<div style="flex:1;min-width:140px;background:var(--surface);border:1px solid var(--border);'
        f'border-radius:9px;padding:1.1rem;text-align:center;box-shadow:var(--shadow);">'
        f'<div style="font-size:0.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;">Horizonte temporal</div>'
        f'<div style="font-size:1.5rem;font-weight:700;color:var(--text);">'
        f'{meses_txt}</div></div>'
        f'</div>'
        f'<div style="margin-bottom:0.875rem;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
        f'<span style="font-size:0.82rem;color:var(--muted);">Encaje con el presupuesto</span>'
        f'<span style="font-size:0.82rem;font-weight:600;color:{conf_color};">{nivel.capitalize()}</span>'
        f'</div>'
        f'<div style="background:var(--border);border-radius:999px;height:9px;overflow:hidden;">'
        f'<div style="background:{conf_color};width:{conf_pct};height:100%;border-radius:999px;"></div>'
        f'</div></div>'
        f'{aviso_html}'
        f'<p style="color:var(--text);font-size:0.875rem;margin:0 0 0.5rem;">{motivo}</p>'
        # La justificación la escribe el LLM sin conocer el equipo: se dice a qué cifras se refiere
        f'<p style="color:var(--muted);font-size:0.875rem;margin:0;font-style:italic;">'
        f'Infraestructura, licencias, formación y soporte ({_rango(sin_equipo_min, sin_equipo_max)}, '
        f'sin el equipo){": " + justificacion if justificacion else "."}</p>'
        f'</div>'

        # ── Supuestos ──
        f'<div style="margin-bottom:1.25rem;">'
        f'<div style="{lbl}">Supuestos</div>'
        f'{supuestos_html}'
        f'</div>'

        # ── Desglose + Componentes ──
        f'<div style="display:flex;gap:1.25rem;margin-bottom:1.25rem;flex-wrap:wrap;align-items:flex-start;">'
        f'<div style="flex:1;min-width:220px;background:var(--surface-alt);border:1px solid var(--border);'
        f'border-radius:12px;padding:1.25rem;">'
        f'<div style="{lbl}">Desglose por Categoría ({meses_txt})</div>'
        f'{bars_html}'
        f'</div>'
        f'<div style="flex:2;min-width:260px;">'
        f'<div style="{lbl}">Componentes valorados</div>'
        f'{nota_precios}'
        f'<div style="display:flex;flex-wrap:wrap;gap:0.75rem;">{cards_html}</div>'
        f'</div>'
        f'</div>'

        f'{infracost_block}'
        f'</div>'
    )


def _resolve_infracost_binary() -> str | None:
    local = Path(__file__).parent.parent / "infracost.exe"
    if local.exists():
        return str(local)
    return shutil.which("infracost")


def _generate_terraform_from_proposal(proposal_context: str, model_name: str) -> str:
    """
    Usa el LLM para generar un Terraform HCL mínimo con los recursos cloud
    mencionados en la propuesta. Infracost parsea este HCL para obtener precios reales.
    """
    try:
        response = litellm.completion(
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres un experto en Terraform. Tu tarea es generar un archivo Terraform HCL mínimo "
                        "con los recursos cloud mencionados en la propuesta de solución. "
                        "Reglas estrictas:\n"
                        "- Usa valores de placeholder para campos obligatorios "
                        "(name = 'placeholder', resource_group_name = 'rg-placeholder', location = 'West Europe', etc.)\n"
                        "- No uses variables, módulos, data sources ni locals\n"
                        "- Incluye el bloque terraform{} y el bloque provider correspondiente\n"
                        "- Para azurerm: subscription_id = '00000000-0000-0000-0000-000000000000' y features {}\n"
                        "- Solo incluye recursos que tengan un tipo de recurso Terraform conocido\n"
                        "- Incluye como máximo 8-10 recursos, priorizando los más costosos y centrales\n"
                        "- Ignora servicios SaaS sin recurso Terraform (Power BI, Microsoft Teams, etc.)\n"
                        "- Devuelve ÚNICAMENTE el código HCL sin explicaciones ni bloques markdown"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Genera el Terraform para esta propuesta de solución:\n\n{proposal_context}",
                },
            ],
            temperature=0.1,
        )
        raw = response.choices[0].message.content or ""
        cleaned = re.sub(r"```(?:hcl|terraform)?\n?", "", raw).replace("```", "").strip()
        return cleaned
    except Exception as e:
        print(f"[estimation_crew] Error generando Terraform desde propuesta: {e}")
        return ""


def _run_infracost_breakdown(terraform_hcl: str, binary: str, api_key: str) -> str | None:
    """
    Escribe el HCL en un directorio temporal y ejecuta infracost breakdown.
    Retorna el texto del scan summary como string, o None si falla.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tf_path = os.path.join(tmpdir, "main.tf")
        with open(tf_path, "w", encoding="utf-8") as f:
            f.write(terraform_hcl)

        env = {**os.environ, "INFRACOST_API_KEY": api_key}
        try:
            result = subprocess.run(
                [binary, "breakdown", "--path", tmpdir, "--no-color"],
                capture_output=True,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            print("[estimation_crew] infracost breakdown timeout (120s).")
            return None
        except Exception as e:
            print(f"[estimation_crew] infracost breakdown error: {e}")
            return None

        stdout_text = result.stdout.decode(errors="replace").strip()
        stderr_text = result.stderr.decode(errors="replace").strip()

        if result.returncode != 0:
            print(f"[estimation_crew] infracost breakdown falló (rc={result.returncode}): {stderr_text[:300]}")
            return None

        if not stdout_text:
            print("[estimation_crew] infracost breakdown no produjo output.")
            return None

        print(f"[estimation_crew] Infracost scan completado:\n{stdout_text[:400]}")
        return stdout_text


def _load_team_costs_context() -> str:
    csv_path = Path(__file__).parent.parent / "bd" / "costes_perfil.csv"
    if not csv_path.exists():
        return ""
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    return "\n".join(line.strip() for line in lines if line.strip())


def run_estimation_crew(
    proposal_context: str,
    model_name: str,
    tools: list | None = None,
    problem_case: dict | None = None,
) -> EstimationResult:
    """
    Ejecuta la crew de estimación de costos y perfiles de equipo.

    Pre-fase (Python puro): genera Terraform desde la propuesta → infracost breakdown → precios reales.
    Fase CrewAI: report_writer_agent (sin tools, output_pydantic) genera CostEstimationReport.

    `proposal_context` es el output serializado de ProposalResult.as_combined_context().
    """
    agents_config = load_agents_config()

    # --- Pre-fase: obtener precios reales via Infracost subprocess ---
    api_key = os.getenv("INFRACOST_API_KEY", "")
    binary = _resolve_infracost_binary()
    infracost_prices: str | None = None

    if api_key and binary:
        terraform_hcl = _generate_terraform_from_proposal(proposal_context, model_name)
        if terraform_hcl.strip():
            infracost_prices = _run_infracost_breakdown(terraform_hcl, binary, api_key)
            if infracost_prices:
                print("[estimation_crew] Precios reales obtenidos de Infracost.")
            else:
                print("[estimation_crew] infracost breakdown no produjo precios — usando estimación del LLM.")
        else:
            print("[estimation_crew] No se pudo generar Terraform — usando estimación del LLM.")
    else:
        print("[estimation_crew] Infracost no disponible — usando estimación del LLM.")

    team_costs_table = _load_team_costs_context()

    tarifas = _cargar_tarifas()
    datos_cliente = f"{contexto_cliente(problem_case or {}, con_costes=True)}\n"

    # --- Fase CrewAI: report writer + team builder (ambos sin tools) ---
    agent_data = agents_config.get("cost_estimation_agent", {})
    report_writer_agent = Agent(
        role=agent_data.get("role"),
        goal=agent_data.get("goal"),
        backstory=agent_data.get("backstory"),
        llm=build_llm(model_name),
        tools=[],
        verbose=False,
        allow_delegation=False,
    )
    team_builder = _make_agent("team_profiles_advisor", agents_config, model_name)

    pricing_note = (
        f"Resumen de costos reales obtenido de Infracost (basado en infraestructura generada desde la propuesta):\n{infracost_prices}"
        if infracost_prices else
        "No hay precios reales disponibles: usa estimaciones razonables a partir de precios públicos, en euros."
    )

    cost_report_task = Task(
        description=(
            "A partir de la propuesta de solución y los precios indicados, valora el coste de cada componente de la propuesta. "
            f"{pricing_note}\n"
            "En componentes pon un elemento por cada servicio cloud, licencia, formación o soporte que aparezca en la "
            "propuesta, sin dejarte ninguno, cada uno con su categoría, su importe en euros (solo el número) y su "
            "periodicidad: mensual, anual o unico (si se paga una sola vez). Si no tienes el precio exacto, da tu mejor "
            "estimación y explica en calculo de dónde sale. "
            "El coste del equipo (desarrollo e implantación) se calcula aparte con las tarifas de la empresa: NO lo "
            "incluyas como componente. Los totales, el horizonte y el encaje con el presupuesto se calculan después a "
            "partir de tus importes. En meses_proyecto pon los meses del alcance que se presupuesta (el piloto, si lo "
            "hay) según el plazo del cliente. En presupuesto_cliente_max_eur pon el máximo del presupuesto que "
            "indica el caso del cliente, o 0 si no lo indica. Si incluyes una contingencia, usa un único porcentaje, "
            "el mismo en todo el informe: ponlo en contingencia_pct (0 si no hay) y no lo añadas como componente. "
            "En justificacion explica solo de dónde salen los importes y qué los haría variar; no digas si caben en "
            "el presupuesto del cliente, porque el total con el equipo se calcula después.\n"
            "Para los supuestos: si te falta información, indícalo como "
            "'Por determinar, requiere [dato X]' o 'Se asume [dato X]' — nunca inventes datos críticos.\n\n"
            f"{datos_cliente}"
            "Propuesta de solución:\n"
            f"{proposal_context}"
        ),
        expected_output="Un elemento por componente de la propuesta, con su importe en euros y su periodicidad, más el resumen y los supuestos.",
        output_pydantic=CostEstimationReport,
        agent=report_writer_agent,
    )

    # Fallo 37: los perfiles se nombran como en la tabla de tarifas desde el principio, para que el texto y el coste coincidan
    catalogo_perfiles = (
        "Nombra cada perfil con uno de estos, tal cual, porque son los que tienen tarifa en la empresa: "
        f"{', '.join(PERFILES_CON_TARIFA)}. Es un catálogo para elegir, no una lista que haya que cubrir. "
        "Si hace falta una función que no encaja en ninguno (por ejemplo, la gestión del proyecto), dilo aparte "
        "como función sin perfil en la tabla de tarifas; no inventes un perfil para ella. "
    ) if PERFILES_CON_TARIFA else ""

    team_building_task = Task(
        description=(
            "A partir de la propuesta de solución y de los datos del cliente, identifica los perfiles profesionales necesarios "
            "para implementar lo que pide el cliente en el plazo que indica (el piloto, si lo hay), no la solución completa a largo plazo. "
            "Incluye solo los perfiles imprescindibles para ese alcance y, para cada uno, sus responsabilidades clave, las "
            "habilidades requeridas y si su dedicación es completa o parcial. "
            f"{catalogo_perfiles}"
            "Ten en cuenta el tipo de servicio que pide el cliente: si quiere que Inetum lo implemente, los perfiles son del "
            "equipo de Inetum, y el equipo interno del cliente participa y se forma; dilo así. Ten en cuenta también el "
            "equipo técnico que ya tiene el cliente y lo que sabe hacer. "
            "No des cifras de coste: el coste del equipo se calcula aparte con las tarifas de la empresa.\n\n"
            f"{datos_cliente}"
            "Esta es la solución propuesta sobre la que debes identificar los perfiles profesionales necesarios:\n"
            f"{proposal_context}"
        ),
        expected_output="Los perfiles necesarios para el alcance pedido, con las responsabilidades, las habilidades y la dedicación (completa o parcial) de cada uno.",
        agent=team_builder,
    )

    team_cost_task = Task(
        description=(
            "A partir de los perfiles de equipo identificados en la tarea anterior, define el equipo que se va a presupuestar: "
            "los mismos perfiles, sin añadir otros. "
            "Las funciones que la tarea anterior dejó sin perfil en la tabla de tarifas van en funciones_sin_tarifa, no en perfiles. "
            "Para cada perfil indica: su nombre exacto en la tabla de tarifas (primera columna), "
            "el nivel (Junior/Middle/Senior) y las horas al mes. 160 horas al mes es jornada completa: úsala solo para "
            "quien trabaje a tiempo completo en este alcance, y a los perfiles con dedicación parcial ponles las horas que les correspondan. "
            "Indica también cuántos meses trabajará el equipo según el plazo del cliente (meses_min y meses_max). "
            "No calcules costes: se calculan aparte con la tabla.\n\n"
            f"{datos_cliente}"
            f"Tarifas de referencia (€/hora):\n{team_costs_table}"
        ),
        expected_output="Perfiles del equipo con su nivel y horas al mes, y duración del trabajo en meses (mínima y máxima).",
        output_pydantic=EquipoEstimado,
        context=[team_building_task],
        agent=team_builder,
    ) if team_costs_table else None

    crew_tasks: list[Task] = [cost_report_task, team_building_task]
    if team_cost_task:
        crew_tasks.append(team_cost_task)

    crew = Crew(
        agents=[report_writer_agent, team_builder],
        tasks=crew_tasks,
        process=Process.sequential,
        cache=False,
    )
    result = crew.kickoff()

    # Coste del equipo: el LLM elige perfiles, nivel, horas y meses; las cuentas se hacen aquí
    equipo_pydantic = getattr(getattr(team_cost_task, "output", None), "pydantic", None) if team_cost_task else None
    equipo = _calcular_equipo(equipo_pydantic, tarifas) if isinstance(equipo_pydantic, EquipoEstimado) else None
    if team_cost_task and equipo is None:
        logger.warning("No se pudo leer el equipo estimado: el coste total no incluirá el equipo.")

    # Extraer output de costos: preferir pydantic validado, fallback a raw
    report_pydantic = getattr(getattr(cost_report_task, "output", None), "pydantic", None)
    if isinstance(report_pydantic, CostEstimationReport):
        cost_estimation = _format_cost_report(report_pydantic, infracost_prices or "", equipo)
    else:
        raw_obj = getattr(cost_report_task, "output", None)
        cost_estimation = getattr(raw_obj, "raw", "") or str(raw_obj or "")

    team_obj = getattr(team_building_task, "output", None)
    team_profiles = getattr(team_obj, "raw", "") or str(team_obj or "")

    if equipo:
        team_profiles = team_profiles + "\n\n---\n\n## Estimación de Coste del Equipo\n\n" + equipo["texto"]

    if not (cost_estimation and team_profiles):
        raise RuntimeError(getattr(result, "raw", "Error: estimation crew produjo outputs incompletos."))

    return EstimationResult(
        cost_estimation=cost_estimation,
        team_profiles=team_profiles,
    )
