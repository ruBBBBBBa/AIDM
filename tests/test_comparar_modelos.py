"""Comprueba pruebas_modelos/comparar.py, el script que compara modelos con las llamadas grabadas.

No llama a ningún LLM ni a los proveedores: se sustituyen litellm.completion y las consultas de modelos por
versiones simuladas, y los resultados van a una carpeta temporal.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_comparar_modelos.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
sys.path.insert(0, str(RAIZ / "pruebas_modelos"))
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

import contextlib
import io
import json
import tempfile
import urllib.error
from types import SimpleNamespace

import comparar

for clave in [c for c in os.environ if c.startswith("MODELO_")]:
    os.environ.pop(clave, None)

COSTES = json.dumps({
    "componentes": [{"componente": "Azure Data Lake Storage Gen2", "categoria": "infraestructura",
                     "precio_unitario_eur": 0.02, "unidades": 1000, "unidad": "GB", "periodicidad": "mensual",
                     "referencia": "Precio público"}],
    "resumen_ejecutivo": {"meses_proyecto": 6, "presupuesto_cliente_max_eur": 100000, "contingencia_pct": 0,
                          "justificacion": "Almacenamiento del piloto."},
    "supuestos": []}, ensure_ascii=False)
DISCOVERY = json.dumps({"updates": [{"field_key": "sector", "value": "Logística", "operation": "replace"}],
                        "assistant_reply": "¿Cuántos empleados tenéis?", "update_status": None}, ensure_ascii=False)

# 1) evaluar(): JSON válido o no en las tareas que lo piden; los textos, a juicio
sirve, nota = comparar.evaluar("costes", COSTES)
assert sirve and nota.startswith("1 componentes (0 con tarifa); 120 € en 6 meses"), nota
assert comparar.evaluar("costes", "Aquí tienes los costes: Data Lake, 20 €/mes")[0] is False
sirve, nota = comparar.evaluar("discovery", DISCOVERY)
assert sirve and "1 campos" in nota and "¿Cuántos empleados" in nota, nota
assert comparar.evaluar("discovery", "No sé qué decir")[0] is False
sirve, nota = comparar.evaluar("arquitectura", "Propuesta con Data Lake. Coste: 2.000 €/mes.")
assert sirve is None and "1 importes" in nota, nota
assert comparar.evaluar("terraform", 'resource "azurerm_storage_account" "x" {}') == (True, "1 recursos")
print("OK 1: el JSON de costes y de discovery se comprueba; los textos se dejan a juicio, avisando de los importes")

# 2) Una comparación entera, con una grabación de tres tareas y tres modelos simulados
carpeta = Path(tempfile.mkdtemp())
grabacion = carpeta / "grabacion.jsonl"
registros = [
    {"tarea": "discovery", "modelo": "mistral/ministral-14b-2512", "mensajes": [{"role": "user", "content": "Turno 1"}],
     "respuesta": DISCOVERY, "tokens_entrada": 900, "tokens_salida": 80, "segundos": 1.5},
    {"tarea": "discovery", "modelo": "mistral/ministral-14b-2512", "mensajes": [{"role": "user", "content": "Turno 2"}],
     "respuesta": DISCOVERY, "tokens_entrada": 950, "tokens_salida": 70, "segundos": 1.2},
    {"tarea": "costes", "modelo": "mistral/ministral-14b-2512", "mensajes": [{"role": "user", "content": "Valora"}],
     "respuesta": "sin JSON", "tokens_entrada": 6000, "tokens_salida": 2000, "segundos": 20.0},
    {"tarea": "arquitectura", "modelo": "mistral/mistral-large-latest", "mensajes": [{"role": "user", "content": "Escribe"}],
     "respuesta": "Arquitectura <propuesta> & más", "tokens_entrada": 3000, "tokens_salida": 900, "segundos": 9.0},
    {"tarea": "costes", "modelo": "x", "mensajes": [], "respuesta": "", "error": "RateLimitError"},  # no se repite
]
grabacion.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in registros), encoding="utf-8")
llamadas = []


def completion_simulada(model, messages, **kwargs):
    llamadas.append((model, messages[-1]["content"]))
    if model == "groq/caido":
        raise RuntimeError("RateLimitError: Request too large")
    texto = {"Valora": COSTES, "Escribe": "Otra arquitectura"}.get(messages[-1]["content"], DISCOVERY)
    if model == "groq/malo":
        texto = "Lo siento, no puedo."
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=texto))],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))


comparar.litellm.completion = completion_simulada
comparar.RESULTADOS = carpeta / "resultados"
comparar.PAUSA_MINIMA = {}
with contextlib.redirect_stdout(io.StringIO()):
    salida = comparar.comparar(grabacion, modelos=["groq/bueno", "groq/malo", "groq/caido"])
resumen = (salida / "resumen.md").read_text(encoding="utf-8")
html = (salida / "comparacion.html").read_text(encoding="utf-8")
filas = [json.loads(l) for l in (salida / "llamadas.jsonl").read_text(encoding="utf-8").splitlines()]
assert len(llamadas) == 4 * 3, llamadas  # 2 turnos de discovery, costes y arquitectura, con 3 modelos
assert len(filas) == 4 * 4  # y la respuesta grabada de cada una
assert "| groq/bueno | 2 de 2 |" in resumen and "| groq/malo | 0 de 2 |" in resumen, resumen
assert "| mistral/ministral-14b-2512 (grabado) | no |" in resumen  # el costes grabado no traía JSON
assert "| groq/bueno | sí |" in resumen and "| groq/caido | no |" in resumen
assert "RuntimeError: RateLimitError: Request too large" in resumen
assert "| groq/bueno | a juicio |" in resumen  # arquitectura
assert "Arquitectura &lt;propuesta&gt; &amp; más" in html and "<pre>" in html
print("OK 2: cada tarea grabada se repite con cada modelo; el resumen dice cuál sirve y el HTML las pone lado a lado")

# 3) --comprobar: cada modelo del yaml contra lo que da su proveedor
def falla():
    raise urllib.error.URLError("sin red")


comparar.PROVEEDORES = {"groq": lambda: {"openai/gpt-oss-20b"}, "mistral": lambda: None,
                        "gemini": falla, "sambanova": lambda: {"gpt-oss-120b", "DeepSeek-V3.1"},
                        "cloudflare": lambda: {"@cf/openai/gpt-oss-120b"}, "nvidia_nim": lambda: set()}
log = io.StringIO()
with contextlib.redirect_stdout(log):
    faltan = comparar.comprobar()
texto = log.getvalue()
assert "OK                     groq/openai/gpt-oss-20b  (extraccion, decision)" in texto, texto
assert "sin clave en el .env   mistral/ministral-14b-2512" in texto
assert "no se pudo consultar (<urlopen error sin red>) gemini/gemini-3.6-flash" in texto
assert "NO ESTÁ con tu clave   cloudflare/@cf/qwen/qwen2.5-coder-32b-instruct  (codigo)" in texto
assert faltan == 1, faltan
print("OK 3: --comprobar dice qué modelos del yaml no tiene su proveedor, cuáles no tienen clave y qué no se pudo consultar")

# 4) La lista de modelos de cada proveedor: Mistral da también los alias, y Gemini y Cloudflare van por páginas
paginas = {"https://api.mistral.ai/v1/models": {"data": [{"id": "mistral-large-2512", "aliases": ["mistral-large-latest"]}]},
           "gemini1": {"models": [{"name": "models/gemini-3.8-flash"}], "nextPageToken": "p2"},
           "gemini2": {"models": [{"name": "models/gemini-3.5-flash-lite"}]},
           "cf1": {"result": [{"name": f"@cf/m{i}"} for i in range(100)]}, "cf2": {"result": [{"name": "@cf/openai/gpt-oss-120b"}]}}


def pedir_simulado(url, cabeceras=None):
    if "generativelanguage" in url:
        return paginas["gemini2" if "pageToken=p2" in url else "gemini1"]
    if "cloudflare" in url:
        return paginas["cf2" if "page=2" in url else "cf1"]
    return paginas[url]


comparar._pedir_json = pedir_simulado
os.environ.update(MISTRAL_API_KEY="x", GEMINI_API_KEY="x", CLOUDFLARE_API_KEY="x", CLOUDFLARE_ACCOUNT_ID="cuenta")
assert comparar._tipo_openai("https://api.mistral.ai/v1", "MISTRAL_API_KEY") == {"mistral-large-2512", "mistral-large-latest"}
assert comparar._gemini() == {"gemini-3.8-flash", "gemini-3.5-flash-lite"}
assert len(comparar._cloudflare()) == 101 and "@cf/openai/gpt-oss-120b" in comparar._cloudflare()
print("OK 4: --listar lee los modelos de cada proveedor, con los alias de Mistral y las páginas de Gemini y Cloudflare")

print("\nTodo bien: el script de comparación funciona (en la parte que se puede comprobar sin llamar a los modelos).")