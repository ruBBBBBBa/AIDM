# Modelo de decisión de AIDM

Un modelo pequeño (Qwen3.5-0.8B ajustado con Unsloth) que contesta preguntas cerradas sin escribir texto: hace una sola pasada y devuelve una probabilidad por opción. AIDM lo llama desde `decisiones.py`; si no está en marcha, AIDM funciona igual que sin él.

Desde la v2 (fallos 68, 69 y 70), el mismo modelo contesta tres preguntas:

| Tarea | Dónde la usa AIDM | Opciones |
|---|---|---|
| `estado` (fallos 36 y 68) | discovery, en cada turno, a la vez que el LLM | `dato`, `no_lo_sabe`, `no_aplica`, `no_responde`, `vaga` |
| `uso` (fallo 69) | agente de plataforma, en los campos con una negación o un cambio cerca de una herramienta o nube | `la_usa`, `quiere_usarla`, `no_la_usa`, `la_deja` |
| `despliegue` (fallo 70) | agente de plataforma, una vez por informe | `cloud`, `hibrido`, `on_premises`, `sin_datos` |

La regla con la que se etiquetan los ejemplos de despliegue es de Inetum y está en `despliegue_correcto()` de `generar_datos.py`: manda lo que obliga (normas, sistemas que no se mueven, planta, servidores nuevos), no lo que el cliente dice preferir. Para cambiar el criterio, se cambia esa función, se generan los datos y se reentrena.


| Archivo | Qué es |
|---|---|
| `generar_datos.py` | Genera los ejemplos inventados (`datos/`). Usa la misma pregunta y el mismo texto que `decisiones.py`. |
| `datos/entrenamiento.jsonl`, `datos/prueba.jsonl` | v2: 4.340 filas para entrenar y 478 de prueba (estado 2.540 y 278, uso 1.000 y 100, despliegue 800 y 100), con frases que no están en el entrenamiento. Cada fila lleva `tarea`. |
| `entrenar_decision.ipynb` | Cuaderno de Colab: entrena, evalúa, exporta a GGUF, lo prueba en CPU y lo sube a un repo privado de Hugging Face. |
| `space/` | Para servirlo en un Space de Hugging Face. Desde 2026 los Spaces Docker necesitan una cuenta PRO. |

## Medidas de la v1 (6 nov 2026, solo `estado`)

Las de la v2 salen en los pasos 6 y 8 del cuaderno, tarea por tarea.


| | Aciertos en la prueba | Tiempo por petición |
|---|---|---|
| Antes de entrenar | 26,8 % (al azar) | — |
| Entrenado (PyTorch, Colab) | 89,3 % | — |
| GGUF Q8_0 con llama-server, Colab (2 hilos) | 90,6 % | 10 s de mediana |
| GGUF Q8_0 con llama-server, portátil i5-10210U (4 hilos) | — | unos 4 s |

Con una confianza de 0,7 o más (`decisiones.CONFIANZA_MINIMA`), las 75 respuestas de prueba que marcó como `no_lo_sabe` o `no_aplica` eran correctas. Se le escaparon 9 de 84; esas se quedan pendientes y el discovery las vuelve a preguntar. Lo que más falla son expresiones que no ha visto, como «Beats me».

## Servirlo en local (Windows)

No hace falta GPU. Solo dos descargas, que van fuera del repo:

1. **llama.cpp:** en https://github.com/ggml-org/llama.cpp/releases, el archivo `llama-bNÚMERO-bin-win-cpu-x64.zip` de la última versión. Descomprímelo en una carpeta, por ejemplo `C:\Users\<usuario>\llama.cpp`.
2. **El modelo:** `model-Q8_0.gguf` (803 MB) del repo privado de Hugging Face (*Files and versions*). Ponlo en la misma carpeta.

Para arrancarlo, con el cargador puesto y el modo de energía en *Mejor rendimiento*, abre una consola en esa carpeta y ejecuta:

```
llama-server.exe -m model-Q8_0.gguf --port 8090 -c 2048 -b 2048 -ub 2048 -t 4
```

- Está listo cuando dice que escucha en `http://127.0.0.1:8090`. Solo escucha en el propio ordenador. Deja la ventana abierta mientras uses AIDM; Ctrl+C lo para.
- `-t 4`: un hilo por núcleo físico.
- `-c`, `-b` y `-ub` van juntos: con el `-ub` por defecto (512), `/v1/systemone` rechaza las entradas más largas.

En el `.env` de AIDM:

```
AIDM_DECISION_URL=http://127.0.0.1:8090
```

Para probarlo, desde la carpeta del proyecto y con el entorno activado:

```
python -c "import decisiones; print(decisiones.estado_respuesta('tipo_de_arquitectura', 'Estilo de arquitectura', '¿Data warehouse, data lake o lakehouse?', 'No lo sé'))"
```

Tiene que salir `[decisiones] estado_respuesta · tipo_de_arquitectura · no_lo_sabe (0.99) · N s`. Si sale solo `None`, AIDM no ha leído `AIDM_DECISION_URL`; si sale «El modelo de decisión no responde», llama-server no está en marcha.

## Pasar a la v2 (las tres tareas)

El GGUF de la v2 se llama igual (`model-Q8_0.gguf`) y va en el mismo repo de Hugging Face; la v1 queda en su historial. Usa la v2 con el código de los fallos 68, 69 y 70: la v1 no ha visto las preguntas nuevas y contestaría al azar.

1. Para `llama-server` (Ctrl+C en su ventana).
2. En la carpeta de llama.cpp, cambia el nombre del modelo de antes a `model-Q8_0-v1.gguf`, por si hay que volver a él.
3. Descarga `model-Q8_0.gguf` del repo de Hugging Face (*Files and versions*) en esa carpeta.
4. Arráncalo con el mismo comando de arriba. El puerto y el `.env` no cambian.
5. Pruébalo desde la carpeta del proyecto, con el entorno activado:

```
python -c "import decisiones; print(decisiones.estado_respuesta('volumen_de_datos', 'Volumen de datos', '¿Qué volumen de datos manejáis?', 'Bastantes datos')); print(decisiones.usos('stack_tecnologico_actual', 'Python y Excel; no usan Power BI ni Microsoft 365', ['Excel', 'Power BI', 'Microsoft 365'])); print(decisiones.despliegue_recomendado({'regulacion_y_compliance': 'Por contrato con Defensa, ningún dato puede salir de nuestras instalaciones'}))"
```

   Tiene que salir `vaga`; Excel `la_usa` y Power BI y Microsoft 365 `no_la_usa`; y `on_premises`, cada uno con su confianza.

Para volver a la v1: para `llama-server` y arráncalo con `-m model-Q8_0-v1.gguf`.


## Servirlo en un servidor

En el deploy, `llama-server` puede ir en el mismo servidor que AIDM, en CPU, con la imagen `ghcr.io/ggml-org/llama.cpp:server` y los mismos parámetros. Así los datos de los clientes no salen del servidor. La alternativa, un Space de Hugging Face (`space/`), necesita una cuenta PRO, y las respuestas de los clientes pasarían por Hugging Face.
