"""Comprueba el arreglo del fallo 3: el informe se muestra en la página y se descarga.

Ejecuta interface.py sin navegador con el AppTest de Streamlit. El grafo se
sustituye por uno simulado, así que no llama a ningún LLM ni cambia ningún archivo.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_interfaz_informe.py

Los avisos "missing ScriptRunContext" que salgan son normales con AppTest.
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import types
import webbrowser

import streamlit as st
from streamlit.testing.v1 import AppTest

import shutil
import tempfile

import utils
from utils import fuentes_locales

if not hasattr(st, "iframe"):
    sys.exit(f"Tu Streamlit es {st.__version__}; st.iframe necesita 1.56 o superior: pip install -U streamlit")

# Fallo 116: Streamlit lee el informe generado, no la plantilla; aquí, una copia de la plantilla en una carpeta temporal
INFORME = Path(tempfile.mkdtemp()) / "informe_generado.html"
shutil.copy(utils.PLANTILLA_INFORME, INFORME)
utils.INFORME_GENERADO = INFORME

# Grafo simulado: devuelve lo que devolvería run_graph() en cada caso
grafo = types.ModuleType("graph")
grafo.completar = True
grafo.run_graph = lambda prompt, chat_history=None, avisar=None, pintar_progreso=None: {  # fallo 72: avisar
    "assistant_response": "¿Qué volumen de datos manejáis?",
    "completion_reached": grafo.completar,
    "problem_case": {"nombre_del_cliente": {"value": "Farma Norte, S.L."}},
}
sys.modules["graph"] = grafo  # interface.py importará este en lugar del real

# Si algo intentara abrir el navegador del servidor, quedaría registrado aquí
abiertos = []
webbrowser.open = lambda *args, **kwargs: abiertos.append(args)

# 1) Un turno normal de discovery: respuesta en el chat, sin informe
grafo.completar = False
at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
at.run()
at.chat_input[0].set_value("Somos una distribuidora farmacéutica").run()
assert not at.exception, at.exception
assert at.chat_message[-1].markdown[0].value == "¿Qué volumen de datos manejáis?"
assert not at.get("iframe") and not at.get("download_button"), "aún no debería haber informe"
print("OK 1: un turno normal responde en el chat y no muestra informe")

# 2) El turno que completa el discovery: informe en la página y botón de descarga
grafo.completar = True
at = AppTest.from_file(str(RAIZ / "interface.py"), default_timeout=60)
at.run()
at.chat_input[0].set_value("Ya está todo").run()
assert not at.exception, at.exception
assert abiertos == [], "no debe abrir el navegador del servidor"
assert "Aquí tienes la propuesta" in at.success[0].value

boton = at.get("download_button")
assert len(boton) == 1, "falta el botón de descarga"
assert "Descargar informe" in boton[0].proto.label

iframe = at.get("iframe")
assert len(iframe) == 1, "falta la vista previa del informe"
assert iframe[0].proto.src.startswith("data:text/html;base64,"), "el informe debe ir como data: URL"

informe = at.session_state["report_html"]
assert informe == fuentes_locales(INFORME.read_text(encoding="utf-8"))
assert "fonts.googleapis.com" not in informe and informe.count("@font-face") == 2, "fallo 66: la fuente va dentro"
nombre = at.session_state["report_filename"]
assert nombre.startswith("informe_aidm_Farma_Norte_S_L_") and nombre.endswith(".html"), nombre
print(f"OK 2: el informe se ve en la página y se descarga como {nombre}")

# 3) Al recargar (por ejemplo, tras pulsar el botón) el informe sigue ahí
at.run()
assert len(at.get("iframe")) == 1 and len(at.get("download_button")) == 1
print("OK 3: el informe sigue visible al recargar la página")

# 4) Fallo 127: las noticias de la web buscan con el sector del caso (solo el sector), primero las del sector, y si la
#    búsqueda falla salen las generales. Parte 3: Bing News con Google News de reserva, y una frase de cada noticia
#    sacada de la descripción del medio. Sin red: las búsquedas y las páginas se simulan
import json
import urllib.parse

import api

BING = """<rss xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss"><channel>{items}</channel></rss>"""
ITEM_BING = """<item><title>{t} {n}</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url={url}&amp;c=1</link>
<description>Texto del principio del artículo, cortado por Bing ...</description>
<pubDate>{fecha}</pubDate><News:Source>Medio {n}</News:Source></item>"""
GOOGLE = """<rss><channel>
<item><title>{t} G1 - Fuente A</title><link>https://news.google.com/rss/articles/{slug}1</link>
<pubDate>Mon, 23 Nov 2026 08:00:00 GMT</pubDate><source>Fuente A</source></item>
<item><title>{t} G2</title><link>https://news.google.com/rss/articles/{slug}2</link>
<pubDate>Sun, 22 Nov 2026 08:00:00 GMT</pubDate><source>Fuente B</source></item>
</channel></rss>"""
pedidas, paginas, fallan, fallan_bing, vacio_bing = [], [], set(), set(), set()


class _Respuesta:
    def __init__(self, cuerpo):
        self.cuerpo = cuerpo
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self, *a):
        return self.cuerpo


def urlopen_simulado(req, timeout=None):
    url = req.full_url
    pedidas.append(url)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0].replace(" when:7d", "")
    slug = urllib.parse.quote(q.replace(" ", "-"))
    if any(f in q for f in fallan) or ("bing.com" in url and any(f in q for f in fallan_bing)):
        raise OSError("sin red")
    if "bing.com" in url:
        items = "" if any(f in q for f in vacio_bing) else "".join(ITEM_BING.format(
            t=q, n=n, fecha=f"Mon, {24 - n} Nov 2026 08:00:00 GMT",
            url=urllib.parse.quote(f"https://medio.example/{slug}/{n}", safe="")) for n in (1, 2))
        return _Respuesta(BING.format(items=items).encode())
    return _Respuesta(GOOGLE.format(t=q, slug=slug).encode())


LARGA = ("La inteligencia artificial llega a la sala de cata con un sistema que compara cientos de vinos y ayuda a "
         "los enólogos a decidir mezclas, con datos de cada añada y de cada parcela, sin cambiar la forma de "
         "trabajar de la bodega ni sus tiempos de vendimia")


def pagina_simulada(url):
    paginas.append(url)
    if url.endswith("/1"):  # la descripción del medio, larga
        return f'<html><head><meta property="og:description" content="{LARGA}"></head><body><p>Cuerpo</p></body>'
    return "<html><head><title>T</title></head><body><p>" + "Texto del cuerpo del artículo. " * 20 + "</p></body>"


api.urllib.request.urlopen = urlopen_simulado
api._leer_pagina = pagina_simulada
api._lanzar_frases = api._completar_frases  # en la prueba, sin hilo: las frases quedan en la caché al momento
api.DATA = Path(tempfile.mkdtemp())  # el caso de la prueba; el problem_case.json real no se toca
(api.DATA / "problem_case.json").write_text(json.dumps({
    "nombre_del_cliente": {"value": "Bodegas Ebrosol, S.L."},
    "sector": {"value": "Vitivinícola (Bodegas Ebrosol), agroalimentario"}}), encoding="utf-8")
respuesta = api.obtener_noticias()
noticias = respuesta["noticias"]
assert pedidas and all("Ebrosol" not in urllib.parse.unquote_plus(u) for u in pedidas + paginas), pedidas
consultas = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["q"][0] for u in pedidas]
assert all("bing.com" in u for u in pedidas), "con Bing respondiendo, Google no se usa"
assert "vitivinícola inteligencia artificial" in consultas and "vitivinícola análisis de datos" in consultas
assert len(noticias) == 10 and all(n["del_sector"] for n in noticias[:6]) and not noticias[-1]["del_sector"]
assert all(n["url"].startswith("https://medio.example/") for n in noticias), "el enlace es el del artículo"
assert {n["fuente"] for n in noticias} == {"Medio 1", "Medio 2"}
assert {n["tema"] for n in noticias} <= {"ia", "datos", "cloud", "digital"}
assert not any("imagen" in n or "url_medio" in n for n in noticias)
# Primero se pintan las tarjetas, sin frase; la frase llega en la siguiente petición, desde la caché
assert respuesta["frases_pendientes"] is True and all(n["frase"] is None for n in noticias)
pedidas.clear()
respuesta = api.obtener_noticias()
assert respuesta["cache"] is True and respuesta["frases_pendientes"] is False and not pedidas
con_frase = {n["url"].rsplit("/", 1)[1]: n["frase"] for n in respuesta["noticias"]}
assert con_frase["2"] is None, "sin descripción del medio no hay frase: nunca texto del cuerpo"
frase = con_frase["1"]
assert frase.startswith("La inteligencia artificial llega a la sala de cata") and frase.endswith("…")
assert len(frase) <= api.FRASE_MAX + 1 and "Texto del principio" not in frase and "Cuerpo" not in frase
# La frase: og:description primero, si no meta description; sin descripción, None; nunca el cuerpo
assert api._frase('<head><meta name="description" content="Una descripción del medio."></head>') == \
    "Una descripción del medio."
assert api._frase('<head><meta content="Del og." property="og:description"><meta name="description" '
                  'content="La otra."></head>') == "Del og."
assert api._frase("<head></head><body><p>" + "Solo cuerpo. " * 30 + "</p></body>") is None
assert api._frase('<body><meta name="description" content="Dentro del cuerpo"></body>') is None
assert api._frase('<head><meta name="description" content="Un foro del sector | Medio X"></head>') == \
    "Un foro del sector"  # el nombre del medio tras « | » ya va aparte
assert api._frase("") is None and api._frase('<head><meta name="description" content=""></head>') is None
assert api._frase('<head><meta name="description" content="Título &amp; más"></head>', "Título & más") is None
# Parte 4: el HTML escapado dos veces y las etiquetas no llegan a la frase
assert api._frase('<head><meta name="description" content="&lt;p&gt;Llegamos una vez m&amp;aacute;s a la feria '
                  'del vino&lt;/p&gt;"></head>') == "Llegamos una vez más a la feria del vino"
assert api._frase('<head><meta name="description" content="Texto con &lt;strong&gt;negrita&lt;/strong&gt; '
                  'dentro"></head>') == "Texto con negrita dentro"
assert api._limpiar("&amp;amp;amp;lt;b&amp;amp;amp;gt;Hola") == "&lt;b&gt;Hola"  # como mucho 3 veces
# Parte 4: el título genérico de Bing se cambia por el de la página; sin título fiable, la noticia se descarta
PAGINA_OG = '<head><meta property="og:title" content="VitiGPT llega a las bodegas"><title>X | Tecnovino</title></head>'
assert api._titulo_fiable("Suscríbete a nuestro Boletín", PAGINA_OG) == "VitiGPT llega a las bodegas"
assert api._titulo_fiable("Suscríbete al boletín", "<head><title>VitiGPT llega a las bodegas | Tecnovino</title>"
                          "</head>") == "VitiGPT llega a las bodegas"
assert api._titulo_fiable("VitiGPT llega a las bodegas catalanas", PAGINA_OG) == "VitiGPT llega a las bodegas catalanas"
assert api._titulo_fiable("Resultados del tercer trimestre", PAGINA_OG) == "VitiGPT llega a las bodegas"
assert api._titulo_fiable("Suscríbete a nuestro Boletín", "") is None
assert api._titulo_fiable("Política de cookies", "<head><title>Aviso legal</title></head>") is None
assert api._titulo_fiable("VitiGPT llega a las bodegas", "<head><title>Tecnovino</title></head>") == \
    "VitiGPT llega a las bodegas"  # el nombre del sitio solo no es un título
paginas_titulo = {"https://medio.example/generico/1": PAGINA_OG, "https://medio.example/generico/2": ""}
api._leer_pagina = lambda url: paginas_titulo.get(url, "")
generica = lambda n: api._noticia("IA y automatización", True, "Suscríbete a nuestro Boletín",
                                  f"https://medio.example/generico/{n}", "Mon, 23 Nov 2026 08:00:00 GMT", "Tecnovino",
                                  f"https://medio.example/generico/{n}")
assert api._noticia("Cloud", False, "Suscríbete al boletín", "https://news.google.com/x", "", "G", None) is None
api._noticias_cache["prueba"] = {"timestamp": 0, "items": [generica(1), generica(2)], "frases": "pendientes"}
assert api._para_la_web(api._noticias_cache["prueba"]["items"]) == [], "el título genérico no se enseña"
api._completar_frases("prueba")
assert [n["titulo"] for n in api._para_la_web(api._noticias_cache["prueba"]["items"])] == [
    "VitiGPT llega a las bodegas"], api._noticias_cache["prueba"]["items"]
api._leer_pagina = pagina_simulada
# Solo https y servidores públicos, como mucho 3 redirecciones
api.socket.getaddrinfo = lambda host, *a: [(0, 0, 0, "", ({"medio.example": "93.184.216.34",
                                                             "interno.example": "10.0.0.7"}.get(host, "127.0.0.1"), 443))]
assert api._url_permitida("https://medio.example/noticia")
assert not api._url_permitida("http://medio.example/noticia")
assert not api._url_permitida("https://localhost/x") and not api._url_permitida("https://127.0.0.1/x")
assert not api._url_permitida("https://interno.example/x") and not api._url_permitida("https://192.168.1.10/x")
assert api._Redirecciones.max_redirections == 3 and api.PAGINA_MAX_BYTES == 200_000
# Si Bing falla o no devuelve nada, Google News, sin frase
api._noticias_cache.clear()
pedidas.clear()
fallan_bing.add("vitivinícola inteligencia")
vacio_bing.add("vitivinícola digitalización")
respuesta = api.obtener_noticias()
de_google = [n for n in respuesta["noticias"] if n["url"].startswith("https://news.google.com/")]
assert any("news.google.com" in u and "inteligencia" in u for u in pedidas)
assert any("news.google.com" in u and "digitalizaci" in u for u in pedidas)
assert de_google and all(n["frase"] is None for n in de_google) and not any(" - Fuente A" in n["titulo"] for n in de_google)
paginas.clear()
api.obtener_noticias()
assert not any("news.google.com" in u for u in paginas), "a Google News no se le piden páginas"
fallan_bing.clear(); vacio_bing.clear()
# Si la búsqueda del sector falla del todo, las generales
api._noticias_cache.clear()
fallan.add("vitivinícola")
noticias = api.obtener_noticias()["noticias"]
assert noticias and not any(n["del_sector"] for n in noticias), noticias
# Sin red del todo, las últimas que hubo; y sin sector, solo las generales
fallan.add("empresas")
assert api.obtener_noticias()["cache"] is True  # siguen en caché
api._noticias_cache["vitivinícola"]["timestamp"] = 0
assert [n["titulo"] for n in api.obtener_noticias()["noticias"]] == [n["titulo"] for n in noticias]
fallan.clear()
(api.DATA / "problem_case.json").write_text(json.dumps({"sector": {"value": ""}}), encoding="utf-8")
pedidas.clear()
assert not any(n["del_sector"] for n in api.obtener_noticias()["noticias"]) and len(pedidas) == 3
print("OK 4: las noticias buscan solo con el sector en Bing (Google de reserva), sin el nombre del cliente; la frase "
      "es la descripción del medio, limpia y recortada; un título genérico se cambia por el de la página o se descarta; "
      "si fallan, las generales")

# 5) Fallo 132: la pestaña Resumen. Cada enlace del resumen (bloques, puntos clave, flujo, CRISP-DM y «← Volver al
#    resumen») apunta a un id que existe en el HTML, también con fases «no» y «ya lo tiene» y con una sección sin
#    generar (su bloque, sin enlace); no hay ids repetidos ni ids internos a la vista. La pestaña CRISP-DM de Asier se
#    oculta mientras su JS no lea #crisp-datos (y sus nodos del resumen van sin enlace); cuando lo lea, se ve y enlazan.
import re

from bs4 import BeautifulSoup

from agents import resumen_secciones as rs
from agents import roadmap_builder as rb

PROPUESTA = {clave: {"para_todos": f"Texto de {clave}.", "decision_principal": f"Decisión de {clave}."}
             for clave, _, _, _ in rs.SECCIONES}
PROYECTO = {"general": "Resumen general.",
            "puntos": [("Punto de arquitectura.", "arquitectura"), ("Punto de gobernanza.", "gobernanza"),
                       ("Punto de fuentes.", "fuentes"), ("Punto de Ops.", "ops"), ("Punto suelto.", None)],
            "fases": {clave: {"puntos": [f"Punto de {clave}"], "decision_principal": "Decisión."}
                      for clave, _, _ in rs.FASES},
            "crisp": {clave: [f"Punto de {clave}"] for clave, _, _ in rs.CRISP}}
CIFRAS = {"total_etiqueta": "Coste total (equipo incluido)", "total": "50.000 €", "meses": "4 meses", "mvp": "",
          "al_mes": ""}


def montar(no_aplican=None, ya=None, sin_generar=(), crisp_lee=False) -> BeautifulSoup:
    rb.REPORT_PATH = Path(tempfile.mkdtemp()) / "informe_generado.html"
    shutil.copy(utils.PLANTILLA_INFORME, rb.REPORT_PATH)
    rb.inject_roadmap_into_html(*[f"Texto de la fase {n}." for n in range(7)], no_aplican=no_aplican or {},
                                ya_implantadas=ya or {})
    sopa = BeautifulSoup(rb.REPORT_PATH.read_text(encoding="utf-8"), "html.parser")
    for div_id in sin_generar:
        sopa.find(id=div_id).clear()
    if crisp_lee:  # como lo dejará Asier: su JS lee los textos de #crisp-datos
        script = sopa.new_tag("script")
        script.string = 'const datos = JSON.parse(document.getElementById("crisp-datos").textContent);'
        sopa.body.append(script)
    estados = {**{d: rb.NO for d in no_aplican or {}}, **{d: rb.YA for d in ya or {}}}
    rs.montar_resumen(sopa, PROPUESTA, PROYECTO, CIFRAS, estados)
    return BeautifulSoup(str(sopa), "html.parser")  # como queda escrito en el informe


def comprobar_enlaces(sopa: BeautifulSoup) -> None:
    ids = [tag["id"] for tag in sopa.find_all(id=True)]
    repetidos = sorted({i for i in ids if ids.count(i) > 1})
    assert not repetidos, f"ids repetidos: {repetidos}"
    rotos = [a["href"] for a in sopa.select('a[href^="#"]') if a["href"][1:] not in ids]
    assert not rotos, f"enlaces sin destino: {rotos}"
    resumen = sopa.find(id="tab-resumen")
    assert all(a["href"].startswith("#") for a in resumen.find_all("a")), "un enlace del resumen sale del informe"
    assert not resumen.select("[src]") and "http" not in str(resumen), "la pestaña Resumen carga algo de fuera"
    assert "roadmap-" not in resumen.get_text() and "content-" not in resumen.get_text(), "un id interno a la vista"
    assert sopa.select('a.volver-resumen[href="#resumen"]'), "sin «← Volver al resumen»"


# a) Todo generado
sopa = montar()
comprobar_enlaces(sopa)
assert [a["href"] for a in sopa.select("#resumen-bloques > a")] == [f"#{d}" for *_, d in rs.SECCIONES + [rs.COSTES]]
assert [a["href"] for a in sopa.select("#resumen-flujo a.rdm-pipe-node")] == [f"#{d}" for *_, d in rs.FASES]
assert sopa.select_one(".resumen-pdf")["onclick"] == "window.print()"
assert len(sopa.select(".volver-resumen")) == 6 + 7, "un «Volver» por sección de destino (sin CRISP-DM, oculta)"

# b) Ops «no», Fuentes e Ingesta «ya lo tiene» y gobernanza sin generar
sopa = montar(no_aplican={"roadmap-ml": "sin modelos"}, ya={"roadmap-sources": "su ERP", "roadmap-ingestion": "SSIS"},
              sin_generar=["content-governance"])
comprobar_enlaces(sopa)
gobernanza = sopa.select("#resumen-bloques > .resumen-bloque")[2]
assert gobernanza.name == "div" and gobernanza.h3.get_text() == "Gobernanza", "sin generar: bloque sin enlace"
nodos = sopa.select("#resumen-flujo .rdm-pipe-node")
assert [n.get_text(" ", strip=True).split(" ")[0] for n in nodos][:2] == ["Fuentes", "Ingesta"] and len(nodos) == 6
assert [n.get("href") for n in nodos] == ["#fases-reutilizadas"] * 2 + ["#fase-almacenamiento", "#fase-etl",
                                                                        "#fase-modelado", "#fase-visualizacion"]
assert [(li.get_text(), li.a["href"] if li.a else None) for li in sopa.select("#resumen-puntos > li")] == [
    ("Punto de arquitectura.", "#seccion-arquitectura"), ("Punto de gobernanza.", None),
    ("Punto de fuentes.", "#fases-reutilizadas"), ("Punto de Ops.", None), ("Punto suelto.", None)]
assert sopa.select_one("#resumen-flujo .rdm-ya-implantado").get_text().startswith("Se reutiliza: Fuentes")
assert sopa.select_one("#resumen-flujo .rdm-no-aplica").get_text().startswith("No aplica a este proyecto: Ops")
assert sopa.find(id="fases-reutilizadas").find_next_sibling("a")["href"] == "#resumen"
assert sopa.select_one("#tab-roadmap > #fases-reutilizadas"), "la línea «Se reutiliza» va en la pestaña Roadmap"

# c) La pestaña CRISP-DM sin soporte de #crisp-datos: oculta y sus nodos del resumen sin enlace; con soporte: visible
for crisp_lee in (False, True):
    sopa = montar(crisp_lee=crisp_lee)
    comprobar_enlaces(sopa)
    tab, boton = sopa.find(id="tab-crispdm"), sopa.find("button", onclick=re.compile("'crispdm'"))
    assert tab.has_attr("hidden") == boton.has_attr("hidden") == (not crisp_lee)
    nodos = sopa.select("#resumen-crisp .crisp-phase")
    assert len(nodos) == 6 and not any(n.has_attr("data-phase") for n in nodos)
    if crisp_lee:
        assert [n.get("href") for n in nodos] == [f"#{i}" for _, _, i in rs.CRISP]
        assert [b["id"] for b in tab.select(".crisp-phase")] == [i for _, _, i in rs.CRISP]
        assert tab.find("a", class_="volver-resumen") is not None
    else:
        assert all(n.name == "div" for n in nodos), "con la pestaña oculta, los nodos van sin enlace"
    assert len(json.loads(sopa.find(id="crisp-datos").string)) == 6

# d) La web deja abrir el diálogo de imprimir (window.print) desde el iframe del informe
assert "allow-modals" in re.search(r'<iframe id="report" sandbox="([^"]+)"', Path("index.html").read_text(
    encoding="utf-8")).group(1)
print("OK 5: la pestaña Resumen va primera; cada enlace llega a un id que existe (con fases «no» y «ya lo tiene» y una "
      "sección sin generar), sin ids repetidos; CRISP-DM oculta y sin enlaces hasta que lea #crisp-datos")

# 6) Fallo 135: GET /api/posicionamiento da 204 sin cuadrante, el JSON cuando está y otra vez 204 tras «Nueva sesión»
#    (POST /api/reset lo borra). Datos inventados, en la carpeta temporal de la prueba
shutil.copy(Path("data_structures/problem_case_recovery.json"), api.DATA / "problem_case_recovery.json")
CUADRANTE = {"area": "Previsión de demanda", "cliente": {"hoy": {"x": 2, "y": 6, "motivo": "Excel"},
                                                         "con_proyecto": {"x": 6, "y": 8, "motivo": "Previsión"}},
             "competidores": [{"nombre": "Tejidos Alfa", "x": 8, "y": 9, "motivo": "Analítica propia", "confianza": "media"}]}
assert api.get_posicionamiento().status_code == 204
(api.DATA / "posicionamiento.json").write_text(json.dumps(CUADRANTE, ensure_ascii=False), encoding="utf-8")
assert api.get_posicionamiento() == CUADRANTE
(api.DATA / "posicionamiento.json").write_text('{"area": "a medi', encoding="utf-8")
assert api.get_posicionamiento().status_code == 204, "un archivo roto no rompe la web"
(api.DATA / "posicionamiento.json").write_text(json.dumps(CUADRANTE, ensure_ascii=False), encoding="utf-8")
actual = api.posicionamiento._actual
assert api.reset() == {"ok": True}
assert not (api.DATA / "posicionamiento.json").exists() and api.get_posicionamiento().status_code == 204
assert api.posicionamiento._actual == actual + 1, "un hilo de la ejecución anterior ya no escribe"
assert Path("assets/js/cuadrante.js").exists() and "window.pintarCuadrante" in Path("assets/js/cuadrante.js").read_text(
    encoding="utf-8")
print("OK 6: /api/posicionamiento da 204, luego el cuadrante, y otra vez 204 tras «Nueva sesión»")

print("\nTodo bien: los fallos 3, 127, 132 y 135 están arreglados.")
