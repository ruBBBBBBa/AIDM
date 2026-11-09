from dotenv import load_dotenv
import litellm

load_dotenv(".env")
texto = "Describe la arquitectura de datos de una distribuidora farmacéutica. " * 500
msgs = [{"role": "user", "content": texto + "\nResume en 3 frases."}]
print("Tokens de entrada (aprox.):", litellm.token_counter(model="groq/openai/gpt-oss-20b", messages=msgs))

r = litellm.completion(
    model="groq/openai/gpt-oss-20b",
    messages=msgs,
    max_tokens=4096,
    fallbacks=[{"model": "cloudflare/@cf/openai/gpt-oss-20b", "max_tokens": 6000}],
)
hp = r._hidden_params
print("Proveedor:", hp.get("custom_llm_provider"))
print("Saltos al de reserva:", hp.get("additional_headers", {}).get("x-litellm-attempted-fallbacks"))
print("Uso:", r.usage)