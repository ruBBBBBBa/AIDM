"""Fallo 67: el agente de plataforma. Después del discovery, decide en qué hiperescalar se diseña la solución.

Compara AWS, Google Cloud, Azure, Oracle (OCI) e IBM Cloud con los datos del caso y valora si al cliente le conviene
quedarse en la nube que ya usa o cambiar y formar a su equipo en la nueva. La nube que sale es la plataforma objetivo
de todo el informe: la arquitectura, los costes y el roadmap se diseñan para ella (plataforma_objetivo en el caso).

La nota la pone Python con data_structures/hiperescalares.yaml (pesos, datos de cada nube y palabras que delatan lo
que usa el cliente); el LLM solo la explica. Así la recomendación es la misma con cualquier modelo y se puede revisar.
"""
import csv
import json
import re
from pathlib import Path

import yaml
import decisiones


import gateway
import utils
from utils import _normalize_text, contexto_cliente, es_valor_de_relleno, md_to_html, parse_html

CONFIG = Path("data_structures/hiperescalares.yaml")
REPORT_PATH = Path("data_structures/solution_builder_report.html")
TARIFAS = Path("bd/costes_perfil.csv") #Fallo 71: La misma tabla de tarifas que usan los costes
SECCION = "content-platform"
CRITERIOS = ("continuidad", "ecosistema", "datos_ia", "cumplimiento", "talento")
NOMBRES_CRITERIOS = {"continuidad": "Continuidad", "ecosistema": "Ecosistema", "datos_ia": "Datos e IA",
                     "cumplimiento": "Cumplimiento", "talento": "Talento"}
# Campos del caso donde el cliente cuenta qué usa y qué sabe su equipo
CAMPOS_DEL_CLIENTE = ("cloud_provider", "stack_tecnologico_actual", "modelo_de_despliegue", "tamano_equipo_tecnico",
                      "tiene_equipo_datos", "herramienta_de_visualizacion", "madurez_analitica_actual",
                      "regulacion_y_compliance", "enfoque_ai", "volumen_de_datos", "sector", "evidencias")


# Fallo 69: palabras que, en el mismo campo que una herramienta o una nube, pueden cambiar lo que significa nombrarla
# («no usan Power BI», «dejaron Oracle»). Solo esos campos se preguntan al modelo de decisión.
_CAMBIO = re.compile(r"(?<![a-z])(no|ni|sin|nunca|nada de|ya no|dej\w*|abandon\w*|antes|antigu\w*|anterior\w*|"
                     r"migr\w*|sal(?:ir|iendo|imos|en) de|sustitu\w*|reemplaz\w*|quit\w*|jubil\w*|"
                     r"en (?:vez|lugar) de|hasta (?:el|hace)|not|no longer|never|without|used to|moving (?:away|off)|"
                     r"replac\w*|instead of|former\w*|previous\w*|dropp\w*|leav\w*|phas\w* out)(?![a-z])"
                     r"|[a-z]+n't")
NO_CUENTA = {"no_la_usa": "dice que no la usa", "la_deja": "la ha dejado o la va a dejar"}
# Fallo 70: el modelo de despliegue que recomienda el modelo de decisión
DESPLIEGUES = {"cloud": "En la nube", "hibrido": "Híbrido", "on_premises": "On-premises (en sus servidores)"}


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def _textos(problem_case: dict, campos=CAMPOS_DEL_CLIENTE) -> dict[str, str]:
    """El texto original de cada campo con dato real (las evidencias, juntas)."""
    textos = {}
    for campo in campos:
        valor = (problem_case or {}).get(campo)
        valor = valor.get("value") if isinstance(valor, dict) else valor
        valores = valor if isinstance(valor, list) else [valor]
        partes = [str(v) for v in valores if isinstance(v, str) and not es_valor_de_relleno(v)]
        if partes:
            textos[campo] = " | ".join(partes)
    return textos


def _palabra(p: str) -> str:
    return rf"(?<![a-z0-9]){re.escape(_normalize_text(p))}(?![a-z0-9])"


def _nombre_en(texto: str, palabra: str) -> str:
    """La palabra tal como la escribe el cliente («Power BI»), para preguntar por ella."""
    m = re.search(rf"(?<!\w){re.escape(palabra)}(?!\w)", texto, re.IGNORECASE)
    return m.group(0) if m else palabra


def _sin_lo_que_no_usa(problem_case: dict, config: dict) -> tuple[str, dict, list[str]]:
    """Fallo 69: el texto del caso (normalizado) sin las herramientas y nubes que el cliente nombra para decir que no
    las usa o que las deja, según el modelo de decisión; lo que se ha quitado ({nombre: opción}) y las nubes que
    deja. Solo se pregunta por los campos con una negación o un cambio (_CAMBIO); sin el modelo, no se quita nada."""
    palabras = [p for e in config["ecosistemas"].values() for p in e.get("palabras", [])]
    claves = {p: prov for prov, datos in config["proveedores"].items() for p in datos.get("claves", [])}
    partes, quitadas, dejadas = [], {}, []
    for campo, original in _textos(problem_case).items():
        texto = _normalize_text(original)
        nombradas = [p for p in dict.fromkeys(palabras + list(claves)) if re.search(_palabra(p), texto)]
        if nombradas and _CAMBIO.search(texto):
            nombres = {p: _nombre_en(original, p) for p in nombradas}
            respuestas = decisiones.usos(campo, original, list(dict.fromkeys(nombres.values()))) or {}
            for p, nombre in nombres.items():
                opcion, confianza = respuestas.get(nombre, (None, 0.0))
                if opcion in NO_CUENTA and confianza >= decisiones.CONFIANZA_MINIMA:
                    texto = re.sub(_palabra(p), " ", texto)
                    quitadas[nombre] = opcion
                    if opcion == "la_deja" and p in claves and claves[p] not in dejadas:
                        dejadas.append(claves[p])
        partes.append(texto)
    return " | ".join(partes), quitadas, dejadas



def _texto(problem_case: dict, campos=CAMPOS_DEL_CLIENTE) -> str:
    """Los campos del caso con dato real, juntos y normalizados (minúsculas y sin tildes)."""
    partes = []
    for campo in campos:
        valor = (problem_case or {}).get(campo)
        valor = valor.get("value") if isinstance(valor, dict) else valor
        valores = valor if isinstance(valor, list) else [valor]
        partes += [str(v) for v in valores if isinstance(v, str) and not es_valor_de_relleno(v)]
    return _normalize_text(" | ".join(partes))


def _encontradas(texto: str, palabras: list[str]) -> list[str]:
    """Las palabras (o frases) que aparecen enteras en el texto."""
    return [p for p in palabras
            if re.search(rf"(?<![a-z0-9]){re.escape(_normalize_text(p))}(?![a-z0-9])", texto)]


def _personas(problem_case: dict, config: dict) -> tuple[int, bool]:
    """Cuántas personas del cliente habría que formar (y si el dato sale del caso o es el de por defecto)."""
    texto = _texto(problem_case, ("tamano_equipo_tecnico", "tiene_equipo_datos"))
    numeros = {"una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6}
    m = re.search(r"\b(\d+|una|un|dos|tres|cuatro|cinco|seis)\s+(?:personas?|tecnicos?|ingenier|desarrollador|"
                  r"informatic|perfiles?|empleados|analistas?)", texto)
    if m:
        return int(numeros.get(m.group(1), m.group(1))), True
    return int((config.get("formacion") or {}).get("personas_si_no_se_sabe", 2)), False


def senales(problem_case: dict, config: dict | None = None) -> dict:
    """Lo que el caso dice que importa para elegir nube: las que ya usa, sus ecosistemas, si pide ENS o soberanía."""
    config = config or _config()
    texto = _texto(problem_case)
    usadas, quitadas, dejadas = _sin_lo_que_no_usa(problem_case, config)  # fallo 69
    actuales = [clave for clave, p in config["proveedores"].items() if _encontradas(usadas, p.get("claves", []))]
    ecosistemas = {clave: _encontradas(usadas, e.get("palabras", [])) for clave, e in config["ecosistemas"].items()}
    personas, de_caso = _personas(problem_case, config)
    return {"actuales": actuales,
            "no_cuentan": quitadas, "dejando": [c for c in dejadas if c not in actuales],

            "ecosistemas": {k: v for k, v in ecosistemas.items() if v},
            "pide_ens": bool(_encontradas(texto, config.get("pide_ens", []))),
            "pide_soberania": bool(_encontradas(texto, config.get("pide_soberania", []))),
            "personas": personas, "personas_del_caso": de_caso}


def despliegue(problem_case: dict) -> dict | None:
    """Fallo 70: el modelo de despliegue que conviene al cliente según el modelo de decisión ({opcion, confianza,
    cliente}), o None si no lo sabe decir, no está seguro o no está disponible (entonces no se recomienda ninguno)."""
    textos = _textos(problem_case, decisiones.CAMPOS_DESPLIEGUE)
    resultado = decisiones.despliegue_recomendado(textos) if textos else None
    if not resultado or resultado[0] not in DESPLIEGUES or resultado[1] < decisiones.CONFIANZA_MINIMA:
        return None
    return {"opcion": resultado[0], "confianza": resultado[1], "cliente": textos.get("modelo_de_despliegue", "")}

def _tarifa_migracion(e: dict) -> float:
    """Fallo 71: la tarifa (€/h) del perfil que migra, de bd/costes_perfil.csv; si no está, la del yaml."""
    try:
        with open(TARIFAS, encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f, skipinitialspace=True):
                if _normalize_text(fila.get("Perfil") or "") == _normalize_text(e.get("perfil_migracion", "")):
                    return float(fila[f"{e.get('nivel_migracion', 'Middle')} (€/h)"])
    except (OSError, KeyError, ValueError):
        pass
    return float(e.get("coste_hora_migracion_eur", 30))


def _frases(texto: str) -> list[str]:
    return [f.strip() for f in re.split(r"[.;|\n]+", texto or "") if f.strip()]


def existentes(problem_case: dict, resultado: dict, config: dict) -> list[dict]:
    """Fallo 71: lo que el cliente ya tiene en una nube que no es la recomendada, y si se mantiene o se migra.

    Se mantiene e integra salvo que el cliente pida migrarlo o que migrar compense mucho: la nube recomendada le saca
    al menos existente.ventaja_minima puntos y el ahorro del horizonte (operar una nube menos) es al menos
    existente.ahorro_sobre_coste veces lo que cuesta migrar. Lo que dice el cliente se busca por palabras
    (existente.mantener y existente.migrar) en las frases donde nombra esa nube."""
    e = config.get("existente") or {}
    rec = resultado["recomendado"]
    otras = [c for c in resultado["senales"]["actuales"] if c != rec]
    if not otras:
        return []
    tarifa = _tarifa_migracion(e)
    anios = e.get("horizonte_anios", 3)
    ahorro = round(e.get("horas_operar_otra_nube_mes", 0) * 12 * anios
                   * (config.get("formacion") or {}).get("coste_hora_eur", 35))
    genericos = {_normalize_text(n) for n in e.get("nombres_de_nube", [])}
    frases = [f for texto in _textos(problem_case).values() for f in _frases(texto)]
    lista = []
    for clave in otras:
        claves = config["proveedores"][clave].get("claves", [])
        suyas = [f for f in frases if any(re.search(_palabra(k), _normalize_text(f)) for k in claves)]
        servicios = list(dict.fromkeys(_nombre_en(f, k) for f in suyas for k in claves
                                       if _normalize_text(k) not in genericos and re.search(_palabra(k), _normalize_text(f))))
        horas = max(len(servicios), 1) * e.get("horas_migracion_por_servicio", 0)
        coste = round(horas * tarifa)
        ventaja = round(resultado["puntos"][rec]["total"] - resultado["puntos"][clave]["total"], 1)
        compensa = ventaja >= e.get("ventaja_minima", 25) and ahorro >= e.get("ahorro_sobre_coste", 1.5) * coste
        dicho = " | ".join(_normalize_text(f) for f in suyas)
        if any(_normalize_text(p) in dicho for p in e.get("mantener", [])):
            dice = "mantener"
        elif any(_normalize_text(p) in dicho for p in e.get("migrar", [])):
            dice = "migrar"
        else:
            dice = None
        lista.append({"clave": clave, "nombre": config["proveedores"][clave]["nombre"], "servicios": servicios,
                      "decision": dice or ("migrar" if compensa else "mantener"), "lo_pide_el_cliente": bool(dice),
                      "compensa": compensa, "ventaja": ventaja, "horas_migracion": horas, "tarifa": tarifa,
                      "coste_migracion_eur": coste, "ahorro_eur": ahorro, "anios": anios})
    return lista


def puntuar(problem_case: dict, config: dict | None = None) -> dict:
    """La nota de cada nube (de 0 a 100, repartida por criterios) y la decisión: mantener, cambiar o elegir."""
    config = config or _config()
    datos = senales(problem_case, config)
    pesos = config["pesos"]
    total_pesos = sum(pesos[c] for c in CRITERIOS)
    puntos = {}
    bloqueos = {}
    for clave, p in config["proveedores"].items():
        # Lo que el cliente ya usa y esta nube no admite (afinidad 0), como Databricks en OCI o en IBM Cloud
        bloqueos[clave] = [config["ecosistemas"][e]["nombre"] for e in datos["ecosistemas"]
                           if config["ecosistemas"][e]["afinidad"].get(clave, 0) == 0]
        notas = {"continuidad": 1.0 if clave in datos["actuales"] else 0.0,
                 "datos_ia": 0.0 if bloqueos[clave] else p.get("datos_ia", 0) / 5,
                 "talento": p.get("talento", 0) / 5}
        if datos["ecosistemas"]:
            afinidades = [config["ecosistemas"][e]["afinidad"].get(clave, 0) / 5 for e in datos["ecosistemas"]]
            notas["ecosistema"] = sum(afinidades) / len(afinidades)
        else:
            notas["ecosistema"] = 0.5  # sin pistas, no favorece a nadie
        ens = p.get("ens_alta")
        nota_ens = 1.0 if ens else (0.0 if ens is False else (0.25 if datos["pide_ens"] else 0.5))
        notas["cumplimiento"] = (0.5 if p.get("region_espana") else 0.0) + 0.5 * nota_ens
        reparto = {c: round(100 * pesos[c] * notas[c] / total_pesos, 1) for c in CRITERIOS}
        puntos[clave] = {**reparto, "total": round(sum(reparto.values()), 1)}

    orden = sorted(puntos, key=lambda c: -puntos[c]["total"])  # a igualdad, el orden del yaml
    mejor = orden[0]
    actual = max(datos["actuales"], key=lambda c: puntos[c]["total"]) if datos["actuales"] else None
    ventaja = round(puntos[mejor]["total"] - puntos[actual]["total"], 1) if actual else None
    if actual is None:
        decision, recomendado = "elegir", mejor
    elif mejor != actual and ventaja >= config.get("cambiar_si_ventaja", 15):
        decision, recomendado = "cambiar", mejor
    else:
        decision, recomendado = "mantener", actual

    formacion = None
    if recomendado not in datos["actuales"]:  # el equipo no conoce la nube recomendada
        f = config.get("formacion") or {}
        horas = datos["personas"] * (f.get("horas_fundamentos", 0) + f.get("horas_especialidad", 0))
        formacion = {"personas": datos["personas"], "personas_del_caso": datos["personas_del_caso"],
                     "horas": horas, "coste_eur": round(horas * f.get("coste_hora_eur", 0)),
                     **(config["proveedores"][recomendado].get("formacion") or {})}
    resultado = {"puntos": puntos, "orden": orden, "senales": datos, "bloqueos": {k: v for k, v in bloqueos.items() if v},
                 "actual": actual, "mejor": mejor,
                 "recomendado": recomendado, "decision": decision, "ventaja": ventaja, "formacion": formacion}
    resultado["existentes"] = existentes(problem_case, resultado, config)  # fallo 71
    return resultado


def _miles(n: float) -> str:
    return f"{n:,.0f}".replace(",", ".")


def _numero(n: float) -> str:
    return f"{n:.1f}".replace(".", ",").replace(",0", "")



def _existente(x: dict, rec: str, config: dict) -> str:
    """Fallo 71: una línea por lo que el cliente ya tiene en otra nube: qué se hace y por qué, con las cuentas."""
    e = config.get("existente") or {}
    que = f"**{x['nombre']}**" + (f" ({', '.join(x['servicios'])})" if x["servicios"] else "")
    migrar = (f"migrarlo costaría unos {_miles(x['coste_migracion_eur'])} € ({_miles(x['horas_migracion'])} h a "
              f"{_numero(x['tarifa'])} €/h) y ahorraría unos {_miles(x['ahorro_eur'])} € en {x['anios']} años "
              f"(operar una nube menos: {e.get('horas_operar_otra_nube_mes', 0)} h al mes de su equipo)")
    if x["decision"] == "migrar" and x["lo_pide_el_cliente"]:
        return f"{que}: se migra a {rec}, porque el cliente lo pide; {migrar}."
    if x["decision"] == "migrar":
        return (f"{que}: se migra a {rec}. {rec} le saca {_numero(x['ventaja'])} puntos y {migrar}: el ahorro paga "
                "con creces la migración.")
    if x["lo_pide_el_cliente"]:
        aviso = f" Migrar compensaría ({migrar}): comentarlo con el cliente." if x["compensa"] else ""
        return f"{que}: se mantiene e integra con {rec}, porque el cliente no quiere migrarlo.{aviso}"
    if x["ventaja"] < e.get("ventaja_minima", 25):
        porque = (f"{rec} solo le saca {_numero(x['ventaja'])} puntos (hacen falta {e.get('ventaja_minima', 25)} "
                  "para plantear migrar)")
    else:
        porque = f"el ahorro no llega a {_numero(e.get('ahorro_sobre_coste', 1.5))} veces lo que cuesta migrar"
    return f"{que}: se mantiene e integra con {rec}. {migrar[0].upper() + migrar[1:]}, pero {porque}."


def tabla(resultado: dict, config: dict) -> str:
    """La comparación en markdown, con los puntos de cada criterio sobre el peso que tiene."""
    pesos = config["pesos"]
    total_pesos = sum(pesos[c] for c in CRITERIOS)
    cabecera = ["Nube"] + [f"{NOMBRES_CRITERIOS[c]} ({round(100 * pesos[c] / total_pesos)})" for c in CRITERIOS]
    filas = ["| " + " | ".join(cabecera + ["Total (100)"]) + " |",
             "| --- |" + " ---: |" * len(CRITERIOS) + " ---: |"]
    for clave in resultado["orden"]:
        p = resultado["puntos"][clave]
        nombre = config["proveedores"][clave]["nombre"]
        if clave in resultado["senales"]["actuales"]:
            nombre += " (ya la usa o la prefiere)"
        if clave == resultado["recomendado"]:
            nombre = f"**{nombre}**"
        celdas = [nombre] + [_numero(p[c]) for c in CRITERIOS] + [f"**{_numero(p['total'])}**"]
        filas.append("| " + " | ".join(celdas) + " |")
    return "\n".join(filas)


def resumen(resultado: dict, config: dict) -> str:
    """La decisión, las señales del caso, la formación y las notas, en markdown (lo calcula Python, no el LLM)."""
    prov = config["proveedores"]
    rec = prov[resultado["recomendado"]]
    s = resultado["senales"]
    if resultado["decision"] == "mantener":
        decision = f"**Mantener {rec['nombre']}.**"
        if resultado["mejor"] != resultado["actual"]:
            decision += (f" {prov[resultado['mejor']]['nombre']} saca {_numero(resultado['ventaja'])} puntos más, "
                         f"por debajo de los {config.get('cambiar_si_ventaja', 15)} que hacen falta para que "
                         "compense cambiar.")
    elif resultado["decision"] == "cambiar":
        decision = (f"**Cambiar de {prov[resultado['actual']]['nombre']} a {rec['nombre']}**: le saca "
                    f"{_numero(resultado['ventaja'])} puntos (hacen falta {config.get('cambiar_si_ventaja', 15)}).")
    elif s.get("dejando"):  # fallo 69
        decision = (f"**{rec['nombre']}.** El cliente está dejando {', '.join(prov[c]['nombre'] for c in s['dejando'])} "
                    "y no usa ni prefiere otra de las cinco.")
    
    else:
        decision = f"**{rec['nombre']}.** El caso no dice que el cliente use o prefiera ninguna de las cinco."
    lineas = [f"**Plataforma recomendada:** {decision}", ""]
    d = resultado.get("despliegue")
    if d:  # fallo 70
        local = rec.get("hibrido") or "su oferta para centros de datos propios"
        como = {"cloud": f"toda la solución en {rec['nombre']}.",
                "hibrido": f"lo que no puede salir de sus servidores se queda en ellos y el resto va a {rec['nombre']}; "
                           f"para la parte local, {local}.",
                "on_premises": f"toda la solución en sus servidores. {rec['nombre']} queda como opción si más adelante "
                               f"sale a la nube ({local} lleva sus servicios a su centro de datos)."}[d["opcion"]]
        dijo = f" El cliente dijo: «{d['cliente']}»." if d.get("cliente") else ""
        lineas += [f"**Modelo de despliegue recomendado:** {DESPLIEGUES[d['opcion']]}: {como}{dijo}", ""]

    pistas = [f"ya usa o prefiere {', '.join(prov[c]['nombre'] for c in s['actuales'])}"] if s["actuales"] else []
    pistas += [f"{config['ecosistemas'][e]['nombre']} ({', '.join(p)})" for e, p in s["ecosistemas"].items()]
    if s["pide_ens"]:
        pistas.append("pide ENS o trabaja con el sector público")
    if s["pide_soberania"]:
        pistas.append("pide soberanía de los datos")
    lineas.append("**Lo que ha pesado del caso:** " + ("; ".join(pistas) if pistas else "nada concreto: "
                  "la nota sale de los servicios de datos e IA, el cumplimiento y el talento") + ".")
    if s.get("no_cuentan"):  # fallo 69: lo que nombra para decir que no lo usa no suma
        lineas += ["", "**No cuenta, aunque el cliente lo nombra:** " + "; ".join(
            f"{nombre} ({NO_CUENTA[opcion]})" for nombre, opcion in s["no_cuentan"].items()) + "."]
    if resultado.get("existentes"):  # fallo 71
        lineas += ["", "**Lo que ya tiene en otras nubes:**", ""] + [
            f"- {_existente(x, rec['nombre'], config)}" for x in resultado["existentes"]]
    afectadas = [c for c in resultado["bloqueos"] if c in (resultado["actual"], resultado["recomendado"])]
    if afectadas:
        lineas += ["", "**No admite lo que ya usa el cliente:** " + "; ".join(
            f"{prov[c]['nombre']} ({', '.join(resultado['bloqueos'][c])})" for c in afectadas) + "."]
    lineas += ["", f"**Región en España:** {rec.get('region_espana') or 'no'}. **ENS categoría alta:** "
               + {True: "sí", False: "no"}.get(rec.get("ens_alta"), "por confirmar")
               + f". **Soberanía:** {rec.get('soberania') or 'sin dato'}."]
    f = resultado["formacion"]
    if f:
        quien = f"{f['personas']} personas" + ("" if f["personas_del_caso"] else " (supuesto: el caso no dice cuántas)")
        lineas += ["", f"**Formación del equipo en {rec['nombre']}:** {quien}, unas {_miles(f['horas'])} horas en "
                   f"total ({_miles(f['coste_eur'])} € de su tiempo, a {config['formacion']['coste_hora_eur']} €/h). "
                   f"Itinerario: {f.get('fundamentos')} y {f.get('datos')}, con {f.get('plataforma')}."]
    if resultado["decision"] == "cambiar" or (s["pide_ens"] and rec.get("ens_alta") is None):
        lineas += [""] + [f"- {n}" for n in config.get("notas", [])]
    if s["pide_ens"] and rec.get("ens_alta") is None:
        lineas.append(f"- El caso pide ENS y no hemos podido confirmar la certificación de {rec['nombre']}: "
                      "comprobarla antes de presentarla.")
    return "\n".join(lineas)


INSTRUCCIONES = (
    "Eres consultor de cloud de Inetum. Inetum ya ha comparado AWS, Google Cloud, Microsoft Azure, Oracle Cloud e IBM "
    "Cloud con los datos del cliente y ha decidido la plataforma. Tu trabajo es explicar esa decisión al cliente, "
    "no tomarla: no cambies la nube recomendada ni ninguna cifra, y no añadas importes.\n"
    "Escribe en el idioma de los datos del cliente, sin tablas, sin títulos de nivel 1 o 2 y sin emojis, en unas 250 "
    "palabras como mucho:\n"
    "1. Un párrafo con la recomendación y por qué, citando los datos del cliente que más pesan.\n"
    "2. Un párrafo sobre mantener o cambiar: si se mantiene, qué ganaría cambiando y por qué no compensa; si se "
    "cambia, qué supone para su equipo (formación) y para lo que ya tiene (migración).\n"
    "3. Una lista de 3 a 5 puntos a confirmar con el cliente antes de cerrar la elección."
)


def explicar(resultado: dict, problem_case: dict, config: dict) -> str:
    """El texto del LLM que explica la decisión; vacío si el modelo falla (la sección sale igual, sin él)."""
    datos = (f"{contexto_cliente(problem_case)}\n\nCOMPARACIÓN DE INETUM (calculada; no la cambies):\n"
             f"{tabla(resultado, config)}\n\n{resumen(resultado, config)}")
    try:
        mensajes = [{"role": "system", "content": INSTRUCCIONES}, {"role": "user", "content": datos}]
        respuesta = gateway.completar(gateway.modelo("plataforma"), mensajes, temperature=0.2)
        return (respuesta.choices[0].message.content or "").strip()
    except Exception as exc:  # sin explicación, la sección lleva igualmente la tabla y la decisión
        print(f"[plataforma] No se pudo escribir la explicación: {type(exc).__name__}: {str(exc)[:200]}")
        return ""


def seccion(resultado: dict, explicacion: str, config: dict) -> str:
    """La sección del informe en markdown: decisión, explicación del LLM y la tabla de la comparación."""
    partes = [resumen(resultado, config)]
    if explicacion:
        partes.append(explicacion)
    partes.append("#### Comparación de los cinco hiperescalares\n\n" + tabla(resultado, config) +
                  "\n\n*Puntos sobre 100. Los pesos y los datos de cada nube están en "
                  "data_structures/hiperescalares.yaml.*")
    return "\n\n".join(partes)

_PALABRAS_EN = {"the", "and", "we", "our", "is", "are", "it", "of", "to", "in", "for", "with", "have", "use", "using",
                "they", "their", "no", "not", "all", "some", "data", "team", "people", "about"}
_PALABRAS_ES = {"el", "la", "los", "las", "de", "del", "que", "y", "en", "un", "una", "por", "con", "para", "es", "son",
                "lo", "se", "nos", "tenemos", "tiene", "usan", "usamos", "todo", "datos", "equipo", "personas"}


def _idioma(problem_case: dict) -> str:
    """'en' si los datos del cliente están sobre todo en inglés; si no, 'es'."""
    palabras = re.findall(r"[a-záéíóúñ']+", " ".join(_textos(problem_case).values()).lower())
    return "en" if sum(p in _PALABRAS_EN for p in palabras) > sum(p in _PALABRAS_ES for p in palabras) else "es"


def mensaje_chat(resultado: dict, config: dict, problem_case: dict) -> str:
    """Fallo 72: lo que ha decidido el agente de plataforma, en pocas líneas y en el idioma del cliente, para el chat:
    sale antes de que empiecen los agentes del informe (que tardan unos minutos). Es informativo: el informe sigue."""
    prov = config["proveedores"]
    rec = prov[resultado["recomendado"]]["nombre"]
    en = _idioma(problem_case) == "en"
    actual = prov[resultado["actual"]]["nombre"] if resultado.get("actual") else ""
    mejor = prov[resultado["mejor"]]["nombre"]
    v = _numero(resultado.get("ventaja") or 0)
    if resultado["decision"] == "mantener" and resultado["mejor"] != resultado["actual"]:
        por = (f"you already use it; {mejor} scores {v} points more, but switching wouldn't pay off" if en else
               f"ya la usáis; {mejor} saca {v} puntos más, pero no compensa cambiar")
    elif resultado["decision"] == "mantener":
        por = "you already use it and it's the best fit" if en else "ya la usáis y es la que mejor encaja"
    elif resultado["decision"] == "cambiar":
        por = (f"it scores {v} points more than {actual}, the one you use today" if en else
               f"le saca {v} puntos a {actual}, la que usáis ahora")
    else:
        por = "it's the best fit for what you've told me" if en else "es la que mejor encaja con lo que me habéis contado"
    lineas = ["Before I prepare the report, this is what I propose:" if en else
              "Antes de preparar el informe, esto es lo que propongo:", "",
              f"- **{'Platform' if en else 'Plataforma'}:** {rec}: {por}."]
    for x in resultado.get("existentes") or []:
        que = x["nombre"] + (f" ({', '.join(x['servicios'])})" if x["servicios"] else "")
        coste = _miles(x["coste_migracion_eur"])
        if x["decision"] == "migrar":
            hacer = (f"I'd migrate it to {rec}" + (", as you asked" if x["lo_pide_el_cliente"] else
                     f": about {coste} € of migration that pays off in {x['anios']} years") if en else
                     f"lo migraría a {rec}" + (", como nos habéis pedido" if x["lo_pide_el_cliente"] else
                     f": unos {coste} € de migración que se pagan en {x['anios']} años"))
        else:
            hacer = (f"I'd keep it and integrate it with {rec}" + (", as you asked" if x["lo_pide_el_cliente"] else
                     f": migrating it (about {coste} €) wouldn't pay off") if en else
                     f"lo mantendría y lo integraría con {rec}" + (", como nos habéis dicho" if x["lo_pide_el_cliente"]
                     else f": migrarlo (unos {coste} €) no compensa"))
        lineas.append(f"- **{('What you have in ' if en else 'Lo que tenéis en ') + que}:** {hacer}.")
    d = resultado.get("despliegue")
    if d:
        nombres = {"cloud": "in the cloud", "hibrido": "hybrid", "on_premises": "on-premises"} if en else \
                  {"cloud": "en la nube", "hibrido": "híbrido", "on_premises": "en vuestros servidores (on-premises)"}
        lineas.append(f"- **{'Deployment' if en else 'Despliegue'}:** {nombres[d['opcion']]}.")
    f = resultado.get("formacion")
    if f:
        lineas.append(f"- **{'Training' if en else 'Formación'}:** " + (
            f"{f['personas']} people from your team, about {_miles(f['horas'])} hours in {rec}." if en else
            f"{f['personas']} personas de vuestro equipo, unas {_miles(f['horas'])} horas en {rec}."))
    lineas += ["", "Now I'll prepare the full report (architecture, costs and plan); it takes a few minutes." if en else
               "Ahora preparo el informe completo con la arquitectura, los costes y el plan; tardará unos minutos."]
    return "\n".join(lineas)


def _inyectar(markdown: str) -> None:
    """Escribe la sección en el informe (div content-platform), como hacen las demás secciones."""
    if not REPORT_PATH.exists():
        print(f"[plataforma] WARN: no existe {REPORT_PATH}")
        return
    soup = parse_html(REPORT_PATH.read_text(encoding="utf-8"))
    destino = soup.find("div", id=SECCION)
    if destino is None:
        print(f"[plataforma] WARN: div id='{SECCION}' no encontrado en el HTML")
        return
    destino.clear()
    fragmento = parse_html(md_to_html(markdown))
    for hijo in list((fragmento.find("body") or fragmento).children):
        destino.append(hijo.__copy__())
    REPORT_PATH.write_text(str(soup), encoding="utf-8")


def elegir_plataforma(problem_case: dict) -> dict:
    """Decide la plataforma, la escribe en el informe y la guarda en el caso como plataforma_objetivo, que leen la
    arquitectura, los costes y el roadmap. Devuelve el caso con ella."""
    config = _config()
    resultado = puntuar(problem_case, config)
    resultado["despliegue"] = despliegue(problem_case)  # fallo 70
    prov = config["proveedores"]
    rec = prov[resultado["recomendado"]]
    d = resultado["despliegue"]
    print(f"[plataforma] {rec['nombre']} ({resultado['decision']}); notas: "
          + ", ".join(f"{prov[c]['nombre']} {_numero(resultado['puntos'][c]['total'])}" for c in resultado["orden"])
          + (f"; despliegue: {d['opcion']} ({d['confianza']:.2f})" if d else ""))
    _inyectar(seccion(resultado, explicar(resultado, problem_case, config), config))

    se_queda = {x["clave"] for x in resultado.get("existentes", []) if x["decision"] == "mantener"}
    if resultado["decision"] == "cambiar" and resultado["actual"] in se_queda:  # fallo 71
        motivo = (f"recomendada por Inetum; el cliente usa o prefiere {prov[resultado['actual']]['nombre']}: lo que ya "
                  "tiene allí se mantiene e integra, la solución nueva va en esta nube y su equipo se forma en ella")
    elif resultado["decision"] == "cambiar":
        motivo = (f"recomendada por Inetum; el cliente usa o prefiere {prov[resultado['actual']]['nombre']}: la propuesta "
                  "incluye migrar a ella y formar a su equipo")
    elif resultado["decision"] == "mantener":
        motivo = "el cliente ya la usa o la prefiere, y se mantiene"
    else:
        motivo = "recomendada por Inetum; el cliente no usa todavía ninguna nube"
    caso = dict(problem_case or {})
    caso[utils.PLATAFORMA_OBJETIVO] = {
        "description": "Nube en la que se diseña la solución (la elige el agente de plataforma después del discovery)",
        "value": rec["nombre"], "motivo": motivo, "decision": resultado["decision"],
        "puntos": {prov[c]["nombre"]: resultado["puntos"][c]["total"] for c in resultado["orden"]},
    }
    if d:  # fallo 70: lo leen contexto_cliente() y la tabla de precios
        caso[utils.PLATAFORMA_OBJETIVO]["despliegue"] = d["opcion"]
    caso[utils.PLATAFORMA_OBJETIVO]["mensaje"] = mensaje_chat(resultado, config, problem_case)  # fallo 72: para el chat
    if resultado.get("existentes"):  # fallo 71: lo lee contexto_cliente()
        caso[utils.PLATAFORMA_OBJETIVO]["existentes"] = [
            {"nube": x["nombre"], "servicios": x["servicios"], "decision": x["decision"],
             "coste_migracion_eur": x["coste_migracion_eur"]} for x in resultado["existentes"]]
    with open(utils.PROBLEM_CASE_PATH, "w", encoding="utf-8") as f:  # utils.: los tests cambian la ruta
        json.dump(caso, f, ensure_ascii=False, indent=2)
    return caso
