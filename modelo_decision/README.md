# Modelo de decisión de AIDM

Un modelo pequeño (Qwen3.5-0.8B ajustado con Unsloth) que contesta preguntas cerradas sin escribir texto: hace una sola pasada y devuelve una probabilidad por opción. Hoy contesta una sola pregunta, la del fallo 36: qué hace la respuesta del cliente con el campo por el que pregunta el discovery (`dato`, `no_lo_sabe`, `no_aplica` o `no_responde`). AIDM lo llama desde `decisiones.py`; si no está en marcha, AIDM funciona igual que antes.

| Archivo | Qué es |
|---|---|
| `generar_datos.py` | Genera los ejemplos inventados (`datos/`). Usa la misma pregunta y el mismo texto que `decisiones.py`. |
| `datos/entrenamiento.jsonl`, `datos/prueba.jsonl` | 2.108 ejemplos para entrenar y 224 de prueba, con frases que no están en el entrenamiento. |
| `entrenar_decision.ipynb` | Cuaderno de Colab: entrena, evalúa, exporta a GGUF, lo prueba en CPU y lo sube a un repo privado de Hugging Face. |
| `space/` | Para servirlo en un Space de Hugging Face. Desde 2026 los Spaces Docker necesitan una cuenta PRO. |

## Medidas (6 nov 2026)

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

## Servirlo en un servidor

En el deploy, `llama-server` puede ir en el mismo servidor que AIDM, en CPU, con la imagen `ghcr.io/ggml-org/llama.cpp:server` y los mismos parámetros. Así los datos de los clientes no salen del servidor. La alternativa, un Space de Hugging Face (`space/`), necesita una cuenta PRO, y las respuestas de los clientes pasarían por Hugging Face.
