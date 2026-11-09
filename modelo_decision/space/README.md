---
title: AIDM decision
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Modelo de decisión de AIDM

> Desde 2026, los Spaces Docker necesitan una cuenta PRO de Hugging Face (9 $/mes); con la cuenta gratuita solo se pueden crear Spaces estáticos. Lo normal es servir el modelo en local, en el mismo ordenador que AIDM: ver `modelo_decision/README.md`.

Sirve con llama.cpp, en CPU, el modelo de decisión que entrena `modelo_decision/entrenar_decision.ipynb`. Hoy contesta una sola pregunta: qué hace la respuesta del cliente con el campo por el que pregunta el discovery (`dato`, `no_lo_sabe`, `no_aplica` o `no_responde`).

## Montarlo

1. *New Space* en Hugging Face: SDK **Docker**, hardware **CPU basic** (gratis), visibilidad **privada**.
2. Sube este `README.md` y el `Dockerfile`. En el `Dockerfile`, cambia `tu_usuario` por tu usuario.
3. *Settings → Variables and secrets → New secret*: `HF_TOKEN`, con un token de lectura. Sin él, el Space no puede bajar el modelo, que está en un repo privado.

## Probarlo

```bash
curl https://tu_usuario-aidm-decision.hf.space/v1/systemone \
  -H "Authorization: Bearer $HF_TOKEN" -H "Content-Type: application/json" \
  -d '{"state": "Dato que se pregunta: tipo_de_arquitectura (Estilo de arquitectura)\nPregunta del asistente: ¿Data warehouse, data lake o lakehouse?\nRespuesta del cliente: No lo sé",
       "questions": {"estado": {"type": "choice", "instructions": "¿Qué hace la respuesta?",
                                "criteria": {"dato": "Da el dato", "no_lo_sabe": "No lo sabe",
                                             "no_aplica": "No aplica", "no_responde": "No contesta"}}}}'
```

La prueba de verdad la hace AIDM, que manda las instrucciones y opciones completas de `decisiones.PREGUNTA_ESTADO`.

## A tener en cuenta

- Con CPU basic, el Space se pausa tras 48 h sin uso. La primera petición lo despierta y tarda; mientras, AIDM sigue sin el modelo (`decisiones.py` espera 30 s y no vuelve a intentarlo en 5 min).
- Las peticiones llevan respuestas de clientes: el Space tiene que ser privado y su uso con clientes reales está pendiente de la decisión de privacidad.
