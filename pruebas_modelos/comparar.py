"""Compara modelos con las llamadas grabadas de una ejecución de AIDM.

Para grabar: GRABAR_LLAMADAS=1 en el .env y una ejecución normal. Las llamadas quedan en
pruebas_modelos/grabaciones/<fecha_hora>.jsonl, una por línea, con su tarea, el prompt y la respuesta. Este script
repite el prompt de cada tarea con cada modelo candidato (por defecto, los de su perfil en
data_structures/modelos.yaml) y mide si la respuesta sirve, cuánto tarda y cuántos tokens gasta.

Llama a los modelos de verdad, con las claves del .env: no va en tests/ejecutar_todos.py y gasta cupo gratuito
(SambaNova da 20 peticiones al día). Espera entre llamadas al mismo proveedor para no pasarse de su límite por minuto.

Uso (desde la carpeta del proyecto, con el venv activado):
    python pruebas_modelos/comparar.py --listar               # los modelos que te da cada proveedor con tus claves
    python pruebas_modelos/comparar.py --listar mistral gemini
    python pruebas_modelos/comparar.py --comprobar            # los modelos del yaml que el proveedor no tiene
    python pruebas_modelos/comparar.py pruebas_modelos/grabaciones/20261103_101500.jsonl
    python pruebas_modelos/comparar.py GRABACION --tareas costes,equipo --mas groq/openai/gpt-oss-120b
    python pruebas_modelos/comparar.py GRABACION --tareas costes --modelos gemini/gemini-3.8-flash,sambanova/gpt-oss-120b

--modelos cambia los candidatos del perfil por los que digas; --mas los añade a los del perfil.

Deja en pruebas_modelos/resultados/<fecha_hora>/:
- resumen.md: por tarea, cada modelo con si sirve, segundos, tokens y una nota;
- comparacion.html: las respuestas una al lado de otra, para leer los textos;
- llamadas.jsonl: todo en bruto.
La respuesta grabada sale también, como referencia. Las grabaciones y los resultados llevan datos del cliente: no
se suben (están en el .gitignore).
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from html import escape
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en pruebas_modelos/)
sys.path.insert(0, str(RAIZ))  # para importar gateway, utils y agents desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import certifi
import litellm

import gateway
import agents.discovery_agent_logic as discovery
import agents.estimation_crew as ec
from utils import LIST_FIELDS, TEXT_FIELDS

RESULTADOS = Path("pruebas_modelos/resultados")
LLAMADAS_POR_TAREA = {"discovery": 5}  # el discovery tiene una llamada por turno: se prueban hasta 5; el resto, 1
# Segundos entre dos llamadas al mismo proveedor, por sus límites gratuitos por minuto (Gemini, unas 10 por minuto)
PAUSA_MINIMA = {"gemini": 6.5, "groq": 2.1, "mistral": 1.2, "sambanova": 3.1, "nvidia_nim": 1.6, "cloudflare": 0.5}


# --- Los modelos que ofrece cada proveedor ---

def _pedir_json(url: str, cabeceras: dict | None = None) -> dict:
    """Verifica el certificado con los de certifi, los mismos que usa LiteLLM: el almacén de Windows puede no tener
    aún la raíz de algún proveedor (a SambaNova le faltaba la de Go Daddy) y la consulta fallaba aunque el modelo sí
    respondiera."""
    peticion = urllib.request.Request(url, headers={"User-Agent": "aidm-comparar", **(cabeceras or {})})
    contexto = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(peticion, timeout=30, context=contexto) as respuesta:
        return json.loads(respuesta.read().decode("utf-8"))


def _tipo_openai(base: str, variable: str) -> set[str] | None:
    """Groq, Mistral, SambaNova y NVIDIA: GET /models. Mistral da también los alias (…-latest)."""
    clave = os.getenv(variable, "")
    if not clave:
        return None
    datos = _pedir_json(f"{base}/models", {"Authorization": f"Bearer {clave}"})
    ids = set()
    for m in datos.get("data") or []:
        ids.add(m.get("id") or "")
        ids.update(m.get("aliases") or [])
    return ids - {""}


def _gemini() -> set[str] | None:
    clave = os.getenv("GEMINI_API_KEY", "")
    if not clave:
        return None
    ids, pagina = set(), ""
    while True:
        datos = _pedir_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000&key=" + clave
                            + (f"&pageToken={pagina}" if pagina else ""))
        ids.update((m.get("name") or "").removeprefix("models/") for m in datos.get("models") or [])
        pagina = datos.get("nextPageToken")
        if not pagina:
            return ids - {""}


def _cloudflare() -> set[str] | None:
    clave, cuenta = os.getenv("CLOUDFLARE_API_KEY", ""), os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    if not clave or not cuenta:
        return None
    ids, pagina = set(), 1
    while True:
        datos = _pedir_json(f"https://api.cloudflare.com/client/v4/accounts/{cuenta}/ai/models/search"
                            f"?per_page=100&page={pagina}", {"Authorization": f"Bearer {clave}"})
        resultado = datos.get("result") or []
        ids.update(m.get("name") or "" for m in resultado)
        if len(resultado) < 100:
            return ids - {""}
        pagina += 1


PROVEEDORES = {
    "groq": lambda: _tipo_openai("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "mistral": lambda: _tipo_openai("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "sambanova": lambda: _tipo_openai("https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY"),
    "nvidia_nim": lambda: _tipo_openai("https://integrate.api.nvidia.com/v1", "NVIDIA_NIM_API_KEY"),
    "gemini": _gemini,
    "cloudflare": _cloudflare,
}


def modelos_del_proveedor(proveedor: str) -> set[str] | None | str:
    """Los IDs que da el proveedor; None si falta su clave; un texto con el error si no se pudo consultar."""
    try:
        return PROVEEDORES[proveedor]()
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        return f"no se pudo consultar ({error})"


def listar(proveedores: list[str]) -> None:
    for proveedor in proveedores:
        if proveedor not in PROVEEDORES:
            print(f"\n{proveedor}: no lo conozco (son {', '.join(PROVEEDORES)})")
            continue
        ids = modelos_del_proveedor(proveedor)
        if ids is None:
            print(f"\n{proveedor}: sin clave en el .env")
        elif isinstance(ids, str):
            print(f"\n{proveedor}: {ids}")
        else:
            print(f"\n{proveedor} ({len(ids)} modelos):")
            for i in sorted(ids):
                print(f"  {proveedor}/{i}")


def comprobar() -> int:
    """Cada modelo de modelos.yaml contra lo que da su proveedor. Devuelve cuántos no están."""
    config = gateway._config()
    usos: dict[str, list[str]] = {}
    for perfil, lista in (config.get("perfiles") or {}).items():
        for m in lista:
            usos.setdefault(gateway._resolver(m), []).append(perfil)
    cache: dict[str, set[str] | None | str] = {}
    faltan = 0
    for modelo_yaml, perfiles in usos.items():
        proveedor, _, ident = modelo_yaml.partition("/")
        if proveedor not in cache:
            cache[proveedor] = modelos_del_proveedor(proveedor) if proveedor in PROVEEDORES else "proveedor desconocido"
        ids = cache[proveedor]
        if ids is None:
            estado = "sin clave en el .env"
        elif isinstance(ids, str):
            estado = ids
        elif ident in ids:
            estado = "OK"
        else:
            estado, faltan = "NO ESTÁ con tu clave", faltan + 1
        print(f"{estado:<22} {modelo_yaml}  ({', '.join(perfiles)})")
    print(f"\n{faltan} modelo(s) que el proveedor no tiene." if faltan else "\nTodos los que se han podido consultar están.")
    return faltan


# --- Comparación ---

def leer_grabacion(ruta: str | Path) -> dict[str, list[dict]]:
    """Las llamadas grabadas por tarea, en orden; sin las que fallaron."""
    por_tarea: dict[str, list[dict]] = {}
    for linea in Path(ruta).read_text(encoding="utf-8").splitlines():
        if not linea.strip():
            continue
        registro = json.loads(linea)
        if registro.get("error") or not registro.get("mensajes"):
            continue
        por_tarea.setdefault(registro.get("tarea") or "sin_tarea", []).append(registro)
    return por_tarea


def evaluar(tarea: str, texto: str) -> tuple[bool | None, str]:
    """¿Sirve la respuesta? True o False donde se puede comprobar (JSON, perfiles, Terraform); None en los textos, que
    se leen a mano. Y una nota con lo que se ha visto."""
    if not (texto or "").strip():
        return False, "respuesta vacía"
    if tarea == "discovery":
        try:
            payload = json.loads(discovery._extract_json_object(texto), strict=False)
        except Exception:
            payload = None
        if not isinstance(payload, dict) or not {"updates", "assistant_reply"} & set(payload):
            return False, "no es el JSON del discovery"
        plan = discovery._parse_plan(texto, set(TEXT_FIELDS) | set(LIST_FIELDS))
        return True, f"{len(plan['updates'])} campos; pregunta: «{plan['assistant_reply'][:90]}»"
    if tarea == "costes":
        try:
            informe = ec._leer_json(texto, ec.CostEstimationReport)
        except ValueError as error:
            return False, f"JSON no válido: {str(error)[:150]}"
        r = informe.resumen_ejecutivo
        meses, presupuesto = max(r.meses_proyecto, 1), r.presupuesto_cliente_max_eur
        componentes = [ec._sin_periodos_repetidos(ec._con_precio_de_tabla(c)) for c in informe.componentes]
        total = sum(ec._coste_en_proyecto(c, meses) for c in componentes)
        fuera = sum(presupuesto > 0 and ec._coste_en_proyecto(c, meses) > presupuesto for c in componentes)
        revisar = sum(presupuesto > 0 and ec._coste_al_mes(c, meses) > ec.PARTE_MAXIMA_AL_MES * presupuesto
                      for c in componentes)
        con_tarifa = sum(bool(c.tarifa) for c in componentes)
        return True, (f"{len(componentes)} componentes ({con_tarifa} con tarifa); {ec._eur(total)} en {meses} meses "
                      f"sin el equipo; {revisar} a revisar; {fuera} por encima del presupuesto")
    if tarea == "equipo":
        try:
            equipo = ec._leer_json(texto, ec.EquipoEstimado)
        except ValueError as error:
            return False, f"JSON no válido: {str(error)[:150]}"
        calculo = ec._calcular_equipo(equipo, ec._cargar_tarifas())
        return True, (f"{len(equipo.perfiles)} perfiles; {equipo.meses_min}–{equipo.meses_max} meses, "
                      f"{calculo['meses_implantacion']} de implantación; {ec._rango(calculo['total_min'], calculo['total_max'])}")
    palabras = len(texto.split())
    if tarea == "perfiles":
        nombrados = [p for p in ec.PERFILES_CON_TARIFA if p.lower() in texto.lower()]
        return bool(nombrados), f"nombra {len(nombrados)} perfiles de la tabla; {palabras} palabras"
    if tarea == "terraform":
        recursos = texto.count('resource "')
        return recursos > 0, f"{recursos} recursos"
    importes = len(ec._IMPORTE.findall(texto))
    return None, f"{palabras} palabras" + (f"; {importes} importes (solo los costes deberían darlos)" if importes else "")


_ultima_llamada: dict[str, float] = {}


def llamar(modelo: str, mensajes: list[dict]) -> dict:
    """Una llamada a un modelo, sin reservas (se quiere ver ese modelo), esperando lo que pida su proveedor."""
    proveedor = modelo.partition("/")[0]
    espera = PAUSA_MINIMA.get(proveedor, 0) - (time.monotonic() - _ultima_llamada.get(proveedor, -1e9))
    if espera > 0:
        time.sleep(espera)
    inicio = time.monotonic()
    try:
        respuesta = litellm.completion(model=modelo, messages=mensajes, **gateway.parametros(modelo))  # con su tope de tiempo
    except Exception as error:
        return {"error": f"{type(error).__name__}: {str(error)[:300]}", "segundos": time.monotonic() - inicio}
    finally:
        _ultima_llamada[proveedor] = time.monotonic()
    uso = getattr(respuesta, "usage", None)
    return {"respuesta": respuesta.choices[0].message.content or "", "segundos": time.monotonic() - inicio,
            "tokens_entrada": getattr(uso, "prompt_tokens", None), "tokens_salida": getattr(uso, "completion_tokens", None)}


def comparar(ruta: str | Path, tareas: list[str] | None = None, modelos: list[str] | None = None,
             mas: list[str] | None = None) -> Path:
    grabacion = leer_grabacion(ruta)
    elegidas = [t for t in grabacion if not tareas or t in tareas]
    if not elegidas:
        raise SystemExit(f"La grabación no tiene llamadas de {', '.join(tareas or [])}: tiene {', '.join(grabacion)}")
    carpeta = RESULTADOS / datetime.now().strftime("%Y%m%d_%H%M%S")
    carpeta.mkdir(parents=True, exist_ok=True)
    filas = []
    for tarea in elegidas:
        registros = grabacion[tarea][:LLAMADAS_POR_TAREA.get(tarea, 1)]
        _, del_perfil = gateway.modelos_de(tarea)
        candidatos = list(dict.fromkeys((modelos or del_perfil) + (mas or [])))
        for n, registro in enumerate(registros, 1):
            sirve, nota = evaluar(tarea, registro.get("respuesta", ""))
            filas.append({"tarea": tarea, "llamada": n, "modelo": f"{registro.get('modelo')} (grabado)", "sirve": sirve,
                          "nota": nota, "segundos": registro.get("segundos"), "tokens_entrada": registro.get("tokens_entrada"),
                          "tokens_salida": registro.get("tokens_salida"), "respuesta": registro.get("respuesta", "")})
            for modelo in candidatos:
                print(f"{tarea} {n}/{len(registros)} · {modelo}…", flush=True)
                resultado = llamar(modelo, registro["mensajes"])
                if "error" in resultado:
                    sirve, nota = False, resultado["error"]
                else:
                    sirve, nota = evaluar(tarea, resultado["respuesta"])
                filas.append({"tarea": tarea, "llamada": n, "modelo": modelo, "sirve": sirve, "nota": nota,
                              "segundos": resultado.get("segundos"), "tokens_entrada": resultado.get("tokens_entrada"),
                              "tokens_salida": resultado.get("tokens_salida"), "respuesta": resultado.get("respuesta", "")})
    (carpeta / "llamadas.jsonl").write_text(
        "".join(json.dumps(f, ensure_ascii=False, default=str) + "\n" for f in filas), encoding="utf-8")
    (carpeta / "resumen.md").write_text(_resumen(filas, ruta), encoding="utf-8")
    (carpeta / "comparacion.html").write_text(_html(filas, ruta), encoding="utf-8")
    return carpeta


def _sirve_txt(valores: list) -> str:
    comprobados = [v for v in valores if v is not None]
    if not comprobados:
        return "a juicio"
    if len(valores) == 1:
        return "sí" if comprobados[0] else "no"
    return f"{sum(comprobados)} de {len(valores)}"


def _num(valor, decimales: int = 0) -> str:
    if valor is None:
        return "—"
    return f"{valor:,.{decimales}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _resumen(filas: list[dict], ruta) -> str:
    lineas = [f"# Comparación de modelos", "", f"Grabación: `{Path(ruta).name}` · {datetime.now():%d/%m/%Y %H:%M}", "",
              "«Sirve»: JSON válido (discovery, costes, equipo), nombra perfiles de la tabla (perfiles) o trae recursos "
              "(terraform). Los demás textos se leen en comparacion.html.", ""]
    for tarea in dict.fromkeys(f["tarea"] for f in filas):
        de_tarea = [f for f in filas if f["tarea"] == tarea]
        lineas += [f"## {tarea}", "", "| Modelo | Sirve | Segundos | Tokens (entrada / salida) | Nota |",
                   "| --- | --- | ---: | ---: | --- |"]
        for modelo in dict.fromkeys(f["modelo"] for f in de_tarea):
            suyas = [f for f in de_tarea if f["modelo"] == modelo]
            segundos = [f["segundos"] for f in suyas if f["segundos"] is not None]
            entrada = sum(f["tokens_entrada"] or 0 for f in suyas)
            salida = sum(f["tokens_salida"] or 0 for f in suyas)
            fallida = next((f["nota"] for f in suyas if f["sirve"] is False), None)
            nota = (fallida or suyas[-1]["nota"]).replace("|", "/").replace("\n", " ")
            lineas.append(f"| {modelo} | {_sirve_txt([f['sirve'] for f in suyas])} | "
                          f"{_num(sum(segundos) / len(segundos), 1) if segundos else '—'} | "
                          f"{_num(entrada)} / {_num(salida)} | {nota} |")
        lineas.append("")
    return "\n".join(lineas)


def _html(filas: list[dict], ruta) -> str:
    secciones = []
    for tarea in dict.fromkeys(f["tarea"] for f in filas):
        de_tarea = [f for f in filas if f["tarea"] == tarea]
        for n in dict.fromkeys(f["llamada"] for f in de_tarea):
            columnas = "".join(
                f'<div class="col"><h3>{escape(f["modelo"])}</h3>'
                f'<p class="meta">{escape(_sirve_txt([f["sirve"]]))} · {_num(f["segundos"], 1)} s · '
                f'{_num(f["tokens_entrada"])} / {_num(f["tokens_salida"])} tokens</p>'
                f'<p class="nota">{escape(f["nota"])}</p><pre>{escape(f["respuesta"] or "")}</pre></div>'
                for f in de_tarea if f["llamada"] == n)
            titulo = f"{tarea}" + (f" · llamada {n}" if len([f for f in de_tarea if f['llamada'] > 1]) else "")
            secciones.append(f'<section><h2>{escape(titulo)}</h2><div class="fila">{columnas}</div></section>')
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Comparación de modelos</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1.5rem; color: #1f2937; background: #f8fafc; }}
.fila {{ display: flex; gap: 1rem; overflow-x: auto; align-items: flex-start; }}
.col {{ flex: 0 0 420px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 0.75rem; }}
h2 {{ margin-top: 2rem; }} h3 {{ margin: 0 0 0.25rem; font-size: 0.95rem; }}
.meta, .nota {{ font-size: 0.8rem; color: #475569; margin: 0.25rem 0; }}
pre {{ white-space: pre-wrap; font-size: 0.8rem; max-height: 640px; overflow: auto; background: #f1f5f9; padding: 0.5rem; }}
</style></head><body>
<h1>Comparación de modelos</h1><p>Grabación: {escape(Path(ruta).name)} · {datetime.now():%d/%m/%Y %H:%M}</p>
{''.join(secciones)}
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    # Los resultados llevan «€» y tildes: en Windows, sin esto, la consola con cp1252 falla
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Compara modelos con las llamadas grabadas de una ejecución de AIDM.")
    parser.add_argument("grabacion", nargs="?", help="archivo .jsonl de pruebas_modelos/grabaciones/")
    parser.add_argument("--listar", nargs="*", metavar="PROVEEDOR", help="los modelos que da cada proveedor")
    parser.add_argument("--comprobar", action="store_true", help="los modelos del yaml que su proveedor no tiene")
    parser.add_argument("--tareas", help="solo estas tareas, separadas por comas")
    parser.add_argument("--modelos", help="estos candidatos en vez de los del perfil, separados por comas")
    parser.add_argument("--mas", help="estos candidatos además de los del perfil, separados por comas")
    args = parser.parse_args(argv)
    lista = lambda texto: [t.strip() for t in (texto or "").split(",") if t.strip()] or None
    if args.listar is not None:
        listar(args.listar or list(PROVEEDORES))
        return 0
    if args.comprobar:
        return 1 if comprobar() else 0
    if not args.grabacion:
        parser.error("falta la grabación (o --listar / --comprobar)")
    carpeta = comparar(args.grabacion, lista(args.tareas), lista(args.modelos), lista(args.mas))
    print(f"\nResultados en {carpeta}: resumen.md y comparacion.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())