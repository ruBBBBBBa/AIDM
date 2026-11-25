"""Fallo 132: el resumidor del informe y la pestaña «Resumen».

Dos llamadas (tarea resumen_secciones de data_structures/modelos.yaml, perfil contexto_largo):
1. resumir_propuesta(), al empezar el roadmap y a la vez que sus fases: un resumen por sección de la propuesta
   (plataforma, arquitectura, gobernanza, datos e IA, visualización), con la frase para todos del bloque de la
   pestaña Resumen y el «Resumen» del interruptor «Detalle | Resumen» de su desplegable.
2. completar_resumen(), al acabar el roadmap: el resumen general, los puntos clave (cada uno con la sección de la que
   sale), los puntos y el «Resumen» de cada fase del roadmap que se ha generado y los puntos de cada fase CRISP-DM.
   Después monta la pestaña Resumen en el informe generado.

El modelo solo resume y no escribe cifras: las de costes (total, MVP, al mes) salen de las variables del «Resumen
ejecutivo» de costes (cifras_costes), y una cifra del modelo que no esté en el texto del que sale se quita, con su
línea en el log. Si una llamada falla o su JSON no se lee, su parte sale sin texto generado y el informe sigue.
Los destinos de los enlaces son ids fijos (#seccion-…, #fase-…, #crisp-…), nunca los ids internos del informe.
"""
import copy
import json
import logging
import re

from bs4.element import Script
from crewai import Agent, Crew, Process, Task

import gateway
import progreso
from agents import inventario
from agents import roadmap_builder as rb
from agents.discovery_agent_logic import _cifras, _leer_json
from agents.roadmap_builder import NO, SI, YA
from utils import _normalize_text, contexto_cliente, html_del_informe, load_agents_config, parse_html, sin_emojis

logger = logging.getLogger("aidm")

ETIQUETA = "resumen"
MAX_TOKENS = 8192  # la llamada 2 es un JSON grande (general, 5 puntos, hasta 7 fases y 6 de CRISP-DM)

# Clave del JSON, título del bloque, id del texto en el informe e id fijo de destino (en la cabecera del desplegable)
SECCIONES = [
    ("plataforma", "Plataforma", "content-platform", "seccion-plataforma"),
    ("arquitectura", "Arquitectura", "content-architecture", "seccion-arquitectura"),
    ("gobernanza", "Gobernanza", "content-governance", "seccion-gobernanza"),
    ("datos_ia", "Datos e IA", "content-data-ai", "seccion-datos-ia"),
    ("visualizacion", "Visualización", "content-bi", "seccion-visualizacion"),
]
COSTES = ("costes", "Costes", "content-cost-estimation", "seccion-costes")  # sin resumen: sus cifras, de Python
# Las fases del roadmap, en el orden del flujo: clave del JSON, id del texto e id fijo de destino
FASES = [
    ("fuentes", "roadmap-sources", "fase-fuentes"),
    ("ingesta", "roadmap-ingestion", "fase-ingesta"),
    ("almacenamiento", "roadmap-storage", "fase-almacenamiento"),
    ("etl", "roadmap-etl", "fase-etl"),
    ("modelado", "roadmap-modeling", "fase-modelado"),
    ("ops", "roadmap-ml", "fase-ops"),
    ("visualizacion_roadmap", "roadmap-bi", "fase-visualizacion"),
]
REUTILIZADAS = "fases-reutilizadas"  # la línea «Se reutiliza: …» del 131: el destino de las fases «ya lo tiene»
# Las fases CRISP-DM de la pestaña de Asier: clave del JSON, nombre e id del botón (data-phase 1…6)
CRISP = [
    ("negocio", "Comprensión del negocio", "crisp-negocio"),
    ("datos", "Comprensión de los datos", "crisp-datos-fase"),
    ("preparacion", "Preparación de datos", "crisp-preparacion"),
    ("modelado", "Modelado", "crisp-modelado"),
    ("evaluacion", "Evaluación", "crisp-evaluacion"),
    ("despliegue", "Despliegue", "crisp-despliegue"),
]
CRISP_DATOS = "crisp-datos"  # el <script type="application/json"> del que leerá sus textos la pestaña CRISP-DM
_LEE_CRISP_DATOS = re.compile(r"crisp-datos(?![\w-])")

# Topes de palabras, para que la pestaña Resumen quepa en una cara A4
PARA_TODOS, GENERAL = 45, 60
CAMPOS = {"decision_principal": 40, "por_que": 60, "siguiente_paso": 30}
LISTAS = {"piezas_clave": (5, 12), "riesgos": (3, 20)}
PUNTOS_CLAVE = (5, 25)
PUNTOS_FASE = (2, 15)
TITULOS_RESUMEN = [("decision_principal", "Decisión principal"), ("por_que", "Por qué"),
                   ("piezas_clave", "Piezas clave"), ("riesgos", "Riesgos"), ("siguiente_paso", "Siguiente paso")]

_FORMATO_SECCION = ('{"decision_principal": "…", "por_que": "…", "piezas_clave": ["…"], "riesgos": ["…"], '
                    '"siguiente_paso": "…"}')
_TOPES = ("decision_principal: la decisión principal, 40 palabras como máximo. por_que: por qué, 60 como máximo. "
          "piezas_clave: hasta 5, de 12 palabras como máximo. riesgos: hasta 3, de 20 como máximo. "
          "siguiente_paso: 30 como máximo.")
_REGLAS = ("Solo resume: nada que no esté en los textos de abajo. Escribe en el idioma de esos textos. "
           "No escribas importes ni cifras de dinero; si das otra cifra, tiene que estar en el texto del que sale. "
           # Fallo 132 (parte 3): «arquitectura tipo lago-casa» por Lakehouse
           "Los nombres técnicos no se traducen: para quien no es técnico, se nombran y se dice qué hacen (por ejemplo, "
           "«un Lakehouse (un almacén único para todos los datos)»). Frases cortas.\n")
# Fallo 132 (parte 3): las viñetas de las fases y del CRISP-DM, para la dirección, sin los detalles de implementación
_VINETAS = ("de 12 palabras como máximo, cada una una frase completa que empieza por lo que consigue el cliente; usa el "
            "nombre técnico cuando haga falta, con lo que hace si no es evidente, y deja fuera los detalles de "
            "implementación que a un directivo no le dicen nada (por ejemplo, «watermark», «VACUUM/OPTIMIZE», "
            "«particionado por año/mes» o el motor que ejecuta cada paso).")
# Fallo 132 (parte 3): palabras de un nombre de servicio que, sueltas, no lo nombran («Data», «Lake», «Delta»…)
_NO_SUELTAS = {"data", "lake", "delta", "key", "machine", "learning", "power", "sql", "server", "entra", "self-hosted",
               "integration", "pipelines", "analytics", "storage", "engineering", "science", "factory", "runtime",
               "endpoint", "monitor", "functions", "service", "services", "cloud", "ai"}


# ── Lectura y validación ──

def _texto(soup, div_id: str) -> str:
    """El texto de una sección del informe; vacío si no se generó."""
    div = soup.find(id=div_id)
    if div is None or div.find(class_="empty-state"):
        return ""
    return re.sub(r"\s+", " ", div.get_text(" ", strip=True))


def _palabras(valor, tope: int) -> str:
    """Fallo 132 (parte 3): nunca a mitad de frase. La que pasa del tope se corta en su última coma dentro del tope y,
    si no la tiene, se deja entera."""
    texto = sin_emojis(valor).strip() if isinstance(valor, str) else ""
    palabras = texto.split()
    if len(palabras) <= tope:
        return texto
    dentro = " ".join(palabras[:tope])
    coma = dentro.rfind(",")
    return dentro[:coma].rstrip() if coma > 0 else texto


def _cifras_del_caso(problem_case: dict | None) -> set[str]:
    """Fallo 132 (parte 3): las cifras de los datos del cliente («45 tiendas»), que también valen en el resumen."""
    valores = [d.get("value") for d in (problem_case or {}).values() if isinstance(d, dict)]
    return _cifras(" ".join(" ".join(map(str, v)) if isinstance(v, list) else str(v) for v in valores if v))


def _sin_cifras_nuevas(texto: str, validas: set[str], donde: str) -> str:
    """Quita las frases con cifras que no están en el texto del que salen (validas = _cifras de ese texto)."""
    buenas = []
    for frase in re.split(r"(?<=[.!?…])\s+", texto or ""):
        nuevas = _cifras(frase) - validas
        if nuevas:
            logger.warning("[resumen] %s: fuera una frase con cifras que no están en su texto (%s)", donde,
                           ", ".join(sorted(nuevas)))
        elif frase.strip():
            buenas.append(frase.strip())
    return " ".join(buenas)


def _frase(valor, tope: int, validas: set[str], donde: str) -> str:
    return _sin_cifras_nuevas(_palabras(valor, tope), validas, donde)


def _lista(valor, cuantos: int, tope: int, validas: set[str], donde: str) -> list[str]:
    valores = [valor] if isinstance(valor, str) else valor if isinstance(valor, list) else []
    return [x for x in (_frase(v, tope, validas, donde) for v in valores[:cuantos]) if x]


def _resumen_de_seccion(datos, validas: set[str], donde: str) -> dict:
    """Los 5 campos del «Resumen» de un desplegable, con sus topes; {} si no queda nada."""
    if not isinstance(datos, dict):
        return {}
    r: dict = {campo: _frase(datos.get(campo), tope, validas, f"{donde} · {campo}") for campo, tope in CAMPOS.items()}
    r.update({campo: _lista(datos.get(campo), n, tope, validas, f"{donde} · {campo}")
              for campo, (n, tope) in LISTAS.items()})
    return r if any(r.values()) else {}


def _nombres_de_servicio(servicio: str) -> set[str]:
    """Fallo 132 (parte 3): cómo puede nombrar un texto un servicio del inventario: entero, sin la nube delante y por su
    marca. 'Microsoft Fabric OneLake' → Microsoft Fabric OneLake, Fabric OneLake, Microsoft Fabric, Fabric y OneLake.
    Nunca una palabra genérica suelta («Data», «Lake», «Delta»)."""
    entero = re.sub(r"\s*\([^)]*\)", "", servicio or "").strip()
    sin_nube = rb._SIN_NUBE.sub("", entero).strip()
    palabras = sin_nube.split()
    sueltas = lambda *ps: not all(_normalize_text(p) in _NO_SUELTAS for p in ps)
    nombres = {entero}
    if sin_nube != entero and palabras:
        if sueltas(*palabras):
            nombres.add(sin_nube)
        if len(palabras) > 1 and sueltas(palabras[0]):
            nombres |= {f"{entero[:len(entero) - len(sin_nube)].strip()} {palabras[0]}", palabras[0]}
    nombres |= {p for p in palabras if re.fullmatch(r"[A-Z][a-z]+[A-Z]\w*", p)}  # las marcas pegadas: OneLake
    return {n for n in nombres if len(_normalize_text(n)) >= 2}


def _servicio_nombrado(texto: str, filas: list[dict], trozos: list[str]) -> str | None:
    """El servicio del inventario que nombra un texto, entero, sin la nube o por su marca, o None. Fallo 132 (parte 3):
    pueden salir la nube y lo que el cliente ya usa: las fuentes del inventario y lo que nombran sus datos."""
    clave = _normalize_text(texto)
    for fila in filas:
        servicio = fila.get("servicio", "")
        if "fuente" in _normalize_text(fila.get("capa", "")) or inventario.lo_tiene_el_cliente(servicio, trozos):
            continue
        for nombre in _nombres_de_servicio(servicio):
            n = _normalize_text(nombre)
            if (not inventario.lo_tiene_el_cliente(nombre, trozos)
                    and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", clave)):
                return servicio
    return None


def _llamar(descripcion: str, esperado: str) -> str:
    """Una llamada del resumidor, en su propia crew; devuelve el texto tal cual."""
    datos = load_agents_config().get("section_summary_agent", {})
    agente = Agent(role=datos.get("role"), goal=datos.get("goal"), backstory=datos.get("backstory"),
                   llm=gateway.build_llm(gateway.modelo("resumen_secciones"), max_tokens=MAX_TOKENS),
                   verbose=False, allow_delegation=False)
    tarea = Task(description=descripcion, expected_output=esperado, agent=agente)
    resultado = Crew(agents=[agente], tasks=[tarea], process=Process.sequential, cache=False).kickoff()
    return getattr(resultado, "raw", None) or str(resultado or "")


def _textos_propuesta(soup) -> dict[str, str]:
    return {clave: texto for clave, _, div_id, _ in SECCIONES if (texto := _texto(soup, div_id))}


# ── Llamada 1: las secciones de la propuesta ──

def resumir_propuesta(problem_case: dict | None = None) -> dict:
    """Llamada 1 (en un hilo, a la vez que el roadmap): {clave de sección: {para_todos, decision_principal, …}}.
    {} si no hay secciones, si la llamada falla o si su JSON no se lee: los bloques salen sin texto y las secciones,
    sin interruptor."""
    try:
        textos = _textos_propuesta(parse_html(html_del_informe(rb.REPORT_PATH)))
        if not textos:
            logger.warning("[resumen] No hay secciones de la propuesta que resumir")
            return {}
        secciones = "\n\n".join(f"### {clave}\n{texto}" for clave, texto in textos.items())
        descripcion = (
            "Resume cada sección de la propuesta que viene abajo, para la página de resumen del informe. " + _REGLAS
            + "Devuelve solo un objeto JSON, sin texto antes ni después, con una entrada por sección y sus claves "
            f"({', '.join(textos)}):\n"
            '{"<clave>": {"para_todos": "…", "decision_principal": "…", "por_que": "…", "piezas_clave": ["…"], '
            '"riesgos": ["…"], "siguiente_paso": "…"}}\n'
            "para_todos: 1 o 2 frases, 45 palabras como máximo, para alguien sin perfil técnico: qué se propone y "
            "para qué, sin nombres de servicios, sin siglas sin explicar y sin cifras. " + _TOPES + "\n\n"
            f"{contexto_cliente(problem_case or {})}\n\nSECCIONES DE LA PROPUESTA:\n{secciones}")
        bruto = _llamar(descripcion, "Un objeto JSON con el resumen de cada sección.")
        datos = _leer_json(bruto, ETIQUETA, "las secciones de la propuesta salen sin resumen")
        resultado = {}
        del_caso = _cifras_del_caso(problem_case)
        for clave, texto in textos.items():
            entrada = datos.get(clave) if isinstance(datos, dict) else None
            validas = _cifras(texto) | del_caso
            r = _resumen_de_seccion(entrada, validas, clave)
            para_todos = _frase(entrada.get("para_todos"), PARA_TODOS, validas, f"{clave} · para_todos") \
                if isinstance(entrada, dict) else ""
            if r or para_todos:
                resultado[clave] = {**r, "para_todos": para_todos}
        if resultado:
            print(f"[resumen] Llamada 1: {len(resultado)} de {len(textos)} secciones de la propuesta resumidas")
        else:
            logger.error("[resumen] Llamada 1 sin resúmenes: los bloques de la pestaña Resumen salen sin texto y las "
                         "secciones de la propuesta, sin interruptor")
        return resultado
    except Exception as exc:
        logger.error("[resumen] La llamada 1 ha fallado (%s: %s): las secciones de la propuesta salen sin resumen",
                     type(exc).__name__, exc)
        return {}
    finally:
        progreso.avanzar("Resumen: secciones de la propuesta")  # desde el hilo solo se anota; lo pinta la sesión


# ── Llamada 2: general, puntos clave, fases del roadmap y CRISP-DM ──

def _resumir_proyecto(soup, propuesta: dict, estados: dict, problem_case: dict | None, filas: list[dict]) -> dict:
    textos = _textos_propuesta(soup)
    fases = {clave: texto for clave, div_id, _ in FASES
             if estados.get(div_id, SI) == SI and (texto := _texto(soup, div_id))}
    ya = [rb.NOMBRES_FASES[n] for n, d in enumerate(rb._ROADMAP_IDS) if estados.get(d) == YA]
    partes = []
    for clave, texto in textos.items():
        r = propuesta.get(clave) or {}
        corto = " ".join(x for x in (r.get("para_todos"), r.get("decision_principal"), r.get("por_que")) if x)
        partes.append(f"### {clave}\n{corto or texto}")
    partes += [f"### {clave} (fase del roadmap)\n{texto}" for clave, texto in fases.items()]
    descripcion = (
        "Con lo que ya se ha escrito del proyecto (abajo), prepara la página de resumen del informe para la dirección "
        "del cliente. " + _REGLAS
        + "Devuelve solo un objeto JSON, sin texto antes ni después:\n"
        '{"resumen_general": "…", "puntos_clave": [{"texto": "…", "seccion": "…"}], '
        f'"fases": {{"<clave de fase>": {{"puntos": ["…", "…"], {_FORMATO_SECCION[1:-1]}}}}}, '
        '"crisp": {' + ", ".join(f'"{c}": ["…", "…"]' for c, _, _ in CRISP) + "}}\n"
        "resumen_general: 60 palabras como máximo: qué se propone y qué consigue el cliente.\n"
        "puntos_clave: 4 o 5 de todo el proyecto, de 25 palabras como máximo, para alguien sin perfil técnico. "
        # Fallo 132 (parte 3): la misma regla que aplica _servicio_nombrado(), para que casi nunca haya que quitar nada
        "Pueden nombrar la nube elegida, las herramientas que el cliente ya usa según sus datos y conceptos como "
        "Lakehouse, ERP o CRM, diciendo qué hacen; ningún otro servicio de la solución, ni siquiera a medias (ni su "
        "marca ni su nombre corto: «Fabric», «OneLake», «Purview»…), y ninguna sigla sin explicar. En «seccion», la "
        f"clave del texto del que sale (una de: {', '.join(list(textos) + list(fases))}) o null.\n"
        f"fases: solo estas: {', '.join(fases) or 'ninguna'}. puntos: 2, " + _VINETAS + " " + _TOPES + "\n"
        "crisp: 2 puntos por cada fase de la metodología CRISP-DM aplicada a este proyecto ("
        + ", ".join(f"{c}: {n}" for c, n, _ in CRISP) + "), " + _VINETAS + "\n\n"
        + (f"El cliente ya tiene (se reutiliza): {', '.join(ya)}.\n\n" if ya else "")
        + f"{contexto_cliente(problem_case or {})}\n\nLO ESCRITO DEL PROYECTO:\n" + "\n\n".join(partes))
    try:
        bruto = _llamar(descripcion, "Un objeto JSON con el resumen general, los puntos clave, las fases y CRISP-DM.")
    except Exception as exc:
        logger.error("[resumen] La llamada 2 ha fallado (%s: %s): sin puntos clave ni resumen de las fases",
                     type(exc).__name__, exc)
        return {}
    datos = _leer_json(bruto, ETIQUETA, "sin puntos clave ni resumen de las fases")
    if not isinstance(datos, dict) or not datos:
        logger.error("[resumen] Llamada 2 sin JSON: la pestaña Resumen sale sin puntos clave y las fases del roadmap, "
                     "sin interruptor")
        return {}
    del_caso = _cifras_del_caso(problem_case)  # fallo 132 (parte 3)
    todo = _cifras(" ".join(list(textos.values()) + list(fases.values()))) | del_caso
    resultado = {"general": _frase(datos.get("resumen_general"), GENERAL, todo, "resumen general"),
                 "puntos": [], "fases": {}, "crisp": {}}
    trozos = inventario.trozos_del_cliente(problem_case)
    for punto in (datos.get("puntos_clave") or [])[:PUNTOS_CLAVE[0]]:
        texto, seccion = (punto.get("texto"), punto.get("seccion")) if isinstance(punto, dict) else (punto, None)
        texto = _frase(texto, PUNTOS_CLAVE[1], todo, "punto clave")
        servicio = _servicio_nombrado(texto, filas, trozos)
        if servicio:
            logger.warning("[resumen] Fuera un punto clave que nombra un servicio (%s)", servicio)
        elif texto:
            resultado["puntos"].append((texto, seccion if isinstance(seccion, str) else None))
    if len(resultado["puntos"]) < 4:  # fallo 132 (parte 3): sin llamadas nuevas, con los que queden
        logger.warning("[resumen] Solo %d puntos clave tras quitar los que nombran servicios", len(resultado["puntos"]))
    for clave, texto in fases.items():
        entrada = (datos.get("fases") or {}).get(clave)
        validas = _cifras(texto) | del_caso
        r = _resumen_de_seccion(entrada, validas, f"fase {clave}")
        puntos = _lista(entrada.get("puntos"), *PUNTOS_FASE, validas, f"fase {clave} · puntos") \
            if isinstance(entrada, dict) else []
        if r or puntos:
            resultado["fases"][clave] = {**r, "puntos": puntos}
    for clave, _, _ in CRISP:
        puntos = _lista((datos.get("crisp") or {}).get(clave), *PUNTOS_FASE, todo, f"crisp {clave}")
        if puntos:
            resultado["crisp"][clave] = puntos
    print(f"[resumen] Llamada 2: {len(resultado['puntos'])} puntos clave, {len(resultado['fases'])} de {len(fases)} "
          f"fases y {len(resultado['crisp'])} de {len(CRISP)} fases CRISP-DM")
    return resultado


# ── La pestaña Resumen en el informe ──

def _nueva(soup, nombre: str, texto: str | None = None, **attrs):
    tag = soup.new_tag(nombre, attrs={k.rstrip("_").replace("_", "-"): v for k, v in attrs.items()})
    if texto:
        tag.string = texto
    return tag


def _en_lugar_de(soup, viejo, href: str | None):
    """El nodo copiado como enlace (con destino) o como div (sin él), con sus clases y su contenido."""
    nuevo = _nueva(soup, "a" if href else "div", class_=viejo.get("class") or [])
    if href:
        nuevo["href"] = href
    for hijo in list(viejo.contents):
        nuevo.append(hijo.extract())
    viejo.replace_with(nuevo)
    return nuevo


def _puntos(soup, tag, puntos: list[str]) -> None:
    if puntos:
        ul = _nueva(soup, "ul", class_="resumen-puntos-fase")
        for p in puntos:
            ul.append(_nueva(soup, "li", p))
        tag.append(ul)


def _volver(soup):
    return _nueva(soup, "a", "← Volver al resumen", class_="volver-resumen", href="#resumen")


def _interruptor_y_resumen(soup, card, r: dict) -> None:
    """«Detalle | Resumen» (Detalle por defecto) y el resumen de la sección, delante de su texto."""
    body = card.find("div", class_="section-body")
    prose = body.find("div", class_="prose") if body else None
    if prose is None:
        return
    grupo = _nueva(soup, "div", class_="vista-seccion", role="group", aria_label="Vista de la sección")
    for vista, texto, pulsado in (("detalle", "Detalle", "true"), ("resumen", "Resumen", "false")):
        grupo.append(_nueva(soup, "button", texto, class_="vista-btn", type="button", data_vista=vista,
                            aria_pressed=pulsado))
    resumen = _nueva(soup, "div", class_="resumen-seccion")
    for campo, titulo in TITULOS_RESUMEN:
        valor = r.get(campo)
        if not valor:
            continue
        resumen.append(_nueva(soup, "h4", titulo))
        if isinstance(valor, list):
            ul = _nueva(soup, "ul")
            for v in valor:
                ul.append(_nueva(soup, "li", v))
            resumen.append(ul)
        else:
            resumen.append(_nueva(soup, "p", valor))
    prose.insert_before(grupo)
    prose.insert_before(resumen)


def _texto_costes(cifras: dict) -> str:
    if not cifras.get("total"):
        return ""
    partes = [f"{cifras.get('total_etiqueta') or 'Total'}: {cifras['total']}"]
    if cifras.get("mvp"):
        partes.append(f"MVP: {cifras['mvp']}")
    if cifras.get("al_mes"):
        partes.append(f"al mes, lo que queda funcionando: {cifras['al_mes']}")
    if cifras.get("meses"):
        partes.append(f"plazo: {cifras['meses']}")
    return " · ".join(partes) + "."


def _crisp_lee_datos(soup) -> bool:
    """La pestaña CRISP-DM de Asier lee sus textos de #crisp-datos si algún script suyo lo nombra."""
    return any(_LEE_CRISP_DATOS.search(s.decode_contents()) for s in soup.find_all("script")  # get_text() salta el JS
               if (s.get("type") or "").lower() in ("", "text/javascript", "application/javascript", "module"))


def montar_resumen(soup, propuesta: dict, proyecto: dict, cifras: dict, estados: dict) -> None:
    """Monta la pestaña Resumen, los interruptores, los «← Volver al resumen» y lo de CRISP-DM en el informe."""
    for viejo in soup.select(".volver-resumen, .vista-seccion, .resumen-seccion"):  # por si se monta dos veces
        viejo.decompose()
    reutilizadas = soup.select_one("#tab-roadmap p.rdm-ya-implantado")
    if reutilizadas is not None:
        reutilizadas["id"] = REUTILIZADAS

    # Destinos que existen: solo de lo que se ha generado (nunca un enlace roto)
    enlaces = {}
    for clave, _, div_id, destino in SECCIONES + [COSTES]:
        if soup.find(id=destino) is not None and _texto(soup, div_id):
            enlaces[clave] = destino
    for clave, div_id, destino in FASES:
        if soup.find(id=destino) is not None and soup.find(id=f"card-{div_id}") is not None and _texto(soup, div_id):
            enlaces[clave] = destino
        elif estados.get(div_id) == YA and reutilizadas is not None:
            enlaces[clave] = REUTILIZADAS

    # CRISP-DM (pestaña de Asier): ids en sus botones si faltan, sus textos en #crisp-datos y, si su JS no lo lee,
    # la pestaña oculta
    tab_crisp = soup.find(id="tab-crispdm")
    crisp_destinos = {}
    if tab_crisp is not None:
        for n, (clave, _, id_fijo) in enumerate(CRISP, start=1):
            boton = tab_crisp.find(class_="crisp-phase", attrs={"data-phase": str(n)})
            if boton is not None:
                if not boton.get("id"):
                    boton["id"] = id_fijo
                crisp_destinos[clave] = boton["id"]
        if proyecto.get("crisp"):
            datos = {str(n): {"titulo": f"{n:02d} · {nombre}", "actividades": proyecto["crisp"][clave]}
                     for n, (clave, nombre, _) in enumerate(CRISP, start=1) if clave in proyecto["crisp"]}
            script = soup.find(id=CRISP_DATOS) or _nueva(soup, "script", type="application/json", id=CRISP_DATOS)
            script.clear()
            script.append(Script(json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")))
            if script.parent is None:
                tab_crisp.append(script)
        boton_tab = soup.find("button", class_="tab-btn", onclick=re.compile(r"'crispdm'"))
        visible = _crisp_lee_datos(soup)
        for tag in (tab_crisp, boton_tab):
            if tag is None:
                continue
            if visible:
                tag.attrs.pop("hidden", None)
            else:
                tag["hidden"] = ""
        if visible:
            tab_crisp.insert(0, _volver(soup))
        else:
            logger.warning("[resumen] La pestaña CRISP-DM no lee #crisp-datos: se oculta, y sus fases de la pestaña "
                           "Resumen van sin enlace")
            crisp_destinos = {}

    # Interruptor «Detalle | Resumen» y «← Volver al resumen» en cada desplegable de destino
    for clave, _, _, destino in SECCIONES + [COSTES]:
        card = soup.find(id=destino).find_parent(class_="section-card") if clave in enlaces else None
        if card is not None and propuesta.get(clave):
            _interruptor_y_resumen(soup, card, propuesta[clave])
    for clave, _, destino in FASES:
        card = soup.find(id=destino).find_parent(class_="section-card") if enlaces.get(clave) == destino else None
        if card is not None and (proyecto.get("fases") or {}).get(clave):
            _interruptor_y_resumen(soup, card, proyecto["fases"][clave])
    for destino in set(enlaces.values()):
        tag = soup.find(id=destino)
        if destino == REUTILIZADAS:
            tag.insert_after(_volver(soup))
        else:
            card = tag.find_parent(class_="section-card")
            prose = card.find("div", class_="section-body").find("div", class_="prose")
            (card.find(class_="vista-seccion") or prose).insert_before(_volver(soup))

    # La pestaña Resumen
    general = soup.find(id="resumen-general")
    if general is not None:
        general.clear()
        if proyecto.get("general"):
            general.string = proyecto["general"]
    bloques = soup.find(id="resumen-bloques")
    if bloques is not None:
        bloques.clear()
        for clave, titulo, _, destino in SECCIONES + [COSTES]:
            texto = _texto_costes(cifras) if clave == "costes" else (propuesta.get(clave) or {}).get("para_todos")
            bloque = _nueva(soup, "a" if clave in enlaces else "div", class_="resumen-bloque")
            if clave in enlaces:
                bloque["href"] = f"#{enlaces[clave]}"
            bloque.append(_nueva(soup, "h3", titulo))
            if texto:
                bloque.append(_nueva(soup, "p", texto))
            bloques.append(bloque)
    lista = soup.find(id="resumen-puntos")
    if lista is not None:
        lista.clear()
        for texto, seccion in proyecto.get("puntos") or []:
            li = _nueva(soup, "li")
            li.append(_nueva(soup, "a", texto, href=f"#{enlaces[seccion]}") if seccion in enlaces else texto)
            lista.append(li)
        _mostrar(soup.find(id="resumen-puntos-parte"), bool(proyecto.get("puntos")))

    # El flujo del roadmap, tal como está en su pestaña (sin las «no», con las «ya lo tiene» atenuadas)
    flujo = soup.find(id="resumen-flujo")
    original = soup.select_one("#tab-roadmap .rdm-pipeline")
    if flujo is not None:
        flujo.clear()
        if original is not None:
            copia = copy.copy(original)
            nodos = copia.find_all("div", class_="rdm-pipe-node")
            presentes = [(c, d) for c, d, _ in FASES if estados.get(d, SI) != NO]
            if len(nodos) != len(presentes):
                logger.warning("[resumen] El flujo del roadmap tiene %d nodos y se esperaban %d: va sin enlaces",
                               len(nodos), len(presentes))
                presentes = [(None, None)] * len(nodos)
            for nodo, (clave, _) in zip(nodos, presentes):
                destino = enlaces.get(clave)
                nuevo = _en_lugar_de(soup, nodo, f"#{destino}" if destino else None)
                nuevo.attrs.pop("onclick", None)
                _puntos(soup, nuevo, ((proyecto.get("fases") or {}).get(clave) or {}).get("puntos") or [])
            flujo.append(copia)
            for linea in soup.select("#tab-roadmap p.rdm-linea-fases"):
                otra = copy.copy(linea)
                otra.attrs.pop("id", None)
                flujo.append(otra)
        _mostrar(soup.find(id="resumen-flujo-parte"), original is not None)

    # El diagrama CRISP-DM de Asier, copiado sin ids ni data-phase (sin clics que abran su panel)
    crisp = soup.find(id="resumen-crisp")
    diagrama = soup.select_one("#tab-crispdm .crisp-diagram")
    if crisp is not None:
        crisp.clear()
        if diagrama is not None:
            copia = copy.copy(diagrama)
            for tag in [copia] + copia.find_all(True):
                tag.attrs.pop("id", None)
                tag.attrs.pop("data-phase", None)
            for boton in copia.find_all(class_="crisp-phase"):
                numero = next((int(m.group(1)) for c in boton.get("class", []) if (m := re.fullmatch(r"phase-(\d)", c))), 0)
                clave = CRISP[numero - 1][0] if 1 <= numero <= len(CRISP) else None
                destino = crisp_destinos.get(clave)
                nuevo = _en_lugar_de(soup, boton, f"#{destino}" if destino else None)
                _puntos(soup, nuevo, (proyecto.get("crisp") or {}).get(clave) or [])
            crisp.append(copia)
        _mostrar(soup.find(id="resumen-crisp-parte"), diagrama is not None)


def _mostrar(tag, si: bool) -> None:
    if tag is None:
        return
    if si:
        tag.attrs.pop("hidden", None)
    else:
        tag["hidden"] = ""


def completar_resumen(propuesta: dict | None, problem_solution: dict | None = None, problem_case: dict | None = None,
                      fases: dict | None = None) -> None:
    """Llamada 2 y la pestaña Resumen en el informe generado. propuesta: lo de resumir_propuesta(); fases: las del
    bloque FASES DEL ROADMAP (fallo 131), {div_id: (estado, frase)} o None (las 7). Si algo falla, el informe sigue."""
    solucion = problem_solution if isinstance(problem_solution, dict) else {}
    estados = {d: e for d, (e, _) in (fases or {}).items()}
    try:
        soup = parse_html(html_del_informe(rb.REPORT_PATH))
        try:
            filas = [f for f in solucion.get("inventario") or [] if isinstance(f, dict)]
            proyecto = _resumir_proyecto(soup, propuesta or {}, estados, problem_case, filas)
        finally:
            progreso.avanzar("Resumen: puntos clave y fases")
        montar_resumen(soup, propuesta or {}, proyecto, solucion.get("cifras_costes") or {}, estados)
        rb.REPORT_PATH.write_text(str(soup), encoding="utf-8")
        print(f"[resumen] Pestaña Resumen montada en {rb.REPORT_PATH}")
    except Exception as exc:
        logger.error("[resumen] No se ha podido montar la pestaña Resumen (%s: %s): el informe sigue sin ella",
                     type(exc).__name__, exc)
