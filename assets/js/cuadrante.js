/* Fallo 135: el cuadrante de posicionamiento de la pantalla de espera.
 *
 *   pintarCuadrante(contenedor, datos) -> true si lo ha pintado, false si no (y deja el contenedor vacío).
 *
 * `datos` es lo que devuelve GET /api/posicionamiento: area, cliente.hoy y cliente.con_proyecto ({x, y, motivo}),
 * competidores ([{nombre, x, y, motivo, confianza}]) y aviso. X e Y van de 0 a 10. Todo el texto entra con
 * textContent (lo escribe un modelo: nunca como HTML). Colores para el panel oscuro de la espera.
 */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var COLOR = {
    tinta: "#e7edf6", apagado: "#93a1b5", linea: "#385174", fondo: "rgba(255,255,255,.035)",
    resalte: "rgba(115,145,255,.10)", cliente: "#7391ff", competidor: "#93a1b5"
  };
  var AVISO = "Posición orientativa estimada por IA a partir de información pública general. No es un estudio de mercado.";
  var ANCHO = 360, ALTO = 300, IZQ = 30, DER = 10, ARR = 10, ABA = 30;
  var W = ANCHO - IZQ - DER, H = ALTO - ARR - ABA;
  var instancias = 0;

  function num(v) {
    var n = Number(v);
    return isFinite(n) ? Math.max(0, Math.min(10, n)) : null;
  }
  function punto(p) {
    return p && num(p.x) !== null && num(p.y) !== null ? { x: num(p.x), y: num(p.y), motivo: String(p.motivo || "") } : null;
  }
  function px(x) { return IZQ + (x / 10) * W; }
  function py(y) { return ARR + (1 - y / 10) * H; }

  function el(nombre, atributos, texto) {
    var e = document.createElementNS(NS, nombre);
    Object.keys(atributos || {}).forEach(function (k) { e.setAttribute(k, atributos[k]); });
    if (texto != null) e.textContent = texto;
    return e;
  }
  function html(nombre, estilo, texto) {
    var e = document.createElement(nombre);
    if (estilo) e.setAttribute("style", estilo);
    if (texto != null) e.textContent = texto;
    return e;
  }
  function corto(texto, max) {
    texto = String(texto || "");
    return texto.length > max ? texto.slice(0, max - 1) + "…" : texto;
  }

  function leer(datos) {
    if (!datos || typeof datos !== "object") return null;
    var cliente = datos.cliente || {};
    var hoy = punto(cliente.hoy), con = punto(cliente.con_proyecto);
    var competidores = (Array.isArray(datos.competidores) ? datos.competidores : [])
      .map(function (c) {
        var p = punto(c);
        return p && c.nombre ? { nombre: String(c.nombre), x: p.x, y: p.y, motivo: p.motivo,
                                 confianza: ["alta", "media", "baja"].indexOf(c.confianza) >= 0 ? c.confianza : "baja" } : null;
      })
      .filter(Boolean).slice(0, 4);
    if (!hoy || !con || !competidores.length) return null;
    return { area: String(datos.area || ""), hoy: hoy, con: con, competidores: competidores };
  }

  function dibujo(d) {
    var id = "cuadrante-flecha-" + (++instancias);
    var svg = el("svg", { viewBox: "0 0 " + ANCHO + " " + ALTO, width: "100%", role: "img",
                          "aria-label": "Cuadrante de posicionamiento" + (d.area ? ": " + d.area : ""),
                          style: "display:block;font-family:inherit;overflow:visible" });
    var defs = el("defs");
    var marca = el("marker", { id: id, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7",
                               orient: "auto-start-reverse" });
    marca.appendChild(el("path", { d: "M0,0 L10,5 L0,10 z", fill: COLOR.cliente }));
    defs.appendChild(marca);
    svg.appendChild(defs);

    // Los 4 cuadrantes, partidos en el 5; arriba a la derecha, resaltado
    var mx = px(5), my = py(5);
    svg.appendChild(el("rect", { x: IZQ, y: ARR, width: W, height: H, fill: COLOR.fondo, stroke: COLOR.linea, rx: "6" }));
    svg.appendChild(el("rect", { x: mx, y: ARR, width: IZQ + W - mx, height: my - ARR, fill: COLOR.resalte }));
    svg.appendChild(el("line", { x1: mx, y1: ARR, x2: mx, y2: ARR + H, stroke: COLOR.linea, "stroke-dasharray": "4 4" }));
    svg.appendChild(el("line", { x1: IZQ, y1: my, x2: IZQ + W, y2: my, stroke: COLOR.linea, "stroke-dasharray": "4 4" }));
    var etiqueta = { "font-size": "10", fill: COLOR.apagado, "font-weight": "600", "letter-spacing": ".04em" };
    [["En transformación", IZQ + 8, ARR + 16, "start"], ["Referentes", IZQ + W - 8, ARR + 16, "end"],
     ["En arranque", IZQ + 8, ARR + H - 8, "start"], ["Consolidados", IZQ + W - 8, ARR + H - 8, "end"]]
      .forEach(function (c) {
        svg.appendChild(el("text", Object.assign({ x: c[1], y: c[2], "text-anchor": c[3] }, etiqueta), c[0].toUpperCase()));
      });

    // Ejes cortos; el área va de título, encima
    svg.appendChild(el("text", { x: IZQ + W / 2, y: ALTO - 8, "text-anchor": "middle", "font-size": "11", fill: COLOR.tinta },
                       "Madurez actual →"));
    svg.appendChild(el("text", { x: 12, y: ARR + H / 2, "text-anchor": "middle", "font-size": "11", fill: COLOR.tinta,
                                 transform: "rotate(-90 12 " + (ARR + H / 2) + ")" }, "Ambición e inversión →"));

    // Competidores, en gris y con su nombre
    d.competidores.forEach(function (c) {
      var x = px(c.x), y = py(c.y), izquierda = c.x > 7;
      var g = el("g");
      g.appendChild(el("title", {}, c.nombre + " · confianza " + c.confianza));
      g.appendChild(el("circle", { cx: x, cy: y, r: "4.5", fill: COLOR.competidor, "fill-opacity": c.confianza === "baja" ? ".55" : ".9" }));
      g.appendChild(el("text", { x: izquierda ? x - 8 : x + 8, y: y + 3.5, "text-anchor": izquierda ? "end" : "start",
                                 "font-size": "10.5", fill: COLOR.apagado }, corto(c.nombre, 24)));
      svg.appendChild(g);
    });

    // El cliente: hoy (relleno) → con el proyecto (borde discontinuo), con flecha
    var x0 = px(d.hoy.x), y0 = py(d.hoy.y), x1 = px(d.con.x), y1 = py(d.con.y);
    var largo = Math.hypot(x1 - x0, y1 - y0);
    if (largo > 16) {
      var ux = (x1 - x0) / largo, uy = (y1 - y0) / largo;
      svg.appendChild(el("line", { x1: x0 + ux * 8, y1: y0 + uy * 8, x2: x1 - ux * 9, y2: y1 - uy * 9, stroke: COLOR.cliente,
                                   "stroke-width": "2", "marker-end": "url(#" + id + ")" }));
    }
    svg.appendChild(el("circle", { cx: x1, cy: y1, r: "7", fill: "none", stroke: COLOR.cliente, "stroke-width": "2",
                                   "stroke-dasharray": "3 2.5" }));
    svg.appendChild(el("circle", { cx: x0, cy: y0, r: "7", fill: COLOR.cliente }));
    var abajo = y1 < y0;  // las etiquetas, hacia fuera de la flecha
    svg.appendChild(el("text", { x: x0, y: abajo ? y0 + 20 : y0 - 12, "text-anchor": "middle", "font-size": "10.5",
                                 "font-weight": "700", fill: COLOR.cliente }, "Hoy"));
    svg.appendChild(el("text", { x: x1, y: abajo ? y1 - 12 : y1 + 20, "text-anchor": "middle", "font-size": "10.5",
                                 "font-weight": "700", fill: COLOR.cliente }, "Con el proyecto"));
    return svg;
  }

  function motivos(d) {
    var lista = html("ul", "list-style:none;margin:10px 0 0;padding:0;font-size:.78rem;line-height:1.45;color:" + COLOR.apagado);
    function fila(quien, color, texto) {
      var li = html("li", "margin:0 0 5px");
      li.appendChild(html("strong", "color:" + color + ";font-weight:700", quien));
      if (texto) li.appendChild(document.createTextNode(": " + texto));
      lista.appendChild(li);
    }
    fila("Hoy", COLOR.cliente, d.hoy.motivo);
    fila("Con el proyecto", COLOR.cliente, d.con.motivo);
    d.competidores.forEach(function (c) {
      fila(c.nombre + " (confianza " + c.confianza + ")", COLOR.tinta, c.motivo);
    });
    return lista;
  }

  window.pintarCuadrante = function (contenedor, datos) {
    if (!contenedor) return false;
    var d = leer(datos);
    contenedor.replaceChildren();
    if (!d) return false;  // sin datos, no se pinta nada
    var caja = html("section", "margin-top:22px;padding:17px;border:1px solid " + COLOR.linea +
                    ";border-radius:13px;background:rgba(255,255,255,.045);text-align:left;color:" + COLOR.tinta);
    caja.setAttribute("aria-label", "Posicionamiento frente a competidores");
    caja.appendChild(html("div", "font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:" + COLOR.apagado,
                          "Posicionamiento"));
    if (d.area) caja.appendChild(html("h3", "margin:2px 0 10px;font-size:1rem;line-height:1.3;color:" + COLOR.tinta, d.area));
    caja.appendChild(dibujo(d));
    caja.appendChild(motivos(d));
    caja.appendChild(html("p", "margin:8px 0 0;font-size:.7rem;font-style:italic;color:" + COLOR.apagado, AVISO));
    contenedor.appendChild(caja);
    return true;
  };
})();
