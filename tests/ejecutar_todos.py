"""Ejecuta los tests de los arreglos, uno detrás de otro, y resume el resultado.

No incluye test_modelos.py ni test_fallback*.py, que llaman a los modelos de verdad.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/ejecutar_todos.py
"""
import os
import subprocess
import sys
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
TESTS = [
    "test_discovery_update.py",     # fallo 1
    "test_discovery_relleno.py",    # fallo 2
    "test_interfaz_informe.py",     # fallo 3
    "test_roadmap_contexto.py",     # fallo 4
    "test_costes_equipo.py",        # fallo 5
    "test_md_to_html.py",           # fallo 11
    "test_discovery_inventos.py",   # fallo 21
    "test_discovery_formato.py",    # fallo 22
    "test_md_br.py",                # fallo 30
    "test_costes_resumen.py",       # fallo 23
    "test_discovery_respuestas.py", # fallo 33
    "test_discovery_listas.py",     # fallo 35
    "test_contexto_cliente.py",     # fallo 25
    "test_presupuesto_unico.py",    # fallo 24
    "test_prompts_sesgo.py",        # fallos 34 y 10
    "test_roadmap_plataforma.py",   # fallo 6
    "test_costes_componentes.py",   # fallo 32
    "test_costes_perfiles.py",      # fallo 37
    "test_costes_unitarios.py",     # fallo 41 (C15)
    "test_costes_sin_tarifa.py",    # fallo 42 (C16 y C17)
    "test_costes_perfiles_cortados.py",  # fallo 40 (C14); se cayó de la lista con el 42 
    "test_costes_fases.py",         # fallo 43
    "test_costes_tildes.py",        # C9; no estaba en la lista 
    "test_secciones_sin_importes.py",  # fallo 44 (C2)
    "test_costes_ceros.py",         # fallo 45 (C6)
    "test_costes_json.py",          # fallo 46
    "test_costes_json_envuelto.py", # fallo 47
    "test_interfaz_sin_relanzar.py",  # fallo 48
    "test_estructura_sin_importes.py",  # refuerzo del 44
    "test_costes_json_reparado.py", # fallo 50
    "test_costes_justificacion.py", # fallo 51
    "test_costes_fases_minimo.py",  # fallo 49
]

# Los resultados llevan «≥», «€»…: en Windows, sin esto, la salida por tubería usa cp1252 y falla.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
entorno = {**os.environ, "PYTHONIOENCODING": "utf-8"}

fallos = []
for nombre in TESTS:
    test = CARPETA / nombre
    if not test.exists():
        print(f"NO ESTÁ  {nombre}")
        fallos.append(nombre)
        continue
    r = subprocess.run([sys.executable, str(test)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=entorno)
    ultima = (r.stdout.strip().splitlines() or ["(sin salida)"])[-1]
    if r.returncode == 0:
        print(f"OK       {nombre}: {ultima}")
    else:
        fallos.append(nombre)
        print(f"FALLA    {nombre}\n" + "\n".join((r.stderr or r.stdout).strip().splitlines()[-6:]))

print(f"\n{len(TESTS) - len(fallos)} de {len(TESTS)} tests pasan." + (f" Fallan: {', '.join(fallos)}" if fallos else ""))
sys.exit(1 if fallos else 0)