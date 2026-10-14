"""Comprueba el arreglo del fallo 11: md_to_html() convierte tablas y respeta los guiones bajos.

No llama a ningún LLM ni toca ningún archivo.

Uso (desde la carpeta del proyecto, con el venv activado):
    python test_md_to_html.py
"""
from pathlib import Path

from utils import md_to_html

# 1) Tablas, con la alineación de columnas
html = md_to_html("| Servicio | Coste |\n| --- | ---: |\n| ADLS Gen2 | 120 € |")
assert "<table>" in html and "<th>Servicio</th>" in html and "<td>ADLS Gen2</td>" in html
assert '<td style="text-align:right">120 €</td>' in html
assert "|" not in html, "no deben quedar barras sueltas"
print("OK 1: las tablas se convierten en <table>, con la alineación de cada columna")

# 2) Los nombres con guion bajo se quedan como están
html = md_to_html("La tabla fact_ventas_diarias usa dim_cliente_id.")
assert "fact_ventas_diarias" in html and "dim_cliente_id" in html and "<em>" not in html
assert "_texto_" not in md_to_html("Esto es _texto_ en cursiva") and "<em>texto</em>" in md_to_html("Esto es _texto_ en cursiva")
print("OK 2: fact_ventas_diarias se queda igual, y _así_ sigue siendo cursiva")

# 3) El HTML que escriba el LLM se ve como texto, no se ejecuta
html = md_to_html("Servicio: Azure SQL <Basic> staging <script>alert(1)</script>")
assert "&lt;Basic&gt;" in html and "&lt;script&gt;" in html and "<script>" not in html
print("OK 3: <Basic> se ve como texto y un <script> no se ejecuta")

# 4) Lo que ya funcionaba sigue igual
assert md_to_html("# Título") == "<h3>Título</h3>\n", "# debe seguir siendo <h3>"
assert md_to_html("## Sub").startswith("<h3>") and md_to_html("### Detalle").startswith("<h4>")
assert md_to_html("**negrita** y `codigo`") == "<p><strong>negrita</strong> y <code>codigo</code></p>\n"
assert "<ul>" in md_to_html("- uno\n- dos") and "<ol>" in md_to_html("1. uno\n2. dos")
assert "<br" in md_to_html("línea uno\nlínea dos"), "cada salto de línea debe seguir siendo un <br>"
assert md_to_html("") == md_to_html("   ") == "<p>No se ha generado contenido.</p>"
print("OK 4: títulos, negrita, código, listas, saltos de línea y texto vacío como antes")

# 5) La tabla del coste del equipo (fallo 5) sale como tabla
from agents.estimation_crew import EquipoEstimado, PerfilEquipo, _calcular_equipo, _cargar_tarifas
equipo = EquipoEstimado(perfiles=[PerfilEquipo(perfil="Data Engineer", nivel="Senior", horas_mes=160)],
                        meses_min=3, meses_max=4)
html = md_to_html(_calcular_equipo(equipo, _cargar_tarifas())["texto"])
assert "<table>" in html and "<td>Data Engineer</td>" in html and "<strong>Total mensual</strong>" in html
print("OK 5: la tabla del coste del equipo se ve como tabla")

# 6) Las dos plantillas del informe tienen estilos para las tablas
for plantilla in ("solution_builder_report.html", "solution_builder_report_recovery.html"):
    css = Path("data_structures", plantilla).read_text(encoding="utf-8")
    assert ".prose table" in css and ".prose th, .prose td" in css, f"falta el CSS de tablas en {plantilla}"
print("OK 6: las dos plantillas tienen estilos para tablas")

print("\nTodo bien: el fallo 11 está arreglado.")