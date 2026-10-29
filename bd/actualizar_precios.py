"""Actualiza los precios de bd/precios_cloud.csv con la Azure Retail Prices API (fallo 53).

La API es pública, no pide clave y devuelve los precios de lista (pago por uso, sin descuentos) en euros. Para cada
fila con api_medidor, busca ese medidor en su región y se queda con el de su producto y su SKU, de tipo Consumption.
Si el primer tramo es gratis, el precio es el del primer tramo de pago y lo gratuito al mes, el mínimo de ese tramo.

Las filas que no se pueden actualizar se quedan como están y se dice por qué. La API redondea los precios en euros a
4 decimales: un precio por debajo de 0,0001 € sale como 0 y esa fila no se toca.

Uso, desde la carpeta del proyecto:
    python bd/actualizar_precios.py            # actualiza el CSV y dice qué ha cambiado
    python bd/actualizar_precios.py --probar   # solo dice qué cambiaría, sin escribir

Para añadir un servicio, se añade su fila a mano con api_producto, api_sku, api_medidor y api_region tal como salen
en la API (https://prices.azure.com/api/retail/prices?currencyCode='EUR'&$filter=meterName eq '…') y se ejecuta este
script. «buscar» son las palabras (sin tildes, separadas por «|») con las que la arquitectura nombra el servicio.
"""
import argparse
import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

CSV_PRECIOS = Path(__file__).with_name("precios_cloud.csv")
API = "https://prices.azure.com/api/retail/prices"


def pedir(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as respuesta:
        return json.load(respuesta)


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
    cambios = 0
    for fila in filas:
        if not fila.get("api_medidor"):
            continue
        clave = (fila["api_medidor"], fila.get("api_region") or "westeurope")
        try:
            if clave not in consultas:
                consultas[clave] = consultar(*clave, pedir=pedir)
        except Exception as error:  # sin red, la API caída…: la fila se queda como está
            print(f"SIN CAMBIOS  {fila['id']}: no se pudo consultar la API ({error})")
            continue
        resultado, motivo = precio_de(consultas[clave], fila)
        if resultado is None:
            print(f"SIN CAMBIOS  {fila['id']}: {motivo}")
            continue
        precio, gratis = resultado
        antes = (float(fila["precio_eur"]), float(fila.get("gratis_al_mes") or 0))
        if (precio, gratis) != antes:
            cambios += 1
            print(f"CAMBIA      {fila['id']}: {numero(antes[0])} € → {numero(precio)} €"
                  + (f"; gratis {numero(antes[1])} → {numero(gratis)}" if gratis != antes[1] else ""))
        fila.update(precio_eur=numero(precio), gratis_al_mes=numero(gratis), fecha=hoy)
    if not probar:
        with open(CSV_PRECIOS, "w", encoding="utf-8", newline="") as f:
            escritor = csv.DictWriter(f, fieldnames=campos, delimiter=";", lineterminator="\n")
            escritor.writeheader()
            escritor.writerows(filas)
    print(f"\n{cambios} precio(s) cambiado(s)" + (" (sin escribir: --probar)" if probar else f"; fecha {hoy}"))
    return cambios


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # «€» en la consola de Windows
    argumentos = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    argumentos.add_argument("--probar", action="store_true", help="dice qué cambiaría, sin escribir el CSV")
    actualizar(probar=argumentos.parse_args().probar)