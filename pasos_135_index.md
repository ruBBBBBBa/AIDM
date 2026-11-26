# Fallo 135: el cuadrante de posicionamiento en la pantalla de espera (index.html)

El backend ya está hecho:
- Al empezar el informe, `graph.plataforma_node` lanza en segundo plano una llamada a Groq, con Cloudflare de reserva.
- Si sale bien, deja `data_structures/posicionamiento.json`.
- `GET /api/posicionamiento` devuelve **204** mientras no hay cuadrante, y también si no lo va a haber. Cuando está, devuelve **200 con el JSON**.
- `POST /api/reset` («Nueva sesión») lo borra.

El dibujo está en `assets/js/cuadrante.js`: `pintarCuadrante(contenedor, datos)` devuelve `true` si lo pinta. Sin datos válidos deja el contenedor vacío.

En `index.html` faltan tres cosas, todas en la pantalla de espera.

Para verlo sin servidor, abre con doble clic `assets/js/cuadrante_prueba.html`.

---

## 1. El hueco, encima de «AIDM Tech News»

**Busca**
```html
            <!-- AIDM Tech News: noticias tecnológicas (fallo 127: carrusel, primero las del sector) -->
```

**Cámbialo por**
```html
            <!-- Fallo 135: cuadrante de posicionamiento (lo pinta assets/js/cuadrante.js cuando llega) -->
            <div id="cuadrante"></div>
            <!-- AIDM Tech News: noticias tecnológicas (fallo 127: carrusel, primero las del sector) -->
```

## 2. Cargar el script, antes del `<script>` principal

**Busca** el `<script>` del principio del código de la página (hoy, hacia la línea 603):
```html
<script>
```

**Cámbialo por**
```html
<script src="/assets/js/cuadrante.js"></script>
<script>
```

## 3. Pedirlo mientras se genera el informe, y vaciarlo con «Nueva sesión»

### a) Función `refreshEstado`

**Busca**
```js
async function refreshEstado(silencioso) {
```

**Cámbialo por**
```js
// Fallo 135: el cuadrante se pide en cada sondeo mientras se genera el informe, hasta tenerlo (204 = aún no)
let cuadrantePintado = false, pidiendoCuadrante = false;
async function pedirCuadrante() {
  if (cuadrantePintado || pidiendoCuadrante || typeof pintarCuadrante !== "function") return;
  pidiendoCuadrante = true;
  try {
    const r = await fetch("/api/posicionamiento");
    if (r.status === 200) cuadrantePintado = pintarCuadrante($("cuadrante"), await r.json());
  } catch (err) { /* sin cuadrante: la espera sigue igual */ }
  finally { pidiendoCuadrante = false; }
}

async function refreshEstado(silencioso) {
```

### b) Dentro de `refreshEstado`

**Busca**
```js
      if (e.generando_informe) {
        actualizarIntelligence(f, e.texto);
      }
```

**Cámbialo por**
```js
      if (e.generando_informe) {
        actualizarIntelligence(f, e.texto);
        pedirCuadrante();  // fallo 135
      }
```

### c) Función `resetPantalla`

**Busca**
```js
function resetPantalla() {
  cerrarIntelligence();
```

**Cámbialo por**
```js
function resetPantalla() {
  cerrarIntelligence();
  cuadrantePintado = false; $("cuadrante").replaceChildren();  // fallo 135
```

---

**Comprobar:**
- Con una ejecución, el cuadrante sale encima de las noticias unos segundos después de «Plataforma elegida». Si la llamada falla, no sale nada y el log tiene una línea `[posicionamiento]`.
- «Nueva sesión» lo quita.
- En la pestaña Red del navegador, `/api/posicionamiento` da 204 hasta que llega y después deja de pedirse.
