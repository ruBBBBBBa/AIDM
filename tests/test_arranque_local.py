"""Comprueba el fallo 66: AIDM arranca y genera el informe sin pedir nada a internet que no sean los modelos.

- LiteLLM usa la tabla de modelos que trae instalada, en vez de bajarla de GitHub en cada arranque.
- CrewAI no manda telemetría.
- La fuente del informe va dentro del HTML (assets/fonts), sin Google Fonts.

No llama a ningún LLM.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_arranque_local.py
"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import base64
import re
import tempfile

# 1) Al cargar gateway (lo primero que carga graph.py), LiteLLM y CrewAI no abren ninguna conexión. Va en un proceso
#    aparte, con las variables quitadas, para que cuente como un arranque de verdad.
PRUEBA = """
import os, socket, sys
conexiones = []
_conectar = socket.socket.connect
def conectar(self, direccion):
    conexiones.append(direccion)
    return _conectar(self, direccion)
socket.socket.connect = conectar
sys.path.insert(0, ".")
import gateway
from crewai.telemetry import Telemetry
print(os.environ["LITELLM_LOCAL_MODEL_COST_MAP"], Telemetry._is_telemetry_disabled(), conexiones)
"""
entorno = {k: v for k, v in os.environ.items()
           if k not in ("LITELLM_LOCAL_MODEL_COST_MAP", "CREWAI_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED")}
salida = subprocess.run([sys.executable, "-c", PRUEBA], capture_output=True, text=True, env=entorno, cwd=RAIZ,
                        encoding="utf-8", errors="replace", timeout=300)
assert salida.returncode == 0, salida.stderr[-2000:]
assert salida.stdout.strip().splitlines()[-1] == "True True []", salida.stdout[-500:]
print("OK 1: al arrancar, LiteLLM usa su tabla de modelos local, CrewAI no manda telemetría y no se abre ninguna conexión")

# 2) La fuente va dentro del informe: sin enlaces a Google Fonts y con las dos fuentes en base64, tal cual
from utils import FUENTES, fuentes_locales

plantilla = Path("data_structures/solution_builder_report.html").read_text(encoding="utf-8")
assert "fonts.googleapis.com" in plantilla, "la plantilla ya no usa Google Fonts: revisa este test"
html = fuentes_locales(plantilla)
assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
incrustadas = re.findall(r"font-weight:(\d+);src:url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\)", html)
assert [peso for peso, _ in incrustadas] == ["400", "700"], [peso for peso, _ in incrustadas]
for peso, datos in incrustadas:
    assert base64.b64decode(datos) == (FUENTES / f"space-mono-{peso}.woff2").read_bytes()
assert html.index("@font-face") < html.index("</head>")
assert fuentes_locales(html) == html, "pasarla dos veces no debe cambiar nada"
assert "font-family: 'Space Mono', monospace;" in html, "el resto del CSS se queda igual"
print("OK 2: el informe lleva Space Mono dentro (400 y 700) y ya no pide nada a Google Fonts")

# 3) Sin los archivos de la fuente, el informe se queda como estaba (con Google Fonts), sin romperse
import utils

utils.FUENTES = Path(tempfile.mkdtemp())
assert fuentes_locales(plantilla) == plantilla
utils.FUENTES = FUENTES
print("OK 3: si faltan los archivos de la fuente, el informe sale igual que antes")

print("\nTodo bien: el fallo 66 está arreglado.")
