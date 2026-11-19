"""Comprueba el arreglo del fallo 5: los costes conocen el presupuesto y el total cuadra con el equipo.

No llama a ningún LLM: se sustituye Crew.kickoff por una versión que guarda el prompt
de cada tarea y devuelve respuestas simuladas. Tampoco usa Infracost.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_costes_equipo.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

from types import SimpleNamespace

from crewai import Crew

import agents.estimation_crew as ec
from agents.estimation_crew import (
    CostEstimationReport, EquipoEstimado, PerfilEquipo,
    _calcular_equipo, _cargar_tarifas, _encaje_presupuesto,
)

# 1) Las tarifas se leen bien del CSV
tarifas = _cargar_tarifas()
assert tarifas[("data engineer", "senior")] == 45
assert tarifas[("data analyst/bi", "middle")] == 28
assert len(tarifas) == 13 * 3, f"se esperaban 39 tarifas y hay {len(tarifas)}"
print("OK 1: tarifas leídas de bd/costes_perfil.csv (13 perfiles × 3 niveles)")

# 2) El coste del equipo se calcula en Python: horas × tarifa × meses
equipo = EquipoEstimado(
    perfiles=[
        PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160),   # 160 × 45 = 7.200
        PerfilEquipo(perfil="data architect", nivel="Senior", horas_mes=80),   # 80 × 75 = 6.000 (sin importar mayúsculas)
        PerfilEquipo(perfil="Experto en SAP", nivel="Senior", horas_mes=40),   # no está en la tabla: no se suma
    ],
    meses_min=4, meses_max=3,  # al revés a propósito: se ordenan
)
calculo = _calcular_equipo(equipo, tarifas)
assert calculo["mensual"] == 13_200
assert (calculo["total_min"], calculo["total_max"]) == (39_600, 52_800)
assert "| Data Engineer | Senior | 160 | 45 €/h | 7.200 € |" in calculo["texto"]
assert "| Experto en SAP | Senior | 40 | sin tarifa | no se suma |" in calculo["texto"]
assert "| **Total mensual** | | | | **13.200 €** |" in calculo["texto"]
assert "Coste del equipo en 3–4 meses: 39.600 € – 52.800 €" in calculo["texto"]
print("OK 2: equipo = 13.200 €/mes → 39.600 – 52.800 € en 3–4 meses; el perfil sin tarifa no se suma")

# 3) La regla del encaje con el presupuesto
assert _encaje_presupuesto(100_000, 150_000, 200_000)[0] == "alto"
assert _encaje_presupuesto(150_000, 250_000, 200_000)[0] == "medio"
assert _encaje_presupuesto(250_000, 300_000, 200_000)[0] == "bajo"
assert _encaje_presupuesto(250_000, 300_000, 0)[0] == "medio"  # sin presupuesto no se puede comprobar
print("OK 3: encaje alto / medio / bajo según el presupuesto, y 'medio' si no hay presupuesto")

# 4) run_estimation_crew completo con el LLM simulado
os.environ.pop("INFRACOST_API_KEY", None)
ec._resolve_infracost_binary = lambda: None  # sin Infracost: no se llama a ningún LLM para el Terraform

# Sin equipo, en los 3–4 meses del equipo: 10.000 €/mes + 10.000 € únicos = 40.000–50.000 €,
# y la contingencia del 20 % sube el escenario alto a 60.000 € (fallo 32: las sumas las hace Python)
informe_simulado = CostEstimationReport(
    componentes=[
        {"componente": "Azure Synapse", "categoria": "infraestructura", "precio_unitario_eur": 10_000,
         "unidades": 1, "unidad": "pool", "periodicidad": "mensual", "referencia": "Pool dedicado"},
        {"componente": "Formación", "categoria": "formacion", "precio_unitario_eur": 5_000,
         "unidades": 2, "unidad": "taller", "periodicidad": "unico", "referencia": "2 talleres"},
    ],
    resumen_ejecutivo={
        "meses_proyecto": 4, "presupuesto_cliente_max_eur": 200_000, "contingencia_pct": 20,
        "justificacion": "Infraestructura Azure de tamaño medio.",
    },
    supuestos=["Se asume Power BI Pro."],
)
prompts: list[str] = []

def kickoff_simulado(self, *args, **kwargs):
    for task in self.tasks:
        prompts.append(task.description)
        if task.name == "costes":
            task.output = SimpleNamespace(pydantic=informe_simulado, raw="")
        elif task.name == "equipo":
            task.output = SimpleNamespace(pydantic=equipo, raw="")
        else:
            task.output = SimpleNamespace(pydantic=None, raw="## Perfiles\n\nData Engineer y Data Architect.")
    return SimpleNamespace(raw="")

Crew.kickoff = kickoff_simulado

caso = {
    "restriccion_presupuestaria": {"description": "Presupuesto", "value": "150.000 a 200.000 euros"},
    "plazo_objetivo": {"description": "Plazo", "value": "MVP en 3-4 meses"},
}
resultado = ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case=caso)

assert len(prompts) == 3, f"se esperaban 3 tareas y hay {len(prompts)}"
for n, prompt in enumerate(prompts, start=1):
    assert "DATOS DEL CLIENTE" in prompt and "150.000 a 200.000 euros" in prompt, f"la tarea {n} no lleva el caso"
# Fallo 87: sin filas de la tabla de precios para esta propuesta, el JSON de costes no pide tarifa
prompt_costes = next(p for p in prompts if "valora el coste de cada componente" in p)
assert '"tarifa"' not in prompt_costes and "Tabla de precios" not in prompt_costes
print("OK 4: las 3 tareas de costes reciben el presupuesto y el plazo del cliente; sin tabla, no se pide tarifa")

# Total = 40.000–60.000 (sin equipo) + 39.600–52.800 (equipo) = 79.600–112.800 → cabe en 200.000
html = resultado.cost_estimation
assert "79.600 € – 112.800 €" in html, "el total no suma infraestructura + equipo"
assert "Encaje con el presupuesto" in html and "Alto" in html
assert "Equipo: 39.600 € – 52.800 €" in html, "la barra de desarrollo debe mostrar el coste del equipo"
assert "| **Total mensual** | | | | **13.200 €** |" in resultado.team_profiles
print("OK 5: el informe suma infraestructura + equipo (79.600 – 112.800 €) y el encaje sale 'Alto'")

# 6) Fallo 84: si se valoran menos meses que el plazo del cliente, el encaje es «Parcial» con la parte del presupuesto
#    que se lleva lo valorado, en el titular y en el texto (lo que lee el resumen). El 12 nov salía «Alto» con el MVP
#    (4 de 10 meses) frente al presupuesto de todo el proyecto
largo = {**caso, "plazo_objetivo": {"description": "Plazo", "value": "MVP en 3-4 meses; proyecto completo en 10 meses"}}
resultado = ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case=largo)
html = resultado.cost_estimation
# 79.600 € y 112.800 €, más el mínimo por el Experto en SAP (40 h/mes a 20 €/h): 82.000 € y 116.000 € de 200.000 €
assert "Parcial: el MVP, 3–4 de 10 meses, se lleva el 41–58 % (provisional)" in html, html
assert "el MVP, 3–4 de 10 meses, se lleva el 41–58 % del presupuesto del cliente (200.000 €)" in html
assert "Los meses que faltan hasta los 10 del plazo no están valorados" in html and "Alto" not in html
assert "width:40%" in html  # la barra: 4 de los 10 meses
# Si lo valorado ya no cabe, «Bajo», también con la parte que se lleva
pequeno = informe_simulado.model_copy(update={"resumen_ejecutivo": informe_simulado.resumen_ejecutivo.model_copy(
    update={"presupuesto_cliente_max_eur": 60_000})})
html = ec._format_cost_report(pequeno, "", _calcular_equipo(equipo, tarifas), largo)
assert "Bajo: el MVP, 3–4 de 10 meses, se lleva el 137–193 %" in html, html
assert "solo el MVP, 3–4 de 10 meses, ya supera el presupuesto del cliente (60.000 €)" in html  # tras «Contando…»
# Sin la palabra MVP ni piloto, «lo valorado»; con el plazo igual a lo valorado, el encaje de siempre
sin_mvp = {**caso, "plazo_objetivo": {"description": "Plazo", "value": "4 meses la primera parte y 12 meses en total"}}
assert "Parcial: lo valorado, 3–4 de 12 meses, se lleva el" in ec._format_cost_report(
    informe_simulado, "", _calcular_equipo(equipo, tarifas), sin_mvp)
assert ">Alto (provisional)<" in ec._format_cost_report(informe_simulado, "", _calcular_equipo(equipo, tarifas), caso)
print("OK 6: con 3–4 de 10 meses valorados el encaje es 'Parcial' (41–58 %), o 'Bajo' si ya no cabe")

# 7) Fallo 93: costes va a la vez que perfiles → equipo, en dos crews, y perfiles no lee el JSON de costes. La barrera
#    solo se abre si las dos crews están dentro de kickoff a la vez: en secuencia, el test se queda esperando y falla
import threading

barrera = threading.Barrier(2, timeout=10)
crews_vistas = []


contextos = {}


def kickoff_a_la_vez(self, *args, **kwargs):
    barrera.wait()
    crews_vistas.append([t.name for t in self.tasks])
    contextos.update({t.name: t.context for t in self.tasks})
    return kickoff_simulado(self, *args, **kwargs)


Crew.kickoff = kickoff_a_la_vez
resultado = ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case=caso)
Crew.kickoff = kickoff_simulado
assert sorted(crews_vistas) == [["costes"], ["perfiles", "equipo"]], crews_vistas
assert contextos["perfiles"] == [], contextos["perfiles"]  # sin el JSON de costes
assert [t.name for t in contextos["equipo"]] == ["perfiles"]  # equipo sigue leyendo los perfiles
assert "79.600 € – 112.800 €" in resultado.cost_estimation  # el total sigue juntando costes y equipo
print("OK 7: costes va a la vez que perfiles → equipo, perfiles no lee costes, y el total junta los dos")

# 8) Fallo 86: el MVP y el resto del proyecto, cada uno con sus meses y su equipo. El proyecto completo es la suma; lo que
#    solo entra después del MVP se paga en los meses del resto; el encaje es el del proyecto completo y el MVP sale como
#    parte del presupuesto. El 14 nov salió «4–10 meses» y «Medio» con el equipo completo durante 4 meses
from agents.estimation_crew import ComponenteCoste, EquipoDelResto, ResumenEjecutivo

mvp = EquipoEstimado(perfiles=[PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160)],  # 7.200 €/mes
                     meses_min=4, meses_max=4)
resto = EquipoDelResto(perfiles=[PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160),
                                 PerfilEquipo(perfil="Data Scientist", nivel="Senior", horas_mes=160)],
                       meses_min=6, meses_max=6)
calculado = _calcular_equipo(mvp, tarifas)
calculado["resto"] = _calcular_equipo(resto, tarifas)
tarifa_ds = tarifas[("data scientist", "senior")]
informe = CostEstimationReport(
    componentes=[ComponenteCoste(componente="Lago de datos", categoria="infraestructura", precio_unitario_eur=1_000,
                                 unidades=1, unidad="mes", periodicidad="mensual", referencia="x", fase="mvp"),
                 ComponenteCoste(componente="Visión artificial", categoria="infraestructura", precio_unitario_eur=500,
                                 unidades=1, unidad="mes", periodicidad="mensual", referencia="x", fase="Fase 2")],
    resumen_ejecutivo=ResumenEjecutivo(meses_proyecto=4, presupuesto_cliente_max_eur=200_000, justificacion="x"),
    supuestos=[])
assert informe.componentes[1].fase == "proyecto_completo"  # «Fase 2» se entiende
dos = {**caso, "plazo_objetivo": {"description": "Plazo", "value": "MVP en 4 meses; proyecto completo en 10 meses"}}
html = ec._format_cost_report(informe, "", calculado, dos)
mvp_total = 1_000 * 4 + 7_200 * 4                                    # sin la visión artificial, que es de la fase 2
completo = 1_000 * 10 + 500 * 6 + 7_200 * 4 + (7_200 + 160 * tarifa_ds) * 6
assert f"Proyecto completo (equipo incluido)</div><div style=\"font-size:1.5rem;font-weight:700;color:var(--text);\">" \
       f"{ec._eur(completo)}" in html, html
assert "MVP: 4 meses" in html and f">{ec._eur(mvp_total)}</div>" in html
pct = round(100 * mvp_total / 200_000)
assert f"el {pct} % del presupuesto" in html and f"El MVP (4 meses) cuesta {ec._eur(mvp_total)}: el {pct} %" in html
nivel_esperado = "Alto" if completo <= 200_000 else "Bajo"
assert f">{nivel_esperado}: el proyecto completo, 10 meses; el MVP se lleva el {pct} %<" in html, html
assert "Parcial" not in html  # con el proyecto completo valorado no hace falta el «Parcial» del fallo 84
assert "Desde el fin del MVP · en 6 meses: 3.000 €" in html and "Desde el MVP · en 10 meses: 10.000 €" in html
assert "Equipo del MVP · 4 meses" in html and "Equipo del resto del proyecto · 6 meses" in html
# Sin resto del proyecto, la fase no cambia nada: todo se paga en los meses valorados, como antes
html = ec._format_cost_report(informe, "", _calcular_equipo(mvp, tarifas), caso)
assert "En 4 meses: 2.000 €" in html and "Proyecto completo" not in html
# El equipo lo pide así el JSON y run_estimation_crew calcula y enseña los dos alcances
assert '"resto_del_proyecto": {"perfiles": [' in ec._plantilla_json(EquipoEstimado)
equipo = mvp.model_copy(update={"resto_del_proyecto": resto})  # lo que devuelve el modelo simulado
resultado = ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case=dos)
assert "Proyecto completo (equipo incluido)" in resultado.cost_estimation
assert "### MVP" in resultado.team_profiles and "### Resto del proyecto, después del MVP" in resultado.team_profiles
assert "Data Scientist" in resultado.team_profiles.split("### Resto del proyecto")[1]
print(f"OK 8: el MVP ({ec._eur(mvp_total)}, {pct} %) y el proyecto completo ({ec._eur(completo)}) se valoran por separado")

# 9) Fallo 88: costes valora los servicios del inventario de la arquitectura, cada uno desde su fase; avisa de lo que
#    el inventario trae y no está valorado, y de lo que se valora sin estar en él. Los perfiles también lo reciben
INVENTARIO = [{"servicio": "Lago de datos", "capa": "almacenamiento", "fase": "mvp", "alternativa": ""},
              {"servicio": "Visión artificial", "capa": "IA", "fase": "proyecto_completo", "alternativa": ""},
              {"servicio": "AWS Step Functions", "capa": "orquestación", "fase": "mvp", "alternativa": ""}]
sin_fase = informe.model_copy(update={"componentes": [
    c.model_copy(update={"fase": "mvp"}) for c in informe.componentes] + [
    ComponenteCoste(componente="Amazon API Gateway", categoria="infraestructura", precio_unitario_eur=10, unidades=1,
                    unidad="mes", periodicidad="mensual", referencia="x")]})
html = ec._format_cost_report(sin_fase, "", calculado, dos, inventario=INVENTARIO)
assert "Desde el fin del MVP · en 6 meses: 3.000 €" in html  # la fase de la visión artificial sale del inventario
assert "el inventario de la arquitectura trae AWS Step Functions, que no está valorado" in html
assert "se valora Amazon API Gateway, que no está en el inventario de la arquitectura" in html
prompts.clear()
equipo = mvp
ec.run_estimation_crew("PROPUESTA SIMULADA", "groq/openai/gpt-oss-20b", problem_case=dos, inventario=INVENTARIO)
costes_p = next(p for p in prompts if "valora el coste de cada componente" in p)
perfiles_p = next(p for p in prompts if "identifica los perfiles profesionales" in p)
assert "exactamente los del inventario de servicios de abajo" in costes_p
assert "- Visión artificial · IA · solo después del MVP" in costes_p and "- AWS Step Functions" in perfiles_p
assert "Pon perfiles solo para los servicios de cada alcance" in perfiles_p
print("OK 9: costes usa la fase del inventario y avisa de lo que falta o sobra; perfiles recibe el inventario")

# 10) Fallo 85: el lago de datos acumula lo que entra cada mes; un precio en dólares se pasa a euros con el tipo fijo
#     de data_structures/costes.yaml. El 12 nov, S3 salía a 12 €/mes fijos con 500 GB nuevos al mes, y los precios de
#     AWS en dólares iban como euros con el mismo número
cambio = ec._cambio()
assert cambio["usd_por_eur"] > 1 and cambio["fecha"] and "ecb.europa.eu" in cambio["fuente"]
lago = ComponenteCoste(componente="Amazon S3", categoria="infraestructura", precio_unitario_eur=0.024, unidades=500,
                       unidad="GB", periodicidad="mensual", referencia="500 GB nuevos al mes", acumulativo=True)
assert ec._coste_en_proyecto(lago, 10) == 12 * 10 * 11 / 2  # 12 € el mes 1, 24 € el 2… 120 € el 10: 660 €
assert ec._coste_al_mes(lago, 10) == 120  # lo que queda funcionando, el último mes
en_dolares = ComponenteCoste(componente="AWS Glue", categoria="infraestructura", precio_unitario_eur=0.44, unidades=60,
                             unidad="DPU-hora", periodicidad="mensual", referencia="Precio público", moneda="$")
assert en_dolares.moneda == "USD"
convertido = ec._en_euros(en_dolares, cambio)
assert abs(convertido.precio_unitario_eur - 0.44 / cambio["usd_por_eur"]) < 1e-9 and convertido.moneda == "EUR"
assert "Precio en dólares (0,44 $) pasado a euros a" in convertido.referencia
informe_85 = CostEstimationReport(componentes=[lago, en_dolares], supuestos=[], resumen_ejecutivo=ResumenEjecutivo(
    meses_proyecto=10, presupuesto_cliente_max_eur=0, justificacion="x"))
html = ec._format_cost_report(informe_85, "", None, caso)
assert "12 €/mes el primer mes (crece cada mes con lo que entra)" in html and "660 €" in html
assert f"{ec._cifra(0.44 / cambio['usd_por_eur'])} €/DPU-hora" in html
print("OK 10: el lago de datos acumula lo que entra cada mes y los precios en dólares se pasan a euros")

# 11) Fallo 113: con MVP y resto del proyecto, cada fila del equipo en el desglose lleva dentro el desarrollo y la
#     implantación, con las mismas cifras que «Estimación de coste del equipo». Desde el fallo 86 solo salía el total
mvp_113 = EquipoEstimado(perfiles=[PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160,
                                                horas_mes_implantacion=80)], meses_min=4, meses_max=4, meses_implantacion=1)
resto_113 = EquipoDelResto(perfiles=[PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160,
                                                  horas_mes_implantacion=40)], meses_min=6, meses_max=6,
                           meses_implantacion=2)
calculado = _calcular_equipo(mvp_113, tarifas)
calculado["resto"] = _calcular_equipo(resto_113, tarifas)
tarifa_de = tarifas[("data engineer", "senior")]
html = ec._format_cost_report(informe, "", calculado, dos)
fila_mvp = (f"Equipo: {ec._eur(tarifa_de * (160 * 3 + 80))} · desarrollo {ec._eur(tarifa_de * 160 * 3)} (3 meses), "
            f"implantación {ec._eur(tarifa_de * 80)} (1 mes)")
fila_resto = (f"Equipo: {ec._eur(tarifa_de * (160 * 4 + 40 * 2))} · desarrollo {ec._eur(tarifa_de * 160 * 4)} "
              f"(4 meses), implantación {ec._eur(tarifa_de * 40 * 2)} (2 meses)")
assert fila_mvp in html and fila_resto in html, html
for e in (calculado, calculado["resto"]):  # las mismas cifras que la sección del equipo
    assert f"Desarrollo en {ec._meses_txt(e['meses_min'] - e['meses_implantacion'], e['meses_max'] - e['meses_implantacion'])}: " \
           f"{ec._rango(e['desarrollo_min'], e['desarrollo_max'])}" in e["texto"]
    assert f"Implantación en {ec._meses_txt(e['meses_implantacion'], e['meses_implantacion'])}: " \
           f"{ec._eur(e['implantacion'])}" in e["texto"]
assert "Equipo del MVP · 4 meses" in html and "Equipo del resto del proyecto · 6 meses" in html
# Sin implantación en un alcance, su fila sigue con el total solo
calculado["resto"] = _calcular_equipo(resto, tarifas)
html = ec._format_cost_report(informe, "", calculado, dos)
assert f"Equipo: {ec._eur(calculado['resto']['total_max'])}</div>" in html and fila_mvp in html
print("OK 11: con MVP y resto, cada fila del equipo separa el desarrollo y la implantación")

print("\nTodo bien: el fallo 5 está arreglado.")