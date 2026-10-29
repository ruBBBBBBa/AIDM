import csv
import logging

import os
import re
import shutil
import subprocess
import tempfile
from html import escape
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
import json
import textwrap

import litellm
from crewai import Agent, Crew, Process, Task
from pydantic import BaseModel, Field, PrivateAttr, field_validator, ValidationError

from utils import (_make_agent, _normalize_text, build_llm, contexto_cliente, es_valor_de_relleno,
                   load_agents_config, md_to_html)

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


# C9: con un cliente que habla inglés, el modelo puede traducir también las opciones; se aceptan igual
_OPCIONES_EN = {"infrastructure": "infraestructura", "licen": "licencias", "training": "formacion",
                "maintenance": "mantenimiento", "support": "mantenimiento", "soporte": "mantenimiento",
                "monthly": "mensual", "annual": "anual", "yearly": "anual", "one": "unico", "once": "unico"}


def _opcion(valor: Any, opciones: tuple[str, ...]) -> Any:
    """'Único' → 'unico', 'Formación y onboarding' → 'formacion', 'Training' → 'formacion': la opción que empieza
    igual, sin tildes ni mayúsculas, o su equivalente en inglés."""
    if not isinstance(valor, str):
        return valor
    texto = _normalize_text(valor)
    return next((o for o in opciones if texto.startswith(o[:5])), None) or next(
        (o for prefijo, o in _OPCIONES_EN.items() if texto.startswith(prefijo) and o in opciones), valor)


class ComponenteCoste(BaseModel):
    # Fallo 41 (C15): el modelo da el precio unitario y las unidades; el importe lo multiplica Python, para que no vuelva a salir «6.000 €/año» con un detalle que suma 13.200 €
    componente: str = Field(
        description="Componente de la propuesta que se valora: un servicio cloud, una licencia, una formación o el "
                    "soporte. Si tiene partes con precios distintos (cómputo y almacenamiento, por ejemplo), un "
                    "elemento por parte")
    # Fallo 53: si el componente está en la tabla de precios, el precio, la unidad de cobro y lo gratuito salen de ella
    tarifa: str = Field(
        default="", description="id de la fila de la tabla de precios que corresponde a este componente, copiado tal "
                                "cual; vacío si ninguna fila le corresponde")
    categoria: Categoria
    precio_unitario_eur: float = Field(ge=0, description="Precio de una unidad en euros: solo el número. Con tarifa, 0: "
                                                         "lo pone el programa")
    unidades: float = Field(ge=0, description="Cuántas unidades se pagan en cada periodo: horas, GB, licencias, talleres… "
                                              "Con tarifa, la cantidad de un mes en la unidad de su fila, sin dividir "
                                              "por el paquete (144 millones de eventos son 144000000)")
    unidad: str = Field(description="Qué es una unidad, en singular: hora, GB, licencia, taller… Con tarifa, la de su fila")
    periodicidad: Periodicidad = Field(
        description="mensual o anual si esas unidades se pagan cada mes o cada año; unico si se pagan una sola vez. "
                    "Con tarifa, mensual")
    referencia: str = Field(description="De dónde salen las unidades (el uso que estimas) y, sin tarifa, el precio "
                                        "unitario: precio público, tarifa de mercado…")
    _cantidad: float | None = PrivateAttr(default=None)  # fallo 53: el uso que dio el modelo, antes de pasarlo a paquetes

    @property
    def importe_eur(self) -> float:
        """Lo que se paga en cada periodo."""
        return self.precio_unitario_eur * self.unidades

    @field_validator("precio_unitario_eur", "unidades", mode="before")
    @classmethod
    def _numerico(cls, valor: Any) -> Any:
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
    horas_mes: int = Field(description="Horas al mes en el desarrollo (160 = jornada completa; menos si es parcial; "
                                       "0 si solo participa en la implantación)")
    # Fallo 43: el desarrollo y la implantación se presupuestan por separado
    horas_mes_implantacion: int = Field(
        default=0, description="Horas al mes en la implantación (puesta en producción y acompañamiento); 0 si no participa")

class FuncionSinTarifa(BaseModel):
    funcion: str = Field(description="Trabajo de una persona que no encaja en ningún perfil de la tabla de tarifas "
                                     "(por ejemplo, la gestión del proyecto)")
    horas_mes: int = Field(default=0, description="Horas al mes que necesita en el desarrollo")
    # Fallo 55: como los perfiles, con las horas de la implantación aparte; antes el modelo partía la función en dos
    horas_mes_implantacion: int = Field(default=0, description="Horas al mes en la implantación; 0 si no participa")


def _funcion_y_fase(nombre: str) -> tuple[str, str | None]:
    """Fallo 55: 'Gestión del Proyecto (fase implantación)' → ('Gestión del Proyecto', 'implantacion'). Sin fase en el
    nombre, (nombre, None)."""
    m = re.search(r"\s*(?:\(([^)]*)\)|\s[-–—:]\s*([^-–—:()]+))\s*$", nombre)
    if m:
        fase = _normalize_text(m.group(1) or m.group(2) or "")
        if any(p in fase for p in ("implant", "puesta en produccion", "deploy", "rollout", "go-live")):
            return nombre[:m.start()].strip(), "implantacion"
        if any(p in fase for p in ("desarroll", "construccion", "build", "development")):
            return nombre[:m.start()].strip(), "desarrollo"
    return nombre.strip(), None


class EquipoEstimado(BaseModel):
    perfiles: list[PerfilEquipo]

    meses_min: int = Field(description="Meses de trabajo del equipo según el plazo del cliente, implantación incluida "
                                       "(escenario corto)")
    meses_max: int = Field(description="Meses de trabajo del equipo según el plazo del cliente, implantación incluida "
                                       "(escenario largo)")
    meses_implantacion: int = Field(
        default=0, description="Cuántos de esos meses, al final del plazo, son de implantación: puesta en producción y "
                               "acompañamiento hasta que el cliente use la solución. El resto son de desarrollo")
    
    funciones_sin_tarifa: list[FuncionSinTarifa] = Field(
        default_factory=list,
        description="Trabajo de personas que hace falta y no encaja en ningún perfil de la tabla de tarifas, con sus "
                    "horas al mes. No van aquí la formación, las licencias ni los servicios, que se valoran en los "
                    "costes. No se presupuestan: se avisa de ellas junto al total")

    @field_validator("funciones_sin_tarifa", mode="before")
    @classmethod
    def _funciones_sin_horas(cls, valor: Any) -> Any:
        """Un modelo que mande solo el nombre de la función, como antes del fallo 42, no pierde el equipo entero."""
        if isinstance(valor, list):
            return [{"funcion": v, "horas_mes": 0} if isinstance(v, str) else v for v in valor]
        return valor
    
    @field_validator("funciones_sin_tarifa", mode="after")
    @classmethod
    def _unir_fases(cls, funciones: list[FuncionSinTarifa]) -> list[FuncionSinTarifa]:
        """Fallo 55: «Gestión del Proyecto» (160 h/mes) y «Gestión del Proyecto (fase implantación)» (80 h/mes) son una
        sola función con 160 h/mes en el desarrollo y 80 en la implantación. Antes se sumaban las dos todos los meses."""
        unidas: dict[str, FuncionSinTarifa] = {}
        for f in funciones:
            nombre, fase = _funcion_y_fase(f.funcion)
            union = unidas.setdefault(_normalize_text(nombre), FuncionSinTarifa(funcion=nombre))
            if fase == "implantacion":
                horas_dev, horas_impl = 0, max(f.horas_mes, f.horas_mes_implantacion)
            else:
                horas_dev, horas_impl = f.horas_mes, f.horas_mes_implantacion
            # Si una fase sale dos veces, se queda la cifra mayor: sumarlas contaría la misma persona dos veces
            union.horas_mes = max(union.horas_mes, horas_dev)
            union.horas_mes_implantacion = max(union.horas_mes_implantacion, horas_impl)
        return list(unidas.values())

def _eur(valor: float) -> str:
    """12345.6 -> '12.346 €'"""
    return f"{valor:,.0f} €".replace(",", ".")

def _rango(minimo: float, maximo: float) -> str:
    """'75.000 € – 90.000 €', o un solo valor si los dos coinciden ('214.400 €')."""
    return _eur(minimo) if _eur(minimo) == _eur(maximo) else f"{_eur(minimo)} – {_eur(maximo)}"

def _lista(nombres: list[str]) -> str:
    """['A'] -> 'A'; ['A', 'B', 'C'] -> 'A, B y C'."""
    return nombres[0] if len(nombres) == 1 else f"{', '.join(nombres[:-1])} y {nombres[-1]}"

def _meses_txt(minimo: int, maximo: int) -> str:
    """(1, 1) -> '1 mes'; (2, 3) -> '2–3 meses'."""
    cuantos = f"{minimo}" if minimo == maximo else f"{minimo}–{maximo}"
    return f"{cuantos} {'mes' if maximo == 1 else 'meses'}"

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
    """Coste del equipo calculado en Python (horas × tarifa del CSV), nunca por el LLM.

    Fallo 43: si el equipo trae fase de implantación (sus meses y horas de algún perfil), el desarrollo y la
    implantación se calculan por separado. La implantación son los últimos meses del plazo; el desarrollo, el
    resto, y es el que cambia entre el escenario corto y el largo. Sin implantación, todo es como antes.
    """
    meses_min, meses_max = sorted((max(equipo.meses_min, 1), max(equipo.meses_max, 1)))
    meses_impl = min(max(equipo.meses_implantacion, 0), meses_min)
    con_fases = meses_impl > 0 and any(p.horas_mes_implantacion > 0 for p in equipo.perfiles)
    if meses_impl and not con_fases:
        logger.warning("El equipo trae %d mes(es) de implantación pero ningún perfil con horas en ella: "
                       "se cuenta todo como desarrollo", meses_impl)
    if not con_fases:
        meses_impl = 0

    # Fallo 49: el desarrollo dura al menos un mes. El 27 oct (Iberdrola), el equipo salió con 6–18 meses y 6 de
    # implantación, porque mezcló el piloto y el despliegue completo, y la tabla decía «Desarrollo en 0–12 meses: 0 €»
    if con_fases and meses_impl >= meses_min:
        if meses_min > 1:
            logger.warning("La implantación (%d meses) ocupa todo el plazo corto (%d meses): se deja en %d para que "
                           "el desarrollo dure al menos un mes", meses_impl, meses_min, meses_min - 1)
            meses_impl = meses_min - 1
        else:
            logger.warning("Un plazo de 1 mes no da para separar desarrollo e implantación: se cuenta todo como "
                           "desarrollo")
            con_fases, meses_impl = False, 0

    dev_min, dev_max = meses_min - meses_impl, meses_max - meses_impl

    filas, no_sumados = [], []
    mensual_dev = mensual_impl = 0.0
    falta_dev = falta_impl = 0  # horas al mes sin tarifa, por fase
    for p in equipo.perfiles:
        nombre = p.perfil.strip().replace("|", "/")  # una barra rompería la tabla
        tarifa = tarifas.get((_clave_perfil(p.perfil), p.nivel.lower()))
        h_impl = max(p.horas_mes_implantacion, 0) if con_fases else 0
        if tarifa is None:
            logger.warning("Perfil sin tarifa en costes_perfil.csv: '%s' (%s); no se suma", p.perfil, p.nivel)
            filas.append(f"| {nombre} | {p.nivel} | sin tarifa | {p.horas_mes} | {h_impl} |" if con_fases
                         else f"| {nombre} | {p.nivel} | {p.horas_mes} | sin tarifa | no se suma |")
            no_sumados.append(f"{nombre} ({p.nivel}, {p.horas_mes} h/mes"
                              + (f" y {h_impl} h/mes en implantación)" if h_impl else ")"))
            falta_dev += max(p.horas_mes, 0)
            falta_impl += h_impl
            continue
        mensual_dev += p.horas_mes * tarifa
        mensual_impl += h_impl * tarifa
        filas.append(f"| {nombre} | {p.nivel} | {_eur(tarifa)}/h | {p.horas_mes} | {h_impl} |" if con_fases
                     else f"| {nombre} | {p.nivel} | {p.horas_mes} | {_eur(tarifa)}/h | {_eur(p.horas_mes * tarifa)} |")

    desarrollo_min, desarrollo_max = mensual_dev * dev_min, mensual_dev * dev_max
    implantacion = mensual_impl * meses_impl
    total_min, total_max = desarrollo_min + implantacion, desarrollo_max + implantacion
    if con_fases:
        lineas = [
            "| Perfil | Nivel | Tarifa | Desarrollo (h/mes) | Implantación (h/mes) |",
            "| --- | --- | ---: | ---: | ---: |",
            *filas,
            f"| **Coste mensual** | | | **{_eur(mensual_dev)}** | **{_eur(mensual_impl)}** |",
            "",
            f"**Desarrollo en {_meses_txt(dev_min, dev_max)}: {_rango(desarrollo_min, desarrollo_max)}**",
            "",
            f"**Implantación en {_meses_txt(meses_impl, meses_impl)}: {_eur(implantacion)}**",
            "",
            f"**Coste del equipo en {_meses_txt(meses_min, meses_max)}: {_rango(total_min, total_max)}**",
        ]
    else:
        meses = f"{meses_min}" if meses_min == meses_max else f"{meses_min}–{meses_max}"
        lineas = [
            "| Perfil | Nivel | Horas/mes | Tarifa | Coste mensual |",
            "| --- | --- | ---: | ---: | ---: |",
            *filas,
            f"| **Total mensual** | | | | **{_eur(mensual_dev)}** |",
            "",
            f"**Coste del equipo en {meses} meses: {_rango(total_min, total_max)}**",
        ]
    # Funciones que el agente no pudo asignar a un perfil con tarifa: tampoco se suman
    # Fallo 55: cada una con sus horas en el desarrollo y en la implantación, como los perfiles
    for f in equipo.funciones_sin_tarifa:
        nombre = f.funcion.strip().replace("|", "/")
        if not nombre:
            continue
        h_dev, h_impl = max(f.horas_mes, 0), max(f.horas_mes_implantacion, 0)
        if not con_fases:  # sin fase de implantación, todos los meses son de desarrollo
            h_dev, h_impl = h_dev or h_impl, 0
        if h_dev and h_impl:
            no_sumados.append(f"{nombre} ({h_dev} h/mes y {h_impl} h/mes en implantación)")
        elif h_dev or h_impl:
            no_sumados.append(f"{nombre} ({h_dev} h/mes)" if h_dev else f"{nombre} ({h_impl} h/mes en implantación)")
        else:
            no_sumados.append(nombre)
        falta_dev += h_dev
        falta_impl += h_impl
    if no_sumados:
        lineas += ["", f"Sin presupuestar, porque no tienen tarifa en la tabla: {'; '.join(no_sumados)}."]
    # Fallo 42 (C16): para el encaje, un mínimo por lo que falta, a la tarifa más baja de la tabla
    tarifa_minima = min(tarifas.values(), default=0.0)
    falta_min = tarifa_minima * (falta_dev * dev_min + falta_impl * meses_impl)
    falta_max = tarifa_minima * (falta_dev * dev_max + falta_impl * meses_impl)
    return {"mensual": mensual_dev, "total_min": total_min, "total_max": total_max,
            "meses_min": meses_min, "meses_max": meses_max, "no_sumados": no_sumados,
            "meses_implantacion": meses_impl, "desarrollo_min": desarrollo_min, "desarrollo_max": desarrollo_max,
            "implantacion": implantacion,
            "horas_sin_tarifa": falta_dev, "horas_sin_tarifa_impl": falta_impl, "tarifa_minima": tarifa_minima,
            "falta_min": falta_min, "falta_max": falta_max,
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
    """1250 -> '1.250'; 32.5 -> '32,50'; los precios de céntimos conservan sus decimales: 0.025 -> '0,025'."""
    if valor == int(valor) or valor >= 100:
        return f"{valor:,.0f}".replace(",", ".")
    if valor < 1:  # fallo 51: 0,00001 salía como «0,»
        return f"{valor:.8f}".rstrip("0").replace(".", ",")
    return f"{valor:.2f}".replace(".", ",")


def _coste_en_proyecto(c: ComponenteCoste, meses: int) -> float:
    """Lo que cuesta un componente en `meses` meses: lo mensual, cada mes; lo anual, la parte proporcional de esos
    meses; lo único, una vez."""
    if c.periodicidad == "mensual":
        return c.importe_eur * meses
    if c.periodicidad == "anual":
        return c.importe_eur * meses / 12
    return c.importe_eur

# Fallo 46: un componente que cada mes se lleva más de esta parte del presupuesto suele tener mal la unidad. En el log
# del 26 oct (Iberdrola, 600.000 €), IoT Hub salía a 0,008 €/mensaje × 10 millones = 80.000 €/mes, y en otro intento a
# 8 €/mensaje × 2 millones = 16 M€/mes. Se marca y se avisa, pero se sigue sumando
'''PARTE_MAXIMA_AL_MES = 0.10'''
# Fallo 51: del 10 % al 5 %. El 28 oct, Event Hubs salía a 45.000 €/mes (0,0001 €/mensaje × 450 millones: el precio es
# por millón), el 7,5 % del presupuesto cada mes, y no se marcaba
PARTE_MAXIMA_AL_MES = 0.05


def _coste_al_mes(c: ComponenteCoste, meses: int) -> float:
    """Lo mensual, tal cual; lo anual, entre 12; lo que se paga una vez, repartido entre los meses del proyecto."""
    if c.periodicidad == "mensual":
        return c.importe_eur
    if c.periodicidad == "anual":
        return c.importe_eur / 12
    return c.importe_eur / max(meses, 1)

# Fallo 53: tabla de precios de los servicios cloud. El modelo elige la fila y da la cantidad de un mes en la unidad de
# la fila (eventos, horas, GB…); el precio, el paquete en que se cobra («millón de eventos») y lo gratuito salen de
# aquí. El 28 oct, Event Hubs salió a 0,3 €/mensaje × 720 millones = 216 M€/mes porque el precio era por millón.
# Los precios se actualizan con bd/actualizar_precios.py (Azure Retail Prices API, en euros y sin clave).
CSV_PRECIOS = Path(__file__).parent.parent / "bd" / "precios_cloud.csv"


def _cargar_precios() -> dict[str, dict]:
    """Filas de bd/precios_cloud.csv por id. Una fila sin números válidos se salta y se avisa en el log."""
    precios: dict[str, dict] = {}
    if not CSV_PRECIOS.exists():
        return precios
    with open(CSV_PRECIOS, encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f, delimiter=";"):
            fila = {k.strip(): (v or "").strip() for k, v in fila.items() if k}
            try:
                fila.update(por_cada=float(fila["por_cada"]), precio_eur=float(fila["precio_eur"]),
                            gratis_al_mes=float(fila.get("gratis_al_mes") or 0))
            except (KeyError, ValueError):
                logger.warning("Fila de precios_cloud.csv sin números válidos: %s", fila.get("id"))
                continue
            if fila.get("id") and fila["por_cada"] > 0:
                precios[fila["id"].lower()] = fila
    return precios


PRECIOS_CLOUD = _cargar_precios()

# La nube de cada fila de la tabla y la del cliente, con un mismo nombre
_PROVEEDORES = {"azure": "azure", "microsoft": "azure", "aws": "aws", "amazon": "aws", "google": "gcp", "gcp": "gcp",
                "oracle": "oracle", "ibm": "ibm"}


def _valor_caso(problem_case: dict | None, campo: str) -> str:
    """El valor de un campo del caso como texto; vacío si no hay dato real."""
    valor = (problem_case or {}).get(campo)
    valor = valor.get("value") if isinstance(valor, dict) else valor
    if isinstance(valor, list):
        valor = ", ".join(str(v) for v in valor)
    return "" if not isinstance(valor, str) or es_valor_de_relleno(valor) else valor


def _tabla_precios(problem_case: dict | None, arquitectura: str) -> str:
    """Fallo 53: las filas de la tabla que puede usar el modelo, en texto: las de los servicios que nombra la
    arquitectura (columna «buscar») y, si se sabe la nube del cliente, solo las de esa nube. Así el prompt no empuja
    servicios que la propuesta no trae (fallo 34). Vacío si no queda ninguna."""
    nube = _normalize_text(_valor_caso(problem_case, "cloud_provider"))
    del_cliente = {p for clave, p in _PROVEEDORES.items() if clave in nube}
    texto = _normalize_text(arquitectura or "")
    filas = [f for f in PRECIOS_CLOUD.values()
             if (not del_cliente or _PROVEEDORES.get(_normalize_text(f.get("proveedor", ""))) in del_cliente)
             and any(b and _normalize_text(b) in texto for b in f.get("buscar", "").split("|"))]
    if not filas:
        return ""
    lineas = ["id | servicio | concepto | unidades en | gratis al mes"]
    lineas += [f"{f['id']} | {f['servicio']} | {f['concepto']} | {f['unidad']} | "
               f"{_cifra(f['gratis_al_mes']) if f['gratis_al_mes'] else '—'}" for f in filas]
    return "\n".join(lineas)


def _fecha(texto: str) -> str:
    """'2026-10-29' -> '29/10/2026'; lo demás, tal cual."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", texto or "")
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else (texto or "")


def _con_precio_de_tabla(c: ComponenteCoste) -> ComponenteCoste:
    """Fallo 53: si el componente trae una fila de la tabla, su precio es el de la tabla. El modelo da la cantidad de
    un mes en la unidad de la fila (144.000.000 eventos); aquí se quita lo gratuito y se pasa al paquete en que se
    cobra (144 millones de eventos a 0,0246 €). Sin fila, o con un id que no está en la tabla, se queda como lo dio
    el modelo, con `tarifa` vacía."""
    if not c.tarifa:
        return c
    fila = PRECIOS_CLOUD.get(c.tarifa.strip().lower())
    if fila is None:
        logger.warning("Tarifa desconocida en costes: '%s' (%s); se usa el precio del modelo", c.tarifa, c.componente)
        return c.model_copy(update={"tarifa": ""})
    cantidad = c.unidades
    gratis = fila["gratis_al_mes"]
    uso = f"Uso estimado al mes: {_cifra(cantidad)} (unidad: {fila['unidad']})" + (
        f", de los que {_cifra(gratis)} son gratis" if gratis else "")
    referencia = (f"{c.referencia.rstrip('. ')}. {uso}. Precio de la tabla: {_cifra(fila['precio_eur'])} € por "
                  f"{fila['cobro'] or fila['unidad']} ({fila.get('fuente') or 'tabla de precios'}, "
                  f"{_fecha(fila.get('fecha', ''))}).")
    nuevo = c.model_copy(update={
        "tarifa": fila["id"], "precio_unitario_eur": fila["precio_eur"],
        "unidades": max(cantidad - gratis, 0) / fila["por_cada"], "unidad": fila["cobro"] or fila["unidad"],
        "periodicidad": "mensual", "referencia": referencia})
    nuevo._cantidad = cantidad
    return nuevo

# Fallo 45 (C6): un componente a 0 € solo cuadra si el cliente ya lo tiene; lo demás se avisa
_YA_LO_TIENE = ("ya lo tiene", "ya la tiene", "ya los tiene", "ya las tiene", "ya adquirid", "ya dispone",
                "ya cuenta con", "ya contratad", "ya pagad", "existente")

# Fallo 54: dónde dicen los datos del cliente lo que ya usa
_CAMPOS_TECNOLOGIA = ("stack_tecnologico_actual", "madurez_analitica_actual", "herramienta_de_visualizacion")
# Nombres de nube que no bastan para decir que el cliente ya tiene un servicio concreto («Azure Event Hubs» no lo
# tiene por usar Azure)
_SOLO_NUBE = {"azure", "microsoft azure", "microsoft", "aws", "amazon web services", "google cloud", "gcp", "oracle",
              "oracle cloud", "ibm", "ibm cloud", "cloud", "nube", "cloud publica"}
# Trozos de los datos del cliente que no son el nombre de un producto
_GENERICOS = {"herramientas", "licencias", "datos", "informes", "proveedores", "proveedores externos", "bases de datos",
              "a nivel corporativo", "excel"}


def _tecnologias_cliente(problem_case: dict | None) -> list[str] | None:
    """Fallo 54: los nombres de lo que el cliente ya usa, sacados de sus datos. 'Azure (Data Lake Storage, Databricks),
    Power BI Premium' → ['data lake storage', 'databricks', 'power bi premium']; el nombre de la nube sola no cuenta.
    None si el caso no trae esos datos: entonces no se puede comprobar."""
    textos = [_valor_caso(problem_case, campo) for campo in _CAMPOS_TECNOLOGIA]
    if not any(textos):
        return None
    trozos = {t.strip() for t in re.split(r"[,;:()\[\]·]|\s+(?:y|e|and|como|con)\s+", _normalize_text(" , ".join(textos)))}
    # Un nombre de producto tiene pocas palabras: las frases largas («madurez baja en alumbrado público») no cuentan
    return sorted(t for t in trozos if len(t) >= 4 and len(t.split()) <= 4 and t not in _SOLO_NUBE | _GENERICOS)

def _ya_lo_tiene(c: ComponenteCoste, tecnologias: list[str] | None = None) -> bool:
    """Fallo 45: la referencia dice que el cliente ya lo tiene. Fallo 54: y, si hay datos del cliente, su nombre está
    entre lo que ya usa. El 28 oct, el modelo escribió «Ya lo tiene el cliente» en Event Hubs, Purview y Azure Monitor
    para justificar un nivel gratuito, y ninguno estaba en el stack de Iberdrola."""
    texto = _normalize_text(f"{c.componente} {c.referencia}")
    if not any(frase in texto for frase in _YA_LO_TIENE):
        return False
    if tecnologias is None:  # sin datos del cliente no se puede comprobar: vale lo que dice el modelo
        return True
    nombre = _normalize_text(c.componente)
    return any(t in nombre for t in tecnologias)

# Fallo 57: un importe (450.000 €, 115K€, €500, $20) y las palabras de un total propio
_IMPORTE = re.compile(r"\d[\d.,]*\s*(?:k|m|mil|millones?)?\s*(?:€|eur\b|euros?\b|\$)|[€$]\s*\d", re.IGNORECASE)
_TOTAL = re.compile(r"\b(?:total(?:es)?|en conjunto|suman?|asciende[n]?)\b")


def _valora_o_suma(frase: str) -> bool:
    """La frase habla del presupuesto, o da un importe con un total propio o con un «límite» (fallo 57)."""
    normalizada = _normalize_text(frase)
    if "presupuesto" in normalizada:
        return True
    return bool(_IMPORTE.search(frase)) and (bool(_TOTAL.search(normalizada)) or "limite" in normalizada)


def _sin_valorar_presupuesto(texto: str) -> str:
    """Quita de la justificación las frases que hablan del presupuesto. El encaje se calcula en Python con el
    equipo incluido, que el LLM no conoce; ministral lo valoraba igualmente aunque el prompt se lo prohíbe.
    Fallo 57: también las que dan un total propio o valoran un «límite». El 29 oct, ministral escribió «El coste total
    estimado (sin equipo) es de ~450.000€ para 6 meses (incluyendo contingencia), dentro del límite de 600.000€» con
    41.000–45.000 € en las tarjetas; el 28 oct, «El total estimado para infraestructura es de ~115.000€/año»."""
    lineas, quitadas = [], 0
    for linea in texto.splitlines():
        frases = re.split(r"(?<=[.!?])\s+", linea)
        quedan = [f for f in frases if not _valora_o_suma(f)]
        quitadas += len(frases) - len(quedan)
        if quedan or not linea.strip():
            lineas.append(" ".join(quedan))
    if quitadas:
        logger.warning("Justificación de costes: se quitan %d frase(s) que valoran el presupuesto o dan un total propio",
                       quitadas)
    return "\n".join(lineas).strip()

def _format_cost_report(report: CostEstimationReport, infracost_raw: str = "", equipo: dict | None = None,
                        problem_case: dict | None = None) -> str:
    r = report.resumen_ejecutivo
    # Fallo 53: los componentes que están en la tabla de precios llevan su precio; el resto, el del modelo
    componentes = [_con_precio_de_tabla(c) for c in report.componentes]
    # Fallo 54: lo que el cliente ya usa, para comprobar los «ya lo tiene»
    tecnologias = _tecnologias_cliente(problem_case)

    # Fallo 51 (C12): el modelo la escribe en markdown y con sangría; se quita la sangría y se convierte a HTML
    justificacion = _sin_valorar_presupuesto(textwrap.dedent(r.justificacion).strip())
    justificacion_html = (f'<div style="color:var(--muted);font-size:0.875rem;">{md_to_html(justificacion)}</div>'
                          if justificacion else "")

    # Fallo 32: el LLM da el importe de cada componente y si es mensual, anual o único; las sumas se hacen aquí, con los mismos meses que el equipo para que todo cubra el mismo periodo
    if equipo and "meses_min" in equipo:
          meses_min, meses_max = equipo["meses_min"], equipo["meses_max"]
    else:
          meses_min = meses_max = max(r.meses_proyecto, 1)
    meses = f"{meses_min}" if meses_min == meses_max else f"{meses_min}–{meses_max}"
    meses_txt = f"{meses} {'mes' if meses_max == 1 else 'meses'}"

    por_categoria = {cat: [0.0, 0.0] for cat in CATEGORIAS}
    for c in componentes:
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

    # Fallo 42 (C16): lo que no tiene tarifa no está en el total, pero el encaje cuenta un mínimo por ello
    # (sus horas a la tarifa más baja de la tabla) y se marca como provisional
    equipo_dict = equipo or {}
    no_sumados = equipo_dict.get("no_sumados") or []
    falta_min, falta_max = equipo_dict.get("falta_min", 0), equipo_dict.get("falta_max", 0)
    presupuesto = r.presupuesto_cliente_max_eur
    nivel, motivo = _encaje_presupuesto(total_min + falta_min, total_max + falta_max, presupuesto)
    if falta_max and presupuesto > 0:
        # Fallo 55: con horas en la implantación, se dicen las de cada fase
        horas_dev, horas_impl = equipo_dict.get("horas_sin_tarifa", 0), equipo_dict.get("horas_sin_tarifa_impl", 0)
        horas = (f"{horas_dev} h/mes en el desarrollo y {horas_impl} h/mes en la implantación," if horas_dev and horas_impl
                 else f"{horas_impl} h/mes en la implantación," if horas_impl else f"{horas_dev} h/mes")
        motivo = (f"Contando al menos {_rango(falta_min, falta_max)} por lo que no tiene tarifa "
                  f"({horas} a {_cifra(equipo_dict['tarifa_minima'])} €/h, la tarifa "
                  f"más baja de la tabla), " + motivo[0].lower() + motivo[1:])
    etiqueta_nivel = nivel.capitalize() + (" (provisional)" if no_sumados else "")
    if not equipo:
        motivo += " Ojo: el total no incluye el equipo, que no se pudo calcular."

    # Lo que no está en el total se avisa al lado del total y del encaje
    avisos = []

    # El aviso del 42 (con horas); el del 37 lo repetía y se quitó el 28 oct
    if no_sumados:
        avisos.append(f"Ojo: el total no incluye las horas de {escape('; '.join(no_sumados))}, porque no tienen "
                      f"tarifa en la tabla de la empresa. El coste real será mayor, así que el encaje es provisional.")

    def _en_gratuito(c: ComponenteCoste) -> bool:
        """Fallo 53: a 0 € porque el uso cabe en lo gratuito de su fila de la tabla."""
        return bool(c.tarifa) and (c._cantidad or 0) > 0

    # Fallo 54: un 0 € vale si cabe en lo gratuito de la tabla o si el cliente lo tiene de verdad (está en sus datos)
    a_cero = [c for c in componentes if not c.importe_eur and not _en_gratuito(c)
              and (c.tarifa or not _ya_lo_tiene(c, tecnologias))]
    if a_cero:
        nombres = _lista([c.componente for c in a_cero])
        avisos.append(f"Ojo: {escape(nombres)} {'va' if len(a_cero) == 1 else 'van'} a 0 €, así que no suman nada al "
                      f"total. Si se pagan por uso (cómputo, ejecuciones, un endpoint…), el coste real será mayor.")
        no_consta = [c.componente for c in a_cero if not c.tarifa and _ya_lo_tiene(c)]
        if no_consta:
            uno = len(no_consta) == 1
            quienes = "" if len(no_consta) == len(a_cero) else f" ({escape(_lista(no_consta))})"
            avisos[-1] += (f" El modelo dice que el cliente ya {'lo tiene' if uno else 'los tiene'}{quienes}, pero "
                           f"{'no está' if uno else 'no están'} entre lo que usa según sus datos.")

    revisar = [c for c in componentes
               if presupuesto > 0 and _coste_al_mes(c, meses_max) > PARTE_MAXIMA_AL_MES * presupuesto]
    # Fallo 53: si el precio es de la tabla, lo que puede estar mal es la cantidad; si es del modelo, la unidad
    for de_tabla in (False, True):
        grupo = [c for c in revisar if bool(c.tarifa) == de_tabla]
        if not grupo:
            continue
        nombres = _lista([f"{c.componente} ({_cifra(_coste_al_mes(c, meses_max))} €/mes)" for c in grupo])
        uno = len(grupo) == 1
        motivo_revisar = (f"El precio es el de la tabla, así que puede que la cantidad de uso esté mal. " if de_tabla else
                          f"Puede que el precio sea por un paquete (por millón de mensajes, por ejemplo) y no por "
                          f"unidad. ")
        avisos.append(f"Ojo: revisa {'la cantidad' if de_tabla else 'la unidad'} de {escape(nombres)}: "
                      f"{'cuesta' if uno else 'cada uno cuesta'} al mes más de un {PARTE_MAXIMA_AL_MES * 100:.0f} % del "
                      f"presupuesto del cliente ({_eur(presupuesto)}). {motivo_revisar}"
                      f"{'Va sumado' if uno else 'Van sumados'} en el total, que puede estar inflado.")

    if not componentes:
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
    if equipo and equipo.get("meses_implantacion"):
        # Fallo 43: el desarrollo y la implantación, cada uno con sus meses
        impl = equipo["meses_implantacion"]
        meses_dev = _meses_txt(equipo["meses_min"] - impl, equipo["meses_max"] - impl)
        filas.append((f"Desarrollo · {meses_dev}", f"Equipo: {_rango(equipo['desarrollo_min'], equipo['desarrollo_max'])}",
                      equipo["desarrollo_max"], "#0f766e"))  # teal
        filas.append((f"Implantación · {_meses_txt(impl, impl)}", f"Equipo: {_eur(equipo['implantacion'])}",
                      equipo["implantacion"], "#15803d"))  # green
    elif equipo:
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
    for c in componentes:
        categoria, color = CATEGORIAS[c.categoria]
        en_proyecto = _rango(_coste_en_proyecto(c, meses_min), _coste_en_proyecto(c, meses_max))
        if c.periodicidad == "anual":
            en_proyecto += " (parte proporcional)"
        importe = f"{_cifra(c.importe_eur)} {_UNIDAD[c.periodicidad]}"
        #Un componente a 0 € solo cuadra si el cliente ya lo tiene; lo demás se avisa
        if not c.importe_eur:
            importe = ("0 € (cabe en el nivel gratuito)" if _en_gratuito(c)
                       else "0 € (ya lo tiene el cliente)" if not c.tarifa and _ya_lo_tiene(c, tecnologias)
                       else '<span style="color:#b45309;">0 € (por revisar)</span>')
        elif any(c is x for x in revisar):
            importe += (f' <span style="color:#b45309;">'
                        f'({"revisar cantidad" if c.tarifa else "revisar unidad"})</span>')
        # Fallo 53: se ve de dónde sale el precio
        origen = "precio de la tabla" if c.tarifa else "precio estimado por el modelo"
        cards_html += (
            f'<div style="background:var(--surface);border:1px solid var(--border);border-top:3px solid {color};'
            f'border-radius:9px;padding:0.75rem;min-width:150px;flex:1;box-shadow:var(--shadow);">'
            f'<div style="font-size:0.75rem;color:var(--muted);margin-bottom:3px;">{escape(c.componente)}</div>'
            f'<div style="font-size:1rem;font-weight:700;color:var(--text);">{importe}</div>'
            f'<div style="font-size:0.72rem;color:var(--muted);margin-top:2px;">'
            f'{_cifra(c.precio_unitario_eur)} €/{escape(c.unidad)} × {_cifra(c.unidades)} · {origen}</div>'
            f'<div style="font-size:0.75rem;color:var(--text);margin-top:3px;">En {meses_txt}: {en_proyecto}</div>'
            f'<div style="font-size:0.7rem;color:var(--muted);margin-top:3px;opacity:0.75;">{escape(c.referencia)}</div>'
            f'<div style="font-size:0.7rem;color:var(--muted);opacity:0.5;">{categoria}</div>'
            f'</div>'
        )
    if not cards_html:
        cards_html = ('<p style="font-size:0.85rem;color:var(--muted);margin:0;">La propuesta no trae componentes '
                      'valorados: no hay importes de infraestructura, licencias, formación ni soporte.</p>')
    # Fallo 53: con la tabla de precios, la nota dice qué parte sale de ella
    de_tabla = sum(1 for c in componentes if c.tarifa)
    nota_precios = "" if infracost_raw else (
        '<p style="font-size:0.75rem;color:var(--muted);margin:0 0 0.5rem;">'
        + ("Importes estimados por el modelo, sin contrastar con Infracost." if not de_tabla else
           "Precios de la tabla de precios de la empresa; las cantidades de uso las estima el modelo."
           if de_tabla == len(componentes) else
           f"{de_tabla} de {len(componentes)} componentes llevan el precio de la tabla de precios de la empresa; el "
           f"resto, el que estima el modelo. Las cantidades de uso las estima el modelo.")
        + '</p>'
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
        f'<span style="font-size:0.82rem;font-weight:600;color:{conf_color};">{etiqueta_nivel}</span>'
        f'</div>'
        f'<div style="background:var(--border);border-radius:999px;height:9px;overflow:hidden;">'
        f'<div style="background:{conf_color};width:{conf_pct};height:100%;border-radius:999px;"></div>'
        f'</div></div>'
        f'{aviso_html}'
        f'<p style="color:var(--text);font-size:0.875rem;margin:0 0 0.5rem;">{motivo}</p>'
        # La justificación la escribe el LLM sin conocer el equipo: se dice a qué cifras se refiere
        f'<p style="color:var(--muted);font-size:0.875rem;margin:0;font-style:italic;">'
        f'Infraestructura, licencias, formación y soporte ({_rango(sin_equipo_min, sin_equipo_max)}, '
        f'sin el equipo){":" if justificacion else "."}</p>'
        f'{justificacion_html}'
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

        # Fallo 45 (C6): precios en euros. INFRACOST_CURRENCY es la del CLI 0.10; INFRACOST_CLI_CURRENCY, la del nuevo
        env = {**os.environ, "INFRACOST_API_KEY": api_key, "INFRACOST_CURRENCY": "EUR", "INFRACOST_CLI_CURRENCY": "EUR"}
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

def _perfiles_nombrados(texto: str, perfiles: list[str]) -> list[str]:
    """Los perfiles de la lista que aparecen en el texto, sin mirar mayúsculas, tildes ni espacios."""
    normalizado = _clave_perfil(texto)
    return [p for p in perfiles if _clave_perfil(p) in normalizado]


def _guardrail_perfiles(reintentos: int = 1):
    """Guardrail de la tarea de perfiles (fallo 40). Si la descripción no nombra ningún perfil de la tabla, ha
    llegado cortada y CrewAI repite la tarea con el motivo. Tras `reintentos` repeticiones se acepta lo que llegue:
    si el guardrail siguiera fallando, CrewAI lanzaría una excepción y se perdería el informe entero."""
    repetidas = 0

    def comprobar(salida):
        nonlocal repetidas
        texto = getattr(salida, "raw", "") or ""
        if _perfiles_nombrados(texto, PERFILES_CON_TARIFA) or repetidas >= reintentos:
            return True, texto
        repetidas += 1
        logger.warning("Descripción de perfiles incompleta (%d caracteres, termina en %r): se repite la tarea",
                       len(texto), texto[-80:])
        return False, ("La descripción está incompleta: no nombra ningún perfil de la tabla de tarifas. "
                       "Escríbela entera, con un apartado por perfil y sin tablas.")

    return comprobar



# Fallo 46: las tareas de costes y de equipo ya no usan output_pydantic. Con él, CrewAI pide la respuesta como
# llamada a herramienta (instructor en modo TOOLS); ministral devolvía el JSON como texto, «No tool calls or function
# call found» tres veces, y el informe se quedaba colgado. Ahora el JSON se pide en el prompt y se valida aquí.
SIN_JSON = "[sin JSON válido]"  # lo que deja el guardrail cuando se rinde


def _plantilla_json(modelo: type[BaseModel]) -> str:
    """La forma del JSON, sacada del modelo para que no se desfase: 0 en los números, «texto» y las opciones con «|»."""
    def valor(tipo: Any, campo: Any = None) -> Any:
        extra = getattr(campo, "json_schema_extra", None)
        if isinstance(extra, dict) and extra.get("enum"):
            return "|".join(extra["enum"])
        if get_origin(tipo) is Literal:
            return "|".join(get_args(tipo))
        if get_origin(tipo) is list:
            return [valor(get_args(tipo)[0])]
        if isinstance(tipo, type) and issubclass(tipo, BaseModel):
            return {nombre: valor(f.annotation, f) for nombre, f in tipo.model_fields.items()}
        return 0 if tipo in (int, float) else "texto"
    return json.dumps(valor(modelo), ensure_ascii=False)


def _desenvolver(objeto: Any, modelo: type[BaseModel]) -> dict | None:
    """El primer dict, mirando nivel a nivel, que tenga algún campo de la raíz de `modelo` (fallo 47). Así vale el JSON
    aunque venga envuelto ({"estimacion": {...}}, {"properties": {...}}) o como llamada a función, con los argumentos
    en un texto ({"name": …, "arguments": "{…}"})."""
    campos = set(modelo.model_fields)
    pendientes = [objeto]
    while pendientes:
        actual = pendientes.pop(0)
        if isinstance(actual, str) and actual.lstrip().startswith("{"):
            try:
                actual = json.loads(actual)
            except ValueError:
                continue
        if isinstance(actual, dict):
            if campos & set(actual):
                return actual
            pendientes.extend(actual.values())
        elif isinstance(actual, list):
            pendientes.extend(actual)
    return None

# Fallo 50: «"unidades": 2000 * 30» no es JSON; se escribe el resultado antes de leerlo (json_repair se quedaría con 2000)
_OPERACION = re.compile(r"(:\s*)(\d+(?:\.\d+)?(?:\s*[*×]\s*\d+(?:\.\d+)?)+)(?=\s*[,}\]\n])")


def _calcular_operaciones(texto: str) -> str:
    def producto(m: re.Match) -> str:
        valor = 1.0
        for factor in re.split(r"\s*[*×]\s*", m.group(2)):
            valor *= float(factor)
        return m.group(1) + (str(int(valor)) if valor == int(valor) else str(valor))
    return _OPERACION.sub(producto, texto)


def _reparar_json(texto: str, inicio: int) -> dict | None:
    """Fallo 50: el JSON desde `inicio` hasta la última llave, arreglado con json_repair (comas al final, comentarios,
    comillas sin escapar…). json_repair viene con CrewAI. None si no se puede."""
    try:
        from json_repair import loads as reparar
    except ImportError:
        return None
    fin = texto.rfind("}")
    if fin <= inicio:
        return None
    try:
        objeto = reparar(texto[inicio:fin + 1])
    except Exception:
        return None
    return objeto if isinstance(objeto, dict) else None

def _leer_json(texto: str, modelo: type[BaseModel]) -> BaseModel:
    """El primer objeto JSON del texto que encaje en `modelo`, aunque venga entre ```json, con texto antes o después o
    envuelto en otra clave. Si no hay ninguno, ValueError con el motivo, que el guardrail le pasa al modelo."""
    decoder = json.JSONDecoder()
    texto = _calcular_operaciones(texto)
    campos = ", ".join(modelo.model_fields)
    primer_error, primeras_claves, cortado, invalido = None, None, False, ""
    for n, inicio in enumerate(m.start() for m in re.finditer(r"\{", texto)):
        try:
            objeto, _ = decoder.raw_decode(texto, inicio)
        except json.JSONDecodeError as error:
            if n:
                continue
            # Fallo 47: si el primer objeto abre más llaves de las que cierra, la respuesta llegó cortada
            resto = texto[inicio:]
            cortado = resto.count("{") > resto.count("}")
            # Fallo 50: si no está cortado, es que tiene un error de sintaxis; se dice dónde y se intenta reparar
            trozo = texto[max(error.pos - 60, 0):error.pos + 40]
            invalido = f"el JSON no es válido cerca del carácter {error.pos} ({error.msg}): …{trozo}…"
            objeto = None if cortado else _reparar_json(texto, inicio)
            if objeto is None:
                continue
            logger.warning("JSON reparado con json_repair; %s", invalido)
        if not isinstance(objeto, dict):
            continue
        primeras_claves = primeras_claves or sorted(objeto)[:10]
        candidato = _desenvolver(objeto, modelo)
        if candidato is None:
            continue
        try:
            return modelo.model_validate(candidato)
        except ValidationError as error:
            primer_error = primer_error or error
    if cortado:
        raise ValueError(f"el JSON llegó cortado: la respuesta ({len(texto)} caracteres) se acaba antes de cerrarlo")
    if primer_error is not None:
        detalle = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in primer_error.errors()[:5])
        raise ValueError(f"el JSON no tiene la forma pedida ({detalle})" + (f"; antes, {invalido}" if invalido else ""))
    if invalido:
        raise ValueError(invalido)
    if primeras_claves is not None:
        raise ValueError(f"ningún objeto JSON tiene los campos de la raíz ({campos}); el primero traía {primeras_claves}")
    raise ValueError("no hay ningún objeto JSON en la respuesta")


def _guardrail_json(modelo: type[BaseModel], tarea: str, reintentos: int = 1):
    """Guardrail de las tareas con salida estructurada (fallo 46). Si la respuesta no trae un JSON válido, CrewAI
    repite la tarea con el motivo. Tras `reintentos` repeticiones devuelve True con SIN_JSON: si el guardrail
    fallara otra vez, CrewAI lanzaría una excepción y se perdería el informe entero (como en _guardrail_perfiles)."""
    repetidas = 0
    plantilla = _plantilla_json(modelo)
    

    def comprobar(salida):
        nonlocal repetidas
        texto = getattr(salida, "raw", "") or ""
        try:
            return True, _leer_json(texto, modelo).model_dump_json()
        except ValueError as error:
            motivo = str(error)
        # Fallo 47: el log dice qué llegó, para saber si vino envuelto, cortado o con otra forma
        muestra = f"{len(texto)} caracteres; empieza {texto[:200]!r}; acaba {texto[-120:]!r}"
        if repetidas >= reintentos:
            logger.error("Tarea de %s: sigue sin un JSON válido después de repetirla (%s); se sigue sin ella. "
                         "Respuesta: %s", tarea, motivo[:300], muestra)
            return True, SIN_JSON
        repetidas += 1
        logger.warning("Tarea de %s: la respuesta no trae un JSON válido (%s); se repite. Respuesta: %s",
                       tarea, motivo[:300], muestra)
        breve = (" Sé más breve: referencias y textos de pocas palabras." if "cortado" in motivo else
                 " Revisa la sintaxis: comillas dobles, sin comas antes de ] o }, sin comentarios y cada número ya "
                 "calculado, sin operaciones." if "no es válido" in motivo else "")
        return False, (f"La respuesta no es válida: {motivo[:600]}.{breve} Responde solo con el objeto JSON, sin texto "
                       f"antes ni después. La raíz del JSON tiene que tener estos campos: {', '.join(modelo.model_fields)}. "
                       f"Esta es su forma: {plantilla}")

    return comprobar


def _leer_salida(tarea: Task | None, modelo: type[BaseModel]) -> BaseModel | None:
    """El resultado de una tarea con salida estructurada, o None si no lo hay (la crew falló o el guardrail se rindió)."""
    salida = getattr(tarea, "output", None)
    ya_leido = getattr(salida, "pydantic", None)
    if isinstance(ya_leido, modelo):
        return ya_leido
    texto = getattr(salida, "raw", "") or ""
    if not texto or texto == SIN_JSON:
        return None
    try:
        return _leer_json(texto, modelo)
    except ValueError:
        return None


def _aviso_sin_costes(model_name: str) -> str:
    """Lo que sale en la sección de costes si el modelo no devolvió los importes: el resto del informe sigue."""
    return ('<div style="background:var(--surface-alt);border:1px solid var(--border);border-radius:12px;padding:1.5rem;">'
            '<p style="color:#b45309;font-size:0.95rem;font-weight:600;margin:0 0 0.5rem;">Ojo: no se han podido '
            'estimar los costes.</p><p style="font-size:0.875rem;color:var(--text);margin:0;">El modelo '
            f'({escape(model_name)}) no devolvió los importes en el formato esperado, ni siquiera al repetir la '
            'tarea. El resto del informe sí está completo. Vuelve a generar la estimación o prueba con otro '
            'modelo.</p></div>')



def run_estimation_crew(
    proposal_context: str,
    model_name: str,
    tools: list | None = None,
    problem_case: dict | None = None,
    componentes: str | None = None,
) -> EstimationResult:
    """
    Ejecuta la crew de estimación de costos y perfiles de equipo.

    Pre-fase (Python puro): genera Terraform desde la propuesta → infracost breakdown → precios reales.
    Fase CrewAI: report_writer_agent (sin tools) devuelve el JSON de CostEstimationReport, que valida un guardrail.

    `proposal_context` es el output serializado de ProposalResult.as_combined_context(); lo usan los perfiles.
    `componentes` es la parte de la propuesta con los componentes y sus volúmenes (la arquitectura); la tarea de
    costes solo recibe esto y los datos del cliente (fallo 46). Si no se pasa, se usa la propuesta entera.
    """
    agents_config = load_agents_config()
    print(f"[estimation_crew] Modelo: {model_name}")  # para saber con qué modelo salió cada informe

    # --- Pre-fase: obtener precios reales via Infracost subprocess ---
    api_key = os.getenv("INFRACOST_API_KEY", "")
    binary = _resolve_infracost_binary()
    infracost_prices: str | None = None

    if api_key and binary:
        terraform_hcl = _generate_terraform_from_proposal(componentes or proposal_context, model_name)
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
        f"Resumen de costos reales obtenido de Infracost, en euros (basado en infraestructura generada desde la propuesta):\n{infracost_prices}"
        if infracost_prices else
        "No hay precios reales disponibles: usa estimaciones razonables a partir de precios públicos, en euros."
    )

    # Fallo 53: la tabla de precios de la empresa, con las filas de la nube del cliente
    tabla_precios = _tabla_precios(problem_case, componentes or proposal_context)
    if tabla_precios:
        pricing_note += (
            "\nTabla de precios: para cada componente que sea uno de estos servicios, pon el id de su fila en tarifa y, "
            "en unidades, la cantidad de un mes en la unidad de la fila, entera y sin dividir por el paquete (144 "
            "millones de eventos son 144000000; un servicio encendido todo el mes son 730 horas). Con tarifa, el "
            "precio, el paquete en que se cobra y lo gratuito los pone el programa: pon precio_unitario_eur 0, la "
            "unidad de la fila y periodicidad mensual. Si un servicio se cobra por varias cosas (eventos y unidades "
            "de throughput, cómputo y almacenamiento), pon un elemento por fila. Usa una fila solo si es el mismo "
            "servicio y el mismo nivel; si ninguna corresponde, deja tarifa vacía y da tú el precio unitario, la "
            "unidad y la periodicidad.\n"
            f"{tabla_precios}\n"
        )

    cost_report_task = Task(
        description=(
            "A partir de la propuesta de solución y los precios indicados, valora el coste de cada componente de la propuesta. "
            f"{pricing_note}\n"
            "En componentes pon un elemento por cada servicio cloud, licencia, formación o soporte que aparezca en la "
            "arquitectura, sin dejarte ninguno, cada uno con su categoría, su precio unitario en euros (solo el número), "
            "las unidades que se pagan en cada periodo, qué es una unidad y la periodicidad: mensual, anual o unico "
            "(si se paga una sola vez). No multipliques: el importe lo calcula el programa. Lo que se paga por uso "
            "(horas, GB, ejecuciones) va como mensual, con las unidades de un mes. Si un componente tiene partes con "
            "precios distintos, pon un elemento por parte. Si no tienes el precio exacto, da tu mejor estimación y di "
            "en referencia de dónde sale. "
            # Fallo 54: el 0 € por «ya lo tiene» se comprueba con sus datos; lo gratuito lo descuenta la tabla
            "Pon precio 0 solo si el cliente ya lo tiene pagado: tiene que estar entre las tecnologías que ya usa según "
            "sus datos (escribe en referencia «Ya lo tiene el cliente»). No pongas 0 por el nivel gratuito: con tarifa, "
            "lo gratuito lo descuenta el programa; sin tarifa, pon el precio normal y di en referencia qué nivel "
            "gratuito podría aplicarse. "
            "Que un servicio no tenga cuota no lo hace gratis: el cómputo, las ejecuciones o un endpoint se pagan por "
            "uso, así que estima el uso del piloto. "
            "El coste del equipo (desarrollo e implantación) se calcula aparte con las tarifas de la empresa: NO lo "
            "incluyas como componente. Los totales, el horizonte y el encaje con el presupuesto se calculan después a "
            "partir de tus importes. En meses_proyecto pon los meses del alcance que se presupuesta (el piloto, si lo "
            "hay) según el plazo del cliente. En presupuesto_cliente_max_eur pon el máximo del presupuesto que "
            "indica el caso del cliente, o 0 si no lo indica. Si incluyes una contingencia, usa un único porcentaje, "
            "el mismo en todo el informe: ponlo en contingencia_pct (0 si no hay) y no lo añadas como componente. "
            # Fallo 57: sin totales propios, que no cuadraban con las tarjetas
            "En justificacion explica solo de dónde salen los importes y qué los haría variar; no des totales ni sumas, "
            "que los calcula el programa, y no digas si caben en el presupuesto del cliente, porque el total con el "
            "equipo se calcula después.\n"
            "Para los supuestos: si te falta información, indícalo como "
            "'Por determinar, requiere [dato X]' o 'Se asume [dato X]' — nunca inventes datos críticos.\n"
            # C9 (lo había deshecho el 46): el idioma lo marca el cliente; las opciones son claves fijas
            "Escribe los textos en el idioma de los datos del cliente, con su ortografía completa (en español, con "
            "tildes). Las opciones de categoria y periodicidad son claves fijas: van tal cual, sin tilde (formacion, "
            "unico), sea cual sea el idioma. Los importes, siempre en euros.\n"
            "Responde solo con un objeto JSON, sin texto antes ni después, con esta forma: "
            f"{_plantilla_json(CostEstimationReport)}\n\n"
            f"{datos_cliente}"
            "Arquitectura propuesta (componentes y volúmenes):\n"
            f"{componentes or proposal_context}"
        ),
        expected_output="Un objeto JSON con un elemento por componente de la arquitectura (su fila de la tabla de precios "
                        "o su precio unitario en euros, sus unidades y su periodicidad), el resumen y los supuestos.",
        name="costes",
        agent=report_writer_agent,
        # Fallo 46: sin output_pydantic, para no depender de las llamadas a herramienta; el JSON se valida aquí
        guardrail=_guardrail_json(CostEstimationReport, "costes"),
        guardrail_max_retries=1,
    )

    # Fallo 37: los perfiles se nombran como en la tabla de tarifas desde el principio, para que el texto y el coste coincidan
    catalogo_perfiles = (
        "Nombra cada perfil con uno de estos, tal cual, porque son los que tienen tarifa en la empresa: "
        f"{', '.join(PERFILES_CON_TARIFA)}. Es un catálogo para elegir, no una lista que haya que cubrir. "
        "Si hace falta trabajo de una persona que no encaja en ninguno (por ejemplo, la gestión del proyecto), dilo "
        "aparte como función sin perfil en la tabla de tarifas, con sus horas al mes; no inventes un perfil para ella. "
        "La formación, las licencias y los servicios no son funciones del equipo: se valoran en los costes. "
    ) if PERFILES_CON_TARIFA else ""

    team_building_task = Task(
        description=(
            "A partir de la propuesta de solución y de los datos del cliente, identifica los perfiles profesionales necesarios "
            "para implementar lo que pide el cliente en el plazo que indica (el piloto, si lo hay), no la solución completa a largo plazo. "
            "Incluye solo los perfiles imprescindibles para ese alcance y, para cada uno, sus responsabilidades clave, las "
            "habilidades requeridas y si su dedicación es completa o parcial. "
            "Escribe un apartado por perfil, con su nombre como título y una lista debajo; no uses tablas. "
            "Distingue su dedicación en el desarrollo (construir la solución) y en la implantación (ponerla en "
            "producción y acompañar al cliente hasta que la use); un perfil puede estar solo en una de las dos. "
            f"{catalogo_perfiles}"
            "Ten en cuenta el tipo de servicio que pide el cliente: si quiere que Inetum lo implemente, los perfiles son del "
            "equipo de Inetum, y el equipo interno del cliente participa y se forma; dilo así. Ten en cuenta también el "
            "equipo técnico que ya tiene el cliente y lo que sabe hacer. "
            "No des cifras de coste: el coste del equipo se calcula aparte con las tarifas de la empresa. "
            "Escribe en el idioma de los datos del cliente, con su ortografía completa (en español, con tildes).\n\n"
            f"{datos_cliente}"
            "Esta es la solución propuesta sobre la que debes identificar los perfiles profesionales necesarios:\n"
            f"{proposal_context}"
        ),
        expected_output="Los perfiles necesarios para el alcance pedido, con las responsabilidades, las habilidades y la dedicación (completa o parcial) de cada uno.",
        name="perfiles",
        agent=team_builder,
        # Fallo 40 (C14): si la descripción llega cortada, se repite una vez antes de pasar a la tarea del coste
        guardrail=_guardrail_perfiles() if PERFILES_CON_TARIFA else None,
        guardrail_max_retries=1,
    )

    team_cost_task = Task(
        description=(
            "A partir de los perfiles de equipo identificados en la tarea anterior, define el equipo que se va a presupuestar: "
            "los mismos perfiles, sin añadir otros. "
            "Las funciones que la tarea anterior dejó sin perfil en la tabla de tarifas van en funciones_sin_tarifa, no en perfiles, "
            # Fallo 55: una vez cada función, con sus horas de cada fase, como los perfiles
            "cada una una sola vez, con sus horas al mes en el desarrollo (horas_mes) y en la implantación "
            "(horas_mes_implantacion), como los perfiles; no pongas ahí formación, licencias ni servicios, que se "
            "valoran aparte. "
            "Para cada perfil indica: su nombre exacto en la tabla de tarifas (primera columna), "
            "el nivel (Junior/Middle/Senior) y las horas al mes en el desarrollo (horas_mes) y en la implantación "
            "(horas_mes_implantacion), con 0 en la fase en la que no participe. 160 horas al mes es jornada completa: úsala solo para "
            "quien trabaje a tiempo completo en este alcance, y a los perfiles con dedicación parcial ponles las horas que les correspondan. "
            # Fallo 49: los meses del piloto, y una implantación más corta que el plazo
            "Indica también cuántos meses trabajará el equipo en el alcance que se presupuesta (meses_min y meses_max): "
            "si el cliente pide un piloto, los meses del piloto, no los del despliegue completo. En meses_implantacion "
            "pon cuántos de esos meses, al final, son de implantación; antes tiene que quedar al menos un mes de "
            "desarrollo. "
            "No calcules costes: se calculan aparte con la tabla.\n"
            "Responde solo con un objeto JSON, sin texto antes ni después, con esta forma: "
            f"{_plantilla_json(EquipoEstimado)}\n\n"
            f"{datos_cliente}"
            f"Tarifas de referencia (€/hora):\n{team_costs_table}"
        ),
        expected_output="Un objeto JSON con los perfiles del equipo (nivel y horas al mes) y la duración del trabajo en "
                        "meses (mínima y máxima).",
        name="equipo",
        context=[team_building_task],
        agent=team_builder,
        # Fallo 46: igual que la de costes, sin output_pydantic y con el JSON validado aquí
        guardrail=_guardrail_json(EquipoEstimado, "equipo"),
        guardrail_max_retries=1,
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
    
    # Fallo 46: un fallo de la crew de costes no para el informe; lo que no llegue se avisa en su sección
    try:
        crew.kickoff()
    except Exception as exc:
        logger.error("[estimation_crew] La crew de costes falló con el modelo %s: %s", model_name, exc)

    # Coste del equipo: el LLM elige perfiles, nivel, horas y meses; las cuentas se hacen aquí
    equipo_pydantic = _leer_salida(team_cost_task, EquipoEstimado) if team_cost_task else None
    equipo = _calcular_equipo(equipo_pydantic, tarifas) if equipo_pydantic else None
    if team_cost_task and equipo is None:
        logger.warning("No se pudo leer el equipo estimado: el coste total no incluirá el equipo.")

    # Costes: el JSON ya validado por el guardrail; si no llegó, la sección lo avisa y el informe sigue
    report = _leer_salida(cost_report_task, CostEstimationReport)
    if report:
        cost_estimation = _format_cost_report(report, infracost_prices or "", equipo, problem_case)
    else:
        logger.error("[estimation_crew] Sin costes: el modelo %s no devolvió un JSON válido", model_name)
        cost_estimation = _aviso_sin_costes(model_name)

    team_profiles = getattr(getattr(team_building_task, "output", None), "raw", "") or ""
    if not team_profiles:
        team_profiles = ("**Ojo:** no se han podido describir los perfiles del equipo: el modelo no respondió. "
                         "Vuelve a generar la estimación o prueba con otro modelo.")


    # Fallo 40 (C14): si la descripción no nombra algún perfil presupuestado, se avisa en vez de dejarla cortada sin más
    if isinstance(equipo_pydantic, EquipoEstimado) and team_profiles:
        presupuestados = [p.perfil.strip().replace("|", "/") for p in equipo_pydantic.perfiles]
        nombrados = _perfiles_nombrados(team_profiles, presupuestados)
        faltan = [p for p in presupuestados if p not in nombrados]
        if faltan:
            logger.warning("La descripción de perfiles no menciona a %s (%d caracteres, termina en %r)",
                           faltan, len(team_profiles), team_profiles[-80:])
            team_profiles += (f"\n\n**Ojo:** la descripción de arriba no menciona a {', '.join(faltan)}; puede haber "
                              "llegado cortada. La tabla de coste de abajo sí está completa.")

    if equipo:
        team_profiles = team_profiles + "\n\n---\n\n## Estimación de Coste del Equipo\n\n" + equipo["texto"]


    return EstimationResult(
        cost_estimation=cost_estimation,
        team_profiles=team_profiles,
    )
