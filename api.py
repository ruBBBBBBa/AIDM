"""API del frontend de AIDM.

Arranque, desde la carpeta AIDM (donde están graph.py e interface.py):
    pip install fastapi uvicorn
    python api.py
Con log:  python api.py > ejecucion_grabada_2026-11-12.log 2>&1
"""
import html as html_lib
import inspect
import ipaddress
import json
import re
import shutil
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Igual que en interface.py: con la salida a un archivo en Windows, un print raro no debe tumbar el turno.
for _flujo in (sys.stdout, sys.stderr):
    if hasattr(_flujo, "reconfigure"):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

from graph import run_graph  # los módulos pesados se cargan una sola vez, al arrancar el servidor
from utils import INFORME_GENERADO, fuentes_locales  # fallo 66: el informe sin enlaces a Google Fonts

DATA = Path("data_structures")
REPORT_PATH = INFORME_GENERADO  # fallo 116: el informe de la ejecución, no la plantilla

app = FastAPI(title="AIDM Discovery Agent")
from fastapi.staticfiles import StaticFiles
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
lock = threading.Lock()  # problem_case.json es un único archivo: una ejecución a la vez

# ---- Estado compartido con el frontend (GET /api/estado) ----
_estado_lock = threading.Lock()
_n_avisos = 0  # contador que no se reinicia: el frontend se queda con el último id que ya pintó
estado = {"ocupado": False, "fraccion": 0.0, "texto": "", "avisos": [], "completado": False}
informe = None  # {"report_html": ..., "filename": ...} cuando el caso está completo y el informe hecho (fallo 48)


def pintar_progreso(fraccion, texto=""):
    """Fallo 94: guarda el avance real (0..1) y su texto para que la web lo lea."""
    with _estado_lock:
        estado["fraccion"] = max(0.0, min(1.0, float(fraccion)))
        estado["texto"] = str(texto or "")


def avisar(texto):
    """Fallo 72: aviso de la plataforma. Va al estado, nunca al historial que leen los agentes."""
    global _n_avisos
    with _estado_lock:
        _n_avisos += 1
        estado["avisos"] = (estado["avisos"] + [{"id": _n_avisos, "texto": str(texto)}])[-20:]


def _extras_graph():
    """Pasa a run_graph solo los parámetros que acepta, para no romper si el nombre cambia."""
    params = inspect.signature(run_graph).parameters
    extras = {}
    if "avisar" in params:
        extras["avisar"] = avisar
    for nombre in ("pintar_progreso", "progreso", "on_progress"):
        if nombre in params:
            extras[nombre] = pintar_progreso
            break
    return extras


def _leer_informe():
    html = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
    return fuentes_locales(html) if html else ""


class ChatIn(BaseModel):
    prompt: str
    history: list[dict[str, str]]  # incluye ya el mensaje nuevo del usuario, como en Streamlit


@app.get("/")
def home():
    return FileResponse("index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/chat")
def chat(body: ChatIn):
    global informe
    with lock:
        # Fallo 48: con el informe ya hecho, una segunda petición (otra pestaña, o en cola) lo devuelve sin llamar a run_graph.
        if informe is not None:
            return {"reply": "", "completed": True, **informe}

        with _estado_lock:
            estado.update(ocupado=True, fraccion=0.0, texto="")
        try:
            raw = run_graph(body.prompt, chat_history=body.history, **_extras_graph())
        except Exception as e:
            return {"error": str(e)}
        finally:
            with _estado_lock:
                estado["ocupado"] = False

        res = raw if isinstance(raw, dict) else {"assistant_response": str(raw)}
        out = {
            "reply": res.get("assistant_response", ""),
            "completed": bool(res.get("completion_reached")),
        }
        if out["completed"]:
            cliente = res.get("problem_case", {}).get("nombre_del_cliente", {}).get("value", "")
            cliente = re.sub(r"[^\w-]+", "_", cliente).strip("_") or "cliente"
            informe = {
                "report_html": _leer_informe(),
                "filename": f"informe_aidm_{cliente}_{datetime.now():%Y%m%d_%H%M}.html",
            }
            with _estado_lock:
                estado.update(completado=True, fraccion=1.0)
            out.update(informe)
        return out


@app.post("/api/reset")
def reset():
    """Nueva sesión (solo el botón): copia la plantilla en blanco encima de problem_case.json.
    Si hay una ejecución en curso no espera ni borra nada: devuelve 409."""
    global informe
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "Hay una ejecución en curso. Espera a que termine."}, status_code=409)
    try:
        shutil.copyfile(DATA / "problem_case_recovery.json", DATA / "problem_case.json")
        informe = None
        with _estado_lock:
            estado.update(ocupado=False, fraccion=0.0, texto="", avisos=[], completado=False)
    finally:
        lock.release()
    return {"ok": True}


@app.get("/api/caso")
def caso():
    """Estado actual de la entrevista, para la ficha del proyecto."""
    ruta = DATA / "problem_case.json"
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


@app.get("/api/estado")
def get_estado():
    """Progreso real, avisos de la plataforma y si el informe ya está hecho. Se sondea cada 3 s."""
    with _estado_lock:
        return {**estado, "avisos": list(estado["avisos"])}


@app.get("/api/informe")
def get_informe():
    """El informe ya generado, para recuperarlo tras recargar la página."""
    if informe is None:
        return JSONResponse({"error": "Todavía no hay informe."}, status_code=404)
    return informe


# ==========================================

# AIDM TECH NEWS - Noticias tecnológicas

# Fallo 127: primero las del sector del caso y, si no llegan, las generales de datos e IA. Al buscador solo va el
# sector, nunca el nombre del cliente ni otro dato del caso. Si algo falla, las generales: nunca rompe el informe.
# Parte 3: Bing News RSS (trae la URL real del artículo) con Google News de reserva, y una frase de cada noticia: la
# descripción que publica el propio medio (og:description o meta description), nunca texto del cuerpo. El RSS de Bing
# no está documentado: revisarlo antes de producción.

# ==========================================

_noticias_lock = threading.Lock()

_noticias_cache = {}  # sector ("" = sin sector) → {"timestamp": …, "items": […], "frases": "pendientes" | "hechas"}

NOTICIAS_MAX = 10
NOTICIAS_CACHE_S = 15 * 60
NOTICIAS_TIEMPO_S = 5
FRASE_TIEMPO_S = 3
FRASE_MAX = 200
PAGINA_MAX_BYTES = 200_000
USER_AGENT_NOTICIAS = "AIDM-Tech-News/1.0 (noticias del sector para AIDM)"

NEWS_TOPICS = {  # las generales, como hasta ahora

    "IA y automatización": "inteligencia artificial empresas",

    "Cloud": "cloud computing empresas",

    "Datos y analítica": "big data analytics empresas"

}

SECTOR_TOPICS = {  # van detrás del sector: «vitivinícola inteligencia artificial»

    "IA y automatización": "inteligencia artificial",

    "Datos y analítica": "análisis de datos",  # con «datos» solo salían noticias del sector sin datos

    "Digitalización": "digitalización"

}

# El tema de cada categoría, para la etiqueta y el color de la tarjeta
_TEMAS = {"IA y automatización": "ia", "Cloud": "cloud", "Datos y analítica": "datos", "Digitalización": "digital"}


def _rss(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AIDM-Tech-News"})
    with urllib.request.urlopen(req, timeout=4) as respuesta:
        return ET.fromstring(respuesta.read(1_000_000))


def _noticia(tema, del_sector, titulo, enlace, fecha, fuente, url_medio):
    """Una noticia del RSS, o None si no vale. url_medio: la del artículo, si se conoce, para buscar su frase."""
    titulo, fuente = (titulo or "").strip(), (fuente or "").strip()
    if fuente and titulo.endswith(" - " + fuente):  # Google News repite la fuente al final del título
        titulo = titulo[:-len(fuente) - 3].rstrip()
    titulo = _limpiar(titulo)
    if not titulo or not (enlace or "").startswith("https://"):
        return None
    # Parte 4: un título genérico («Suscríbete a nuestro Boletín») no se enseña; si se puede leer la página, se
    # cambia por el suyo y, si no, la noticia se descarta
    generico = _es_generico(titulo)
    if generico and not url_medio:
        return None
    try:
        fecha_iso = parsedate_to_datetime(fecha).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, IndexError):
        fecha_iso = None
    return {"titulo": titulo, "url": enlace, "fuente": fuente or "Google News", "fecha": fecha_iso,
            "categoria": tema, "tema": _TEMAS.get(tema, "datos"), "del_sector": del_sector, "frase": None,
            "url_medio": url_medio, "titulo_pendiente": generico}


def _leer_bing(tema, busqueda, del_sector):
    """Bing News RSS: su enlace lleva la URL real del artículo en el parámetro «url»."""
    query = urllib.parse.urlencode({"q": busqueda, "format": "rss", "cc": "es", "setlang": "es"})
    noticias = []
    for item in _rss("https://www.bing.com/news/search?" + query).findall("./channel/item"):
        enlace = item.findtext("link", "").strip()
        real = urllib.parse.parse_qs(urllib.parse.urlparse(enlace).query).get("url", [""])[0]
        fuente = next((n.text for n in item if n.tag.endswith("}Source") and n.text), "")
        noticia = _noticia(tema, del_sector, item.findtext("title", ""), real, item.findtext("pubDate", ""), fuente,
                           real)
        if noticia:
            noticias.append(noticia)
        if len(noticias) == 5:
            break
    return noticias


def _leer_google(tema, busqueda, del_sector):
    """Google News RSS, la reserva: sus enlaces solo redirigen con JavaScript, así que van sin frase."""
    query = urllib.parse.urlencode({"q": busqueda + " when:7d", "hl": "es", "gl": "ES", "ceid": "ES:es"})
    noticias = []
    for item in _rss("https://news.google.com/rss/search?" + query).findall("./channel/item")[:5]:
        noticia = _noticia(tema, del_sector, item.findtext("title", ""), item.findtext("link", "").strip(),
                           item.findtext("pubDate", ""), item.findtext("source", ""), None)
        if noticia:
            noticias.append(noticia)
    return noticias


def _leer_rss(tema, busqueda, del_sector=False):
    """Bing primero; si falla o no devuelve nada, Google News."""
    try:
        noticias = _leer_bing(tema, busqueda, del_sector)
    except Exception:
        noticias = []
    return noticias or _leer_google(tema, busqueda, del_sector)


# ---- La frase de cada noticia: solo la descripción que publica el medio ----

def _url_permitida(url):
    """Solo https y a un servidor público: nada de localhost ni IPs privadas, reservadas o de enlace local."""
    partes = urllib.parse.urlparse(url or "")
    if partes.scheme != "https" or not partes.hostname or partes.hostname.lower() == "localhost":
        return False
    try:
        direcciones = {d[4][0] for d in socket.getaddrinfo(partes.hostname, partes.port or 443)}
    except (OSError, UnicodeError):
        return False
    for direccion in direcciones:
        ip = ipaddress.ip_address(direccion.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(direcciones)


class _Redirecciones(urllib.request.HTTPRedirectHandler):
    """Como mucho 3 redirecciones, y cada una a una URL permitida."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_permitida(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirección no permitida", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_abridor = urllib.request.build_opener(_Redirecciones)


def _leer_pagina(url):
    """Las primeras 200 KB de la página del medio, o "" si no se puede o no se debe leer."""
    if not _url_permitida(url):
        return ""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT_NOTICIAS, "Accept": "text/html"})
    with _abridor.open(req, timeout=FRASE_TIEMPO_S) as respuesta:
        if "html" not in (respuesta.headers.get("Content-Type") or "html"):
            return ""
        return respuesta.read(PAGINA_MAX_BYTES).decode(respuesta.headers.get_content_charset() or "utf-8", "replace")


_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATRIBUTO = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _limpiar(texto):
    """Parte 4: texto plano. Algunos medios escapan el HTML dos veces («&lt;p&gt;Llegamos una vez m&amp;aacute;s»):
    se desescapa hasta que no cambie (3 veces como mucho), se quitan las etiquetas y se normalizan los espacios."""
    texto = texto or ""
    for _ in range(3):
        nuevo = html_lib.unescape(texto)
        if nuevo == texto:
            break
        texto = nuevo
    return " ".join(re.sub(r"<[^>]*>", " ", texto).split())


def _cabecera(html):
    """Las etiquetas <meta> (og:description, description, og:title) y el <title> de la cabecera; nunca el cuerpo."""
    cabecera = re.split(r"</head\s*>|<body\b", html or "", maxsplit=1, flags=re.IGNORECASE)[0]
    datos = {}
    for etiqueta in _META.findall(cabecera):
        attrs = {k.lower(): (a if a is not None else b) for k, a, b in _ATRIBUTO.findall(etiqueta)}
        clave = (attrs.get("property") or attrs.get("name") or "").lower()
        if clave in ("og:description", "description", "og:title") and _limpiar(attrs.get("content")):
            datos.setdefault(clave, _limpiar(attrs["content"]))
    titulo = re.search(r"<title[^>]*>(.*?)</title>", cabecera, re.IGNORECASE | re.DOTALL)
    if titulo and _limpiar(titulo.group(1)):
        datos["title"] = _limpiar(titulo.group(1))
    return datos


def _sin_medio_final(texto):
    """« | Cadena SER» o « - El Medio» al final: el nombre del medio, que ya va aparte."""
    return re.sub(r"\s+[|\-–—]\s+[^|\-–—]{1,40}$", "", texto) if len(texto) > 25 else texto


def _frase(html, titulo=""):
    """og:description o, si no, meta description, en una frase de unos FRASE_MAX caracteres; None si no hay. Solo se
    miran las etiquetas <meta> de la cabecera: nunca el texto del cuerpo."""
    metas = _cabecera(html)
    texto = metas.get("og:description") or metas.get("description")
    if not texto:
        return None
    texto = re.sub(r"\s+\|\s+[^|]{1,40}$", "", texto)  # « | Cadena SER»: el nombre del medio, que ya va aparte
    primera = re.match(r"(.{60,}?[.!?])(?=\s|$)", texto)  # una frase, si la primera tiene sustancia
    texto = primera.group(1) if primera else texto
    if len(texto) > FRASE_MAX:
        texto = texto[:FRASE_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return None if texto.lower().rstrip(".…") == (titulo or "").lower().rstrip(".…") else texto


# Parte 4: títulos que no son de la noticia (Bing toma a veces el de un bloque de la página)
_GENERICO = re.compile(r"\b(?:suscr[ií]b[ea]te|suscripci[oó]n|bolet[ií]n|newsletter|cookies?|inicia(?:r)? sesi[oó]n|"
                       r"log ?in|reg[ií]strate|aviso legal|pol[ií]tica de privacidad|p[aá]gina no encontrada|"
                       r"error 404|acceso denegado)\b", re.IGNORECASE)


def _es_generico(titulo):
    return not titulo or bool(_GENERICO.search(titulo))


def _palabras(texto):
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(c))
    return {p for p in re.findall(r"\w+", sin_tildes) if len(p) > 3}


def _se_parecen(a, b):
    """Comparten al menos la mitad de las palabras con sustancia del más corto."""
    pa, pb = _palabras(a), _palabras(b)
    return bool(pa and pb) and len(pa & pb) >= 0.5 * min(len(pa), len(pb))


def _titulo_fiable(titulo_rss, html):
    """El título que se enseña: el del RSS si vale, el de la página (og:title o <title>, sin el medio final) si el del
    RSS es genérico o se le parece poco, o None si no hay ninguno fiable (la noticia se descarta)."""
    cabecera = _cabecera(html)
    de_la_pagina = _sin_medio_final(cabecera.get("og:title") or cabecera.get("title") or "")
    if len(_palabras(de_la_pagina)) >= 3 and not _es_generico(de_la_pagina) and (  # solo el nombre del sitio no vale
            _es_generico(titulo_rss) or not _se_parecen(titulo_rss, de_la_pagina)):
        return de_la_pagina
    return None if _es_generico(titulo_rss) else titulo_rss


def _frase_de(noticia):
    """(frase, título fiable) de la noticia, leyendo su página una sola vez."""
    try:
        html = _leer_pagina(noticia["url_medio"])
    except Exception:
        html = ""
    titulo = _titulo_fiable(noticia["titulo"], html)
    return _frase(html, titulo or ""), titulo


def _completar_frases(sector):
    """Busca en paralelo la frase de cada noticia (FRASE_TIEMPO_S como mucho cada una) y la guarda en la caché."""
    with _noticias_lock:
        entrada = _noticias_cache.get(sector)
        pendientes = [n for n in (entrada or {}).get("items", []) if n.get("url_medio") and not n.get("frase")]
    frases = {}
    if pendientes:
        pool = ThreadPoolExecutor(max_workers=min(5, len(pendientes)))
        try:
            futuros = {n["url"]: pool.submit(_frase_de, n) for n in pendientes}
            for url, futuro in futuros.items():
                try:
                    frases[url] = futuro.result(timeout=FRASE_TIEMPO_S + 1)
                except Exception:
                    continue
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    with _noticias_lock:
        if entrada is not None:
            items = []
            for n in entrada["items"]:
                frase, titulo = frases.get(n["url"], (n.get("frase"), None if n.get("titulo_pendiente") else n["titulo"]))
                if titulo:  # sin título fiable, la noticia se descarta
                    items.append({**n, "titulo": titulo, "frase": frase or n.get("frase"), "titulo_pendiente": False})
            entrada["items"] = items
            entrada["frases"] = "hechas"


def _lanzar_frases(sector):
    threading.Thread(target=_completar_frases, args=(sector,), daemon=True).start()


def _sector_del_caso():
    """El sector del caso, para buscar, o "" si aún no lo hay. Solo el sector: sin paréntesis, hasta la primera coma
    y sin ninguna palabra del nombre del cliente, por si el sector lo repite."""
    try:
        caso = json.loads((DATA / "problem_case.json").read_text(encoding="utf-8"))
        sector = str((caso.get("sector") or {}).get("value") or "")
        cliente = str((caso.get("nombre_del_cliente") or {}).get("value") or "")
    except Exception:  # se lee mientras la ejecución lo escribe: a medias, sin sector
        return ""
    if sector.startswith("Confirmado por el usuario"):  # «no lo sabe» o «no aplica»
        return ""
    sector = re.split(r"[,;:./\n]", re.sub(r"\([^)]*\)?", " ", sector))[0]
    del_cliente = {p.lower() for p in re.findall(r"\w+", cliente) if len(p) > 2}
    palabras = [p for p in re.findall(r"[\w-]+", sector) if p.lower() not in del_cliente]
    return " ".join(palabras[:5]).lower()


def _sin_repetir(noticias):
    vistos, unicas = set(), []
    for noticia in noticias:
        clave = noticia["titulo"].lower()
        if clave not in vistos:
            vistos.add(clave)
            unicas.append(noticia)
    return unicas


def _buscar(sector):
    """Las del sector primero (por fecha) y, detrás, las generales hasta NOTICIAS_MAX. Como mucho NOTICIAS_TIEMPO_S."""
    busquedas = [(tema, f"{sector} {busqueda}", True) for tema, busqueda in SECTOR_TOPICS.items()] if sector else []
    busquedas += [(tema, busqueda, False) for tema, busqueda in NEWS_TOPICS.items()]
    resultados = {True: [], False: []}
    limite = time.time() + NOTICIAS_TIEMPO_S
    pool = ThreadPoolExecutor(max_workers=len(busquedas))
    try:
        futuros = [(del_sector, pool.submit(_leer_rss, tema, busqueda, del_sector))
                   for tema, busqueda, del_sector in busquedas]
        for del_sector, futuro in futuros:
            try:
                resultados[del_sector].extend(futuro.result(timeout=max(0.1, limite - time.time())))
            except Exception:
                continue
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # no espera a una búsqueda lenta
    por_fecha = lambda grupo: sorted(grupo, key=lambda noticia: noticia["fecha"] or "", reverse=True)
    return _sin_repetir(por_fecha(resultados[True]) + por_fecha(resultados[False]))[:NOTICIAS_MAX]


def _para_la_web(noticias):
    """Sin la URL interna ni las noticias cuyo título aún se está comprobando (parte 4)."""
    return [{k: v for k, v in n.items() if k not in ("url_medio", "titulo_pendiente")}
            for n in noticias if not n.get("titulo_pendiente")]


@app.get("/api/noticias")

def obtener_noticias():

    ahora = time.time()

    sector = _sector_del_caso()

    with _noticias_lock:

        cache = _noticias_cache.get(sector)

        if cache and ahora - cache["timestamp"] < NOTICIAS_CACHE_S:

            return {

                "noticias": _para_la_web(cache["items"]),

                "frases_pendientes": cache.get("frases") == "pendientes",

                "actualizado": datetime.now(timezone.utc).isoformat(),

                "cache": True

            }

    try:

        noticias = _buscar(sector)

    except Exception:

        noticias = []

    pendientes = False

    with _noticias_lock:

        if noticias:

            pendientes = any(n["url_medio"] for n in noticias)

            _noticias_cache[sector] = {"timestamp": ahora, "items": noticias,
                                       "frases": "pendientes" if pendientes else "hechas"}

        else:  # sin red: las últimas que hubo, del sector o las generales

            noticias = (_noticias_cache.get(sector) or _noticias_cache.get("") or {}).get("items", [])

    if pendientes:

        _lanzar_frases(sector)  # primero se pintan las tarjetas; la frase llega en la siguiente petición

    return {

        "noticias": _para_la_web(noticias),

        "frases_pendientes": pendientes,

        "actualizado": datetime.now(timezone.utc).isoformat(),

        "cache": False

    }

if __name__ == "__main__":
    import webbrowser

    import uvicorn

    if not _extras_graph():
        print("AVISO: run_graph no acepta 'avisar' ni un parámetro de progreso; la barra y los avisos no se alimentarán.")
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()
    uvicorn.run(app, port=8000)
