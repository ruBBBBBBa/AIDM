import os
from dotenv import load_dotenv
import litellm

load_dotenv(".env")
for var in ["BIG_MODEL", "REASONING_MODEL", "REASONING_RIGOROUS_MODEL", "SMALL_REASONING_MODEL"]:
    model = os.getenv(var)
    try:
        r = litellm.completion(model=model, max_tokens=300,
                               messages=[{"role": "user", "content": "Responde solo: OK"}])
        print(f"{var:<26} -> {r.choices[0].message.content.strip()!r}")
    except Exception as e:
        print(f"{var:<26} -> ERROR: {str(e)[:200]}")