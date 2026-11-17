"""Actualiza los precios de bd/precios_cloud.csv con la Azure Retail Prices API (fallo 53) y la Price List Bulk API de AWS
(fallo 87, con la elección de SKU en bd/precios_aws.yaml), y da un informe de cobertura.

La API es pública, no pide clave y devuelve los precios de lista (pago por uso, sin descuentos) en euros. Para cada
fila con api_medidor, busca ese medidor en su región y se queda con el de su producto y su SKU, de tipo Consumption.
Si el primer tramo es gratis, el precio es el del primer tramo de pago y lo gratuito al mes, el mínimo de ese tramo.

Las filas que no se pueden actualizar se quedan como están y se dice por qué. La API redondea los precios en euros a
4 decimales: un precio por debajo de 0,0001 € sale como 0 y esa fila no se toca.

Uso, desde la carpeta del proyecto:
    python bd/actualizar_precios.py            # actualiza el CSV y dice qué ha cambiado
    python bd/actualizar_precios.py --probar   # solo dice qué cambiaría, sin escribir
    python bd/actualizar_precios.py --solo aws # solo AWS (o --solo azure)

Para añadir un servicio, se añade su fila a mano con api_producto, api_sku, api_medidor y api_region tal como salen
en la API (https://prices.azure.com/api/retail/prices?currencyCode='EUR'&$filter=meterName eq '…') y se ejecuta este
script. «buscar» son las palabras (sin tildes, separadas por «|») con las que la arquitectura nombra el servicio.
"""
import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

CSV_PRECIOS = Path(__file__).with_name("precios_cloud.csv")
API = "https://prices.azure.com/api/retail/prices"


def pedir(url: str, intentos: int = 3, espera: float = 5) -> dict:
    """Una página de la API. Si no responde a tiempo (WinError 10060), se cae la conexión o la API está ocupada (429 o
    5xx), lo intenta otra vez, esperando 5 s y luego 10 s; un error que no se arregla repitiendo (404…) sale a la primera."""
    for intento in range(1, intentos + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as respuesta:
                return json.load(respuesta)
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500 or intento == intentos:
                raise
            motivo = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if intento == intentos:
                raise
            motivo = getattr(error, "reason", error)
        print(f"            reintento {intento} de {intentos - 1}: la API no respondió ({motivo})")
        time.sleep(espera * intento)


def consultar(medidor: str, region: str, pedir=pedir) -> list[dict]:
    """Todos los precios de un medidor en una región, siguiendo las páginas."""
    filtro = f"armRegionName eq '{region}' and meterName eq '{medidor}'"
    url = f"{API}?currencyCode=%27EUR%27&$filter={urllib.parse.quote(filtro)}"
    items: list[dict] = []
    while url:
        datos = pedir(url)
        items += datos.get("Items") or []
        url = datos.get("NextPageLink")
    return items


def por_cada(unidad_api: str) -> float | None:
    """'1M' → 1000000; '10K' → 10000; '1K' → 1000; '10' → 10; '1 Hour', '1 GB/Month' o '1/Month' → 1."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([KM])?(?![A-Za-z])", unidad_api or "")
    if not m:
        return None
    return float(m.group(1)) * {"K": 1_000, "M": 1_000_000}.get(m.group(2) or "", 1)


def precio_de(items: list[dict], fila: dict) -> tuple[tuple[float, float] | None, str]:
    """(precio por paquete, gratis al mes en la unidad de la fila) o (None, motivo)."""
    validos = [i for i in items if i.get("type") == "Consumption" and i.get("productName") == fila["api_producto"]
               and (not fila.get("api_sku") or i.get("skuName") == fila["api_sku"])]
    if not validos:
        return None, "la API no trae ese producto y SKU"
    validos.sort(key=lambda i: i.get("tierMinimumUnits") or 0)
    unidad_api = validos[0].get("unitOfMeasure", "")
    paquete = por_cada(unidad_api)
    if paquete != float(fila["por_cada"]):
        return None, f"la API cobra por «{unidad_api}» y la tabla por {fila['por_cada']}"
    de_pago = next((i for i in validos if (i.get("retailPrice") or 0) > 0), None)
    if de_pago is None:
        return None, "la API da 0 € en todos los tramos (los precios por debajo de 0,0001 € salen redondeados a 0)"
    gratis = (de_pago.get("tierMinimumUnits") or 0) * paquete if not validos[0].get("retailPrice") else 0
    return (float(de_pago["retailPrice"]), float(gratis)), ""


def numero(valor: float) -> str:
    return f"{valor:.10g}"


def actualizar(probar: bool = False, pedir=pedir, hoy: str | None = None) -> int:
    """Actualiza el CSV y devuelve cuántas filas han cambiado."""
    hoy = hoy or date.today().isoformat()
    with open(CSV_PRECIOS, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f, delimiter=";")
        campos, filas = lector.fieldnames, list(lector)
    consultas: dict[tuple[str, str], list[dict]] = {}
    cambios = iguales = sin_api = sin_precio = 0
    for fila in filas:
        if not fila.get("api_medidor"):
            continue
        clave = (fila["api_medidor"], fila.get("api_region") or "westeurope")
        try:
            if clave not in consultas:
                consultas[clave] = consultar(*clave, pedir=pedir)
        except Exception as error:  # sin red, la API caída…: la fila se queda como está
            sin_api += 1
            print(f"SIN CAMBIOS  {fila['id']}: no se pudo consultar la API ({error})")
            continue
        resultado, motivo = precio_de(consultas[clave], fila)
        if resultado is None:
            sin_precio += 1
            print(f"SIN CAMBIOS  {fila['id']}: {motivo}")
            continue
        precio, gratis = resultado
        antes = (float(fila["precio_eur"]), float(fila.get("gratis_al_mes") or 0))
        if (precio, gratis) != antes:
            cambios += 1
            print(f"CAMBIA      {fila['id']}: {numero(antes[0])} € → {numero(precio)} €"
                  + (f"; gratis {numero(antes[1])} → {numero(gratis)}" if gratis != antes[1] else ""))
        else:  # comprobada y sin cambios: antes no se imprimía y parecía que no se había mirado
            iguales += 1
            print(f"IGUAL       {fila['id']}: {numero(precio)} €")
        fila.update(precio_eur=numero(precio), gratis_al_mes=numero(gratis), fecha=hoy)
    if not probar:
        with open(CSV_PRECIOS, "w", encoding="utf-8", newline="") as f:
            escritor = csv.DictWriter(f, fieldnames=campos, delimiter=";", lineterminator="\n")
            escritor.writeheader()
            escritor.writerows(filas)
    print(f"\n{cambios + iguales + sin_api + sin_precio} filas: {cambios} cambian, {iguales} igual, "
          f"{sin_precio} sin precio en la API y {sin_api} sin poder consultarla"
          + (" (sin escribir: --probar)" if probar else f"; fecha {hoy}"))
    if sin_api:
        print("Las que no se pudieron consultar se quedan con su precio y su fecha. Vuelve a ejecutarlo más tarde; si "
              "fallan todas, la red no deja llegar a prices.azure.com (VPN o proxy de la empresa).")
    return cambios


# --- Fallo 87 (parte 2): AWS, con la Price List Bulk API, y las licencias manuales ---
# Qué SKU va en cada fila está en bd/precios_aws.yaml, para revisarlo sin leer el código. Los archivos son públicos y sin
# claves, uno por servicio y región; solo se bajan los de la lista (nunca el índice entero de EC2). AWS da los precios
# en dólares: se guardan con su moneda (columnas moneda y precio_origen) y estimation_crew los pasa a euros al leerlos,
# con el tipo de data_structures/costes.yaml, así que si cambia el tipo no hay que volver a generar la tabla.
CONFIG_AWS = Path(__file__).with_name("precios_aws.yaml")
API_AWS = "https://pricing.us-east-1.amazonaws.com"
COLUMNAS_NUEVAS = ["moneda", "precio_origen"]
DIAS_LICENCIA = 90


def precio_aws(datos: dict, usagetype: str, desde: float = 0) -> tuple[float, str] | None:
    """(precio por unidad de la API en USD, unidad de la API) del tramo que empieza en `desde`, o None si no está."""
    for sku, producto in (datos.get("products") or {}).items():
        if (producto.get("attributes") or {}).get("usagetype") != usagetype:
            continue
        for termino in ((datos.get("terms") or {}).get("OnDemand") or {}).get(sku, {}).values():
            for dim in (termino.get("priceDimensions") or {}).values():
                if float(dim.get("beginRange") or 0) == float(desde) and "USD" in (dim.get("pricePerUnit") or {}):
                    return float(dim["pricePerUnit"]["USD"]), dim.get("unit", "")
    return None


def archivos_aws(ofertas: set[str], region: str, pedir=pedir) -> dict[str, dict]:
    """El archivo de precios de cada oferta en la región, o {"error": motivo}."""
    archivos = {}
    for oferta in sorted(ofertas):
        try:
            indice = pedir(f"{API_AWS}/offers/v1.0/aws/{oferta}/current/region_index.json")
            ruta = ((indice.get("regions") or {}).get(region) or {}).get("currentVersionUrl")
            archivos[oferta] = pedir(API_AWS + ruta) if ruta else {"error": f"{oferta} no tiene precios en {region}"}
        except Exception as error:  # sin red, la API caída…: esas filas se quedan como están
            archivos[oferta] = {"error": f"no se pudo consultar la API ({error})"}
    return archivos


def actualizar_aws(filas: list[dict], config: dict, pedir=pedir, hoy: str = "") -> dict[str, list[str]]:
    """Actualiza (o añade) en `filas` las de AWS de la configuración. Devuelve el informe: cambia, igual, nueva, sin_fila."""
    region = config.get("region", "eu-south-2")
    entradas = config.get("filas") or []
    archivos = archivos_aws({e["oferta"] for e in entradas}, region, pedir)
    por_id = {f["id"]: f for f in filas}
    informe = {"cambia": [], "igual": [], "nueva": [], "sin_fila": []}
    for e in entradas:
        datos = archivos[e["oferta"]]
        resultado = None if "error" in datos else precio_aws(datos, e["usagetype"], e.get("desde", 0))
        if resultado is None:
            motivo = datos.get("error") or f"no está la SKU {e['usagetype']} (tramo {e.get('desde', 0)})"
            informe["sin_fila"].append(f"{e['servicio']}: {motivo}")
            continue
        unitario, unidad_api = resultado
        precio = unitario * float(e.get("por_cada", 1))
        nueva = {"id": e["id"], "proveedor": "AWS", "servicio": e["servicio"], "buscar": "|".join(e.get("buscar") or []),
                 "concepto": e["concepto"], "unidad": e["unidad"], "por_cada": numero(float(e.get("por_cada", 1))),
                 "cobro": e["cobro"], "precio_eur": "", "gratis_al_mes": numero(float(e.get("gratis_al_mes", 0))),
                 "fuente": f"AWS Price List Bulk API ({region}, {e['oferta']}, {e['usagetype']}, por {unidad_api})",
                 "fecha": hoy, "api_producto": "", "api_sku": "", "api_medidor": "", "api_region": "",
                 "moneda": "USD", "precio_origen": numero(precio)}
        antes = por_id.get(e["id"])
        if antes is None:
            filas.append(nueva)
            informe["nueva"].append(f"{e['id']}: {numero(precio)} $ por {e['cobro']}")
        elif antes.get("precio_origen") and float(antes["precio_origen"]) == precio:
            informe["igual"].append(f"{e['id']}: {numero(precio)} $")
            antes.update(nueva)
        else:
            informe["cambia"].append(f"{e['id']}: {antes.get('precio_origen') or '—'} $ → {numero(precio)} $")
            antes.update(nueva)
    informe["sin_fila"] += [f"{s['servicio']}: {s['motivo']}" for s in config.get("sin_fila") or []]
    return informe


def licencias_viejas(filas: list[dict], hoy: str) -> list[str]:
    """Las filas manuales de licencias (sin API) con la fecha de hace más de 90 días: hay que revisar su precio a mano."""
    viejas = []
    for f in filas:
        if f.get("proveedor") != "Licencia" or not f.get("fecha"):
            continue
        try:
            dias = (date.fromisoformat(hoy) - date.fromisoformat(f["fecha"])).days
        except ValueError:
            continue
        if dias > DIAS_LICENCIA:
            viejas.append(f"{f['id']}: precio del {f['fecha']}, hace {dias} días")
    return viejas


def actualizar_todo(probar: bool = False, solo: str = "", pedir=pedir, hoy: str | None = None,
                    csv_precios: Path | None = None, config_aws: Path | None = None) -> dict:
    """Azure (si no es --solo aws), AWS y el informe de cobertura. Con probar, no escribe nada."""
    hoy = hoy or date.today().isoformat()
    csv_precios, config_aws = csv_precios or CSV_PRECIOS, config_aws or CONFIG_AWS
    if solo != "aws":
        actualizar(probar=probar, pedir=pedir, hoy=hoy)  # Azure, como siempre (escribe su parte si no es --probar)
    import yaml
    config = yaml.safe_load(config_aws.read_text(encoding="utf-8")) or {}
    with open(csv_precios, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f, delimiter=";")
        campos, filas = list(lector.fieldnames), list(lector)
    campos += [c for c in COLUMNAS_NUEVAS if c not in campos]
    informe = actualizar_aws(filas, config, pedir=pedir, hoy=hoy) if solo != "azure" else {}
    informe["licencias_viejas"] = licencias_viejas(filas, hoy)
    informe["licencias_sin_fila"] = [f"{s['servicio']}: {s['motivo']}" for s in config.get("licencias_sin_fila") or []]
    if not probar:
        with open(csv_precios, "w", encoding="utf-8", newline="") as f:
            escritor = csv.DictWriter(f, fieldnames=campos, delimiter=";", lineterminator="\n")
            escritor.writeheader()
            escritor.writerows({c: fila.get(c) or "" for c in campos} for fila in filas)
    titulos = {"cambia": "AWS, cambian de precio", "igual": "AWS, comprobadas y sin cambios", "nueva": "AWS, filas nuevas",
               "sin_fila": "AWS, sin fila", "licencias_viejas": "Licencias con el precio de hace más de 90 días",
               "licencias_sin_fila": "Licencias sin fila"}
    print("\nInforme de cobertura" + (" (sin escribir: --probar)" if probar else ""))
    for clave, titulo in titulos.items():
        if clave in informe:
            print(f"\n{titulo} ({len(informe[clave])}):")
            for linea in informe[clave] or ["(ninguna)"]:
                print(f"  - {linea}")
    return informe


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # «€» en la consola de Windows
    argumentos = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    argumentos.add_argument("--probar", action="store_true", help="dice qué cambiaría, sin escribir el CSV")
    argumentos.add_argument("--solo", choices=["aws", "azure"], default="", help="solo una nube (fallo 87)")
    opciones = argumentos.parse_args()
    actualizar_todo(probar=opciones.probar, solo=opciones.solo)