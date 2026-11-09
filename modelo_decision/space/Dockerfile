# Space de Hugging Face (Docker, CPU basic) que sirve el modelo de decisión de AIDM con llama.cpp.
# AIDM lo llama en https://<usuario>-aidm-decision.hf.space/v1/systemone (ver decisiones.py).
FROM ghcr.io/ggml-org/llama.cpp:server

# El modelo se baja al arrancar. El repo es privado: el Space necesita el secreto HF_TOKEN.
ENV LLAMA_CACHE=/tmp/llama

# -c, -b y -ub van juntos y por encima de lo que ocupa una petición (unos 500 tokens): con el -ub por defecto
# (512), /v1/systemone rechaza las entradas más largas. -t 2 porque el hardware gratuito tiene 2 vCPU.
CMD ["--hf-repo", "tu_usuario/aidm-decision", "--hf-file", "model-Q8_0.gguf", \
     "--host", "0.0.0.0", "--port", "7860", \
     "-c", "2048", "-b", "2048", "-ub", "2048", "-t", "2", "--parallel", "1"]
