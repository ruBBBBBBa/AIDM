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
#    búsqueda falla salen las generales. Sin red: la búsqueda se simula
import json
import urllib.parse

import api

RSS = """<rss xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>{t} 1</title><link>https://ejemplo.com/{t}/1</link><pubDate>Mon, 23 Nov 2026 08:00:00 GMT</pubDate>
<source>Fuente A</source><enclosure url="https://ejemplo.com/{t}.jpg" type="image/jpeg"/></item>
<item><title>{t} 2</title><link>https://ejemplo.com/{t}/2</link><pubDate>Sun, 22 Nov 2026 08:00:00 GMT</pubDate>
<source>Fuente B</source></item>
</channel></rss>"""
pedidas, fallan = [], set()


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
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
    if any(f in q for f in fallan):
        raise OSError("sin red")
    return _Respuesta(RSS.format(t=q.replace(" when:7d", "")).encode())


api.urllib.request.urlopen = urlopen_simulado
api.DATA = Path(tempfile.mkdtemp())  # el caso de la prueba; el problem_case.json real no se toca
(api.DATA / "problem_case.json").write_text(json.dumps({
    "nombre_del_cliente": {"value": "Bodegas Ebrosol, S.L."},
    "sector": {"value": "Vitivinícola (Bodegas Ebrosol), agroalimentario"}}), encoding="utf-8")
noticias = api.obtener_noticias()["noticias"]
assert pedidas and all("Ebrosol" not in urllib.parse.unquote_plus(u) for u in pedidas), pedidas
consultas = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["q"][0] for u in pedidas]
assert "vitivinícola inteligencia artificial when:7d" in consultas and "vitivinícola datos when:7d" in consultas
assert len(noticias) == 10 and all(n["del_sector"] for n in noticias[:6]) and not noticias[-1]["del_sector"]
assert all(n["imagen"].endswith(".jpg") if n["titulo"].endswith(" 1") else n["imagen"] is None for n in noticias)
assert {n["tema"] for n in noticias} <= {"ia", "datos", "cloud", "digital"}
# Con el caso en caché no vuelve a buscar
pedidas.clear()
assert api.obtener_noticias()["cache"] is True and not pedidas
# Si la búsqueda del sector falla, las generales
api._noticias_cache.clear()
fallan.add("vitivinícola")
noticias = api.obtener_noticias()["noticias"]
assert noticias and not any(n["del_sector"] for n in noticias), noticias
# Sin red del todo, las últimas que hubo; y sin sector, solo las generales
fallan.add("empresas")
assert api.obtener_noticias()["cache"] is True  # siguen en caché
api._noticias_cache["vitivinícola"]["timestamp"] = 0
assert api.obtener_noticias()["noticias"] == noticias
fallan.clear()
(api.DATA / "problem_case.json").write_text(json.dumps({"sector": {"value": ""}}), encoding="utf-8")
pedidas.clear()
assert not any(n["del_sector"] for n in api.obtener_noticias()["noticias"]) and len(pedidas) == 3
print("OK 4: las noticias buscan solo con el sector, sin el nombre del cliente, y si fallan salen las generales")

print("\nTodo bien: los fallos 3 y 127 están arreglados.")