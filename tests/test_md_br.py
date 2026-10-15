"""Comprueba el arreglo del fallo 30: los <br> que escriben los agentes salen como saltos de línea.

En la prueba del 15 de octubre, las tablas de roadmap, gobernanza, perfiles y almacenamiento
mostraban «&lt;br&gt;» como texto dentro de las celdas.

No llama a ningún LLM ni toca ningún archivo.

Uso (desde la carpeta del proyecto, con el venv activado):
    python tests/test_md_br.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # la carpeta del proyecto (este archivo está en tests/)
sys.path.insert(0, str(RAIZ))  # para importar utils, agents, graph… desde aquí
os.chdir(RAIZ)                 # para que las rutas relativas (data_structures/…) apunten al proyecto

from utils import md_to_html

# 1) Una celda con varias líneas, como las que escriben los agentes
tabla = (
    "| Dato | Medida |\n"
    "| --- | --- |\n"
    "| driver_id | Seudonimizar el ID<br>Retención de 2 años<br>Acceso solo para operaciones |\n"
)
html = md_to_html(tabla)
assert "&lt;br" not in html, f"sigue saliendo <br> como texto:\n{html}"
assert "<td>Seudonimizar el ID<br />\nRetención de 2 años<br />\nAcceso solo para operaciones</td>" in html, html
assert "<td>driver_id</td>" in html
print("OK 1: un <br> dentro de una celda parte la línea y no se ve como texto")

# 2) Las variantes que usan los modelos
for variante in ("<br/>", "<br />", "<BR>", "<Br >"):
    html = md_to_html(f"| A |\n| --- |\n| uno{variante}dos |")
    assert "<td>uno<br />\ndos</td>" in html, f"{variante!r} no se ha convertido:\n{html}"
print("OK 2: <br/>, <br /> y las mayúsculas también se convierten")

# 3) Fuera de las tablas y dentro de negrita
assert md_to_html("uno<br>dos") == "<p>uno<br />\ndos</p>\n"
assert md_to_html("**uno<br>dos**") == "<p><strong>uno<br />\ndos</strong></p>\n"
print("OK 3: también funciona en párrafos y dentro de negrita")

# 4) Un <br> escrito como código se queda como código
html = md_to_html("Usa `<br>` para partir la línea")
assert "<code>&lt;br&gt;</code>" in html, html
print("OK 4: `<br>` entre comillas de código se sigue viendo como código")

# 5) El resto del HTML sigue escapado: solo se deja pasar el salto de línea
html = md_to_html("| A |\n| --- |\n| x<br><script>alert(1)</script><b>y</b> |")
assert "<script>" not in html and "&lt;script&gt;" in html, html
assert "<b>" not in html and "&lt;b&gt;" in html, html
assert "<br />" in html
print("OK 5: <script> y <b> siguen saliendo como texto; solo <br> se convierte")

print("\nTodo bien: los <br> de las tablas ya se ven como saltos de línea.")