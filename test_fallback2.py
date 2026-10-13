from dotenv import load_dotenv
import litellm

load_dotenv(".env")
texto = "Describe la arquitectura de datos de una distribuidora farmacéutica. " * 450
r = litellm.completion(
    model="groq/openai/gpt-oss-20b",
    messages=[{"role": "user", "content": texto + "\nEscribe un informe detallado de unas 1.500 palabras sobre esta arquitectura."}],
    max_tokens=4096,
)
print("Fin:", r.choices[0].finish_reason)
print("Entrada:", r.usage.prompt_tokens, "· Salida:", r.usage.completion_tokens)