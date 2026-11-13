"""Corrige la detección de números españoles en el filtro de cifras de AIDM.

Uso: coloca este script en la raíz de AIDM y ejecuta:
    python corregir_numeros_aidm.py

Crea una copia .bak del archivo original antes de tocarlo.
No modifica .env ni credenciales. Puede volver a ejecutarse sin duplicar cambios.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil

HELPER = '''
# Interpretación conservadora de números escritos en español para el filtro antifabricación.
# Solo valida equivalencias numéricas: no cambia el texto guardado en los campos.
_UNIDADES_ES = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3,
    "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
    "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13,
    "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21,
    "veintiun": 21, "veintiuna": 21, "veintidos": 22,
    "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
    "veintinueve": 29, "treinta": 30, "cuarenta": 40,
    "cincuenta": 50, "sesenta": 60, "setenta": 70,
    "ochenta": 80, "noventa": 90, "cien": 100,
}
_DECENAS_ES = {"treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"}
_CENTENAS_ES = {
    "ciento": 100, "doscientos": 200, "doscientas": 200,
    "trescientos": 300, "trescientas": 300, "cuatrocientos": 400,
    "cuatrocientas": 400, "quinientos": 500, "quinientas": 500,
    "seiscientos": 600, "seiscientas": 600, "setecientos": 700,
    "setecientas": 700, "ochocientos": 800, "ochocientas": 800,
    "novecientos": 900, "novecientas": 900,
}

def _numeros_escritos(texto: str) -> set[str]:
    """Reconoce números españoles 0-999, sin inferir cantidades no expresadas."""
    import unicodedata
    normal = unicodedata.normalize("NFD", texto or "")
    normal = "".join(c for c in normal if unicodedata.category(c) != "Mn").lower()
    palabras = re.findall(r"(?<!\\w)[a-zñ]+(?!\\w)", normal)
    hallados = set()
    i = 0
    while i < len(palabras):
        palabra = palabras[i]
        valor = None
        avance = 1
        if palabra in _CENTENAS_ES:
            valor = _CENTENAS_ES[palabra]
            j = i + 1
            if j < len(palabras) and palabras[j] in _UNIDADES_ES:
                valor += _UNIDADES_ES[palabras[j]]
                avance += 1
            elif j < len(palabras) and palabras[j] in _DECENAS_ES:
                valor += _UNIDADES_ES[palabras[j]]
                avance += 1
                if j + 2 < len(palabras) and palabras[j + 1] == "y" and palabras[j + 2] in _UNIDADES_ES and _UNIDADES_ES[palabras[j + 2]] < 10:
                    valor += _UNIDADES_ES[palabras[j + 2]]
                    avance += 2
        elif palabra in _UNIDADES_ES:
            valor = _UNIDADES_ES[palabra]
            if palabra in _DECENAS_ES and i + 2 < len(palabras) and palabras[i + 1] == "y" and palabras[i + 2] in _UNIDADES_ES and _UNIDADES_ES[palabras[i + 2]] < 10:
                valor += _UNIDADES_ES[palabras[i + 2]]
                avance = 3
        if valor is not None:
            hallados.add(str(valor))
            i += avance
        else:
            i += 1
    return hallados
'''


def corregir(raiz: Path) -> Path:
    candidatos = []
    for ruta in raiz.rglob('*.py'):
        if any(p in {'.venv', 'venv', 'site-packages', '.git', '__pycache__'} for p in ruta.parts):
            continue
        try:
            contenido = ruta.read_text(encoding='utf-8-sig')
        except (UnicodeError, OSError):
            continue
        if 'def _cifras(' in contenido and 'def _sin_cifras_inventadas(' in contenido:
            candidatos.append((ruta, contenido))

    if len(candidatos) != 1:
        raise SystemExit(f'Necesito encontrar un único archivo Discovery con ambos filtros; encontrados: {len(candidatos)}. No se ha modificado nada.')
    ruta, contenido = candidatos[0]
    if 'def _numeros_escritos(' in contenido:
        print(f'Ya está corregido: {ruta}')
        return ruta

    patron = re.compile(r'(?m)^def _cifras\(texto: str\) -> set\[str\]:\n(?P<body>(?:^[ \t]+.*\n|^\s*\n)+)')
    encontrado = patron.search(contenido)
    if not encontrado or 'return {c.replace(' not in encontrado.group('body'):
        raise SystemExit('La función _cifras no coincide con la versión revisada. No se ha modificado nada.')
    funcion = encontrado.group(0)
    viejo = '    return {c.replace(".", "").replace(",", "") for c in _CIFRA.findall(limpio)}'
    if viejo not in funcion:
        raise SystemExit('No se encuentra el retorno esperado. No se ha modificado nada.')
    nuevo = funcion.replace(viejo, '    cifras = {c.replace(".", "").replace(",", "") for c in _CIFRA.findall(limpio)}\n    return cifras | _numeros_escritos(limpio)')
    actualizado = contenido[:encontrado.start()] + HELPER + '\n' + nuevo + contenido[encontrado.end():]
    ast.parse(actualizado, filename=str(ruta))

    respaldo = ruta.with_suffix(ruta.suffix + '.bak')
    if respaldo.exists():
        raise SystemExit(f'Ya existe el respaldo {respaldo}. No se sobrescribe ni modifica nada.')
    shutil.copy2(ruta, respaldo)
    ruta.write_text(actualizado, encoding='utf-8')
    print(f'Corregido: {ruta}\nRespaldo: {respaldo}')
    return ruta


if __name__ == '__main__':
    corregir(Path(__file__).resolve().parent)
