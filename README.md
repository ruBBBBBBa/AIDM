# AIDM — AI Discovery Manager

Asistente de Inetum para proyectos de Data & AI. Tiene dos fases:

1. **Discovery:** un chat (Streamlit) entrevista al cliente y va rellenando `data_structures/problem_case.json`, campo a campo.
2. **Informe:** cuando el caso está completo, unos agentes (LangGraph + CrewAI) eligen la nube y generan un informe HTML con la estructura, la propuesta (arquitectura, gobernanza, estrategia y BI), los costes y el equipo, y el roadmap. El informe se ve en la propia página y se descarga con un botón.

Cada tarea usa su propio modelo, con reservas de otros proveedores (el *gateway*, `gateway.py`). Todos los modelos que vienen configurados tienen plan gratuito.

---

## Requisitos

- **Python 3.12 o 3.13** y **Git**.
- **Claves de API** de los proveedores de modelos. Todas tienen plan gratuito (ver [Claves](#2-el-archivo-env)).
- Opcional: **llama.cpp** para el modelo de decisión ([más abajo](#modelo-de-decisión-opcional)).

Versiones con las que está probado: Streamlit 1.66, CrewAI 1.15, LiteLLM 1.106. Streamlit tiene que ser **1.56 o superior** (el informe usa `st.iframe`).

---

## Instalación

### 1. Clonar y crear el entorno

```bash
git clone https://github.com/ruBBBBBBa/AIDM.git
cd AIDM
git checkout pruebas-rubengs
```

**Windows (CMD):**
```cmd
python -m venv aidmENV
aidmENV\Scripts\activate.bat
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv aidmENV
.\aidmENV\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
python3 -m venv aidmENV
source aidmENV/bin/activate
pip install -r requirements.txt
```

Con el entorno activado, el prompt empieza por `(aidmENV)`. Si `lxml` no se instala, prueba `pip install lxml --only-binary :all:`.

### 2. El archivo `.env`

En la raíz del proyecto, crea un archivo `.env` **solo con las claves**. Los modelos ya no van aquí: están en `data_structures/modelos.yaml`.

```env
MISTRAL_API_KEY=...
GEMINI_API_KEY=...
GROQ_API_KEY=...
CLOUDFLARE_API_KEY=...
CLOUDFLARE_ACCOUNT_ID=...
SAMBANOVA_API_KEY=...
NVIDIA_NIM_API_KEY=...
```

| Clave | Dónde se saca | Para qué |
|---|---|---|
| `MISTRAL_API_KEY` | [console.mistral.ai](https://console.mistral.ai) (plan Experiment) | Discovery, redacción y Terraform |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) | Costes, roadmap y resumen |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) | Reserva del discovery |
| `CLOUDFLARE_API_KEY` y `CLOUDFLARE_ACCOUNT_ID` | Panel de Cloudflare → Workers AI | Reserva de costes, redacción y roadmap |
| `SAMBANOVA_API_KEY` | [cloud.sambanova.ai](https://cloud.sambanova.ai) | Última reserva de costes |
| `NVIDIA_NIM_API_KEY` | [build.nvidia.com](https://build.nvidia.com) | Solo para comparar modelos (`pruebas_modelos/comparar.py`) |

Con **Mistral y Gemini** ya funciona todo. Las demás son reservas: sin ellas, si un modelo da 429 o falla, no hay a quién pasar la llamada.

> **El `.env` nunca se sube al repo ni se comparte** (está en el `.gitignore`). Si pasas el proyecto en un zip, va sin él.

Variables opcionales del `.env`:

| Variable | Qué hace |
|---|---|
| `AIDM_DECISION_URL=http://127.0.0.1:8090` | Usa el modelo de decisión (ver [más abajo](#modelo-de-decisión-opcional)). Sin ella, AIDM funciona igual. |
| `MODELO_<TAREA>=proveedor/modelo` | Pone ese modelo delante para una tarea (por ejemplo, `MODELO_COSTES=gemini/gemini-3.6-flash`); las reservas del perfil siguen detrás. |
| `GRABAR_LLAMADAS=1` | Guarda el prompt y la respuesta de cada llamada en `pruebas_modelos/grabaciones/`, para comparar modelos después. **Llevan datos del cliente.** |
| `INFRACOST_API_KEY=ico-...` | Precios con Infracost (opcional; ver [más abajo](#infracost-opcional)). |

### 3. Comprobar que todo está bien

```bash
python tests/ejecutar_todos.py
python pruebas_modelos/comparar.py --comprobar
```

- `ejecutar_todos.py` pasa los tests de `tests/`. Ninguno llama a un modelo, así que no gastan cupo. Al final dice cuántos pasan: tienen que pasar todos. Los tests que ejecuta son los que no están comentados en la lista `TESTS` del propio archivo.
- `comparar.py --comprobar` pregunta a cada proveedor si existen los modelos de `modelos.yaml` y avisa de los que no. Si sale `CERTIFICATE_VERIFY_FAILED`, es la red de la empresa: prueba desde otra.

---

## Usar AIDM

### Antes de cada discovery: vaciar el caso

`data_structures/problem_case.json` guarda la entrevista. **Antes de empezar con un cliente nuevo**, cópiale encima la plantilla vacía `data_structures/problem_case_recovery.json`:

```cmd
copy /Y data_structures\problem_case_recovery.json data_structures\problem_case.json
```

(en Linux o macOS: `cp data_structures/problem_case_recovery.json data_structures/problem_case.json`)

Si no lo vacías, AIDM sigue con el cliente anterior, y si ese caso ya estaba completo, genera su informe otra vez.

### Arrancar

```bash
streamlit run interface.py
```

Se abre en `http://localhost:8501`. La primera vez tarda unos segundos en cargar los módulos; la consola dice cuántos (`[aidm] Carga de los módulos…`).

Para guardar la salida de la consola en un archivo:

- **CMD:** `streamlit run interface.py > ejecucion.log 2>&1`
- **PowerShell:** `streamlit run interface.py *> ejecucion.log`

> **Los logs llevan datos del cliente:** no se suben al repo (están en el `.gitignore`) ni a ningún otro sitio.

### Qué pasa en una sesión

```
El cliente describe su proyecto en el chat
        │
        ▼
Discovery: una pregunta por turno, hasta completar todos los campos del caso
  · «no lo sé» / «no aplica» se guardan como tales
  · con el modelo de decisión: una respuesta vaga («bastantes datos») se vuelve a preguntar una vez
        │
        ▼ (caso completo)
Agente de plataforma (agents/plataforma.py)
  · compara AWS, Azure, Google Cloud, Oracle e IBM Cloud y decide en qué nube se diseña;
    lo que el cliente ya tiene en otra nube se mantiene, salvo que pida migrarlo o que compense mucho
  · el chat enseña lo que ha decidido; el informe sigue solo
        │
        ▼
Estructura de la solución
        │
        ▼
Propuesta (4 agentes en paralelo: arquitectura, gobernanza, estrategia y BI)
y costes (componentes con la tabla de precios, perfiles y equipo)
        │
        ▼
Roadmap (7 fases: fuentes, ingesta, almacenamiento, ETL, modelado, ML y BI) y resumen
        │
        ▼
Informe en la página, con el botón «Descargar informe (HTML)»
```

- Mientras AIDM trabaja, la caja de mensajes está desactivada. El informe tarda unos 10–15 minutos.
- Cuando el informe está hecho, la sesión se queda en ese cliente: los mensajes nuevos no lo vuelven a generar. Para otro cliente, vacía el caso (arriba), para Streamlit (Ctrl+C) y arráncalo otra vez.
- Al terminar, la consola dice lo que ha gastado la ejecución (`[gateway] Consumo de la ejecución: …`): llamadas, tokens y cuánto costaría pagando.
- Cada llamada a un modelo deja una línea `[gateway] tarea · modelo · tokens · segundos`. Si responde una reserva, lo dice (`pedido → reserva X`).

---

## Configuración (sin tocar el código)

| Archivo | Qué se cambia ahí |
|---|---|
| `data_structures/modelos.yaml` | Qué modelo usa cada tarea. Las tareas se agrupan en **perfiles** (`extraccion`, `calculo`, `redaccion`, `tecnico`, `contexto_largo`, `codigo`). En cada perfil, el primer modelo es el que se usa y los demás, sus reservas. También van las reglas por proveedor y por modelo (timeouts, reintentos, `max_tokens`), los precios para el contador y los cupos gratuitos. |
| `data_structures/hiperescalares.yaml` | Cómo elige nube el agente de plataforma: pesos de cada criterio, datos de cada nube (región en España, ENS, soberanía, oferta híbrida, formación), palabras que delatan lo que usa el cliente y la valoración de mantener o migrar lo que ya tiene (`existente:`). |
| `data_structures/agents.yaml` | Rol, objetivo y contexto de cada agente de CrewAI. |
| `bd/costes_perfil.csv` | Tarifas por hora de cada perfil (Junior, Middle y Senior). |
| `bd/precios_cloud.csv` | Precios de los servicios de la nube (de momento, Azure) que usan los costes. Se actualiza con `python bd/actualizar_precios.py` (con `--probar`, solo dice qué cambiaría). |
| `data_structures/solution_builder_report_recovery.html` | Plantilla limpia del informe. La de trabajo, `solution_builder_report.html`, se sobrescribe en cada ejecución. |

---

## Comparar modelos

`pruebas_modelos/comparar.py` repite las llamadas de una ejecución grabada (`GRABAR_LLAMADAS=1`) con otros modelos y deja `resumen.md` y `comparacion.html` en `pruebas_modelos/resultados/`:

```bash
python pruebas_modelos/comparar.py --listar mistral                     # los modelos que da un proveedor
python pruebas_modelos/comparar.py pruebas_modelos/grabaciones/<archivo>.jsonl --tareas costes,equipo
```

Las grabaciones y los resultados llevan datos del cliente y están en el `.gitignore`.

---

## Modelo de decisión (opcional)

Un modelo pequeño (Qwen3.5-0.8B ajustado con Unsloth) que contesta preguntas cerradas en unos 4 s, en CPU y en el mismo ordenador, sin que los datos salgan de él. Lo usan:

- **El discovery:** si la respuesta del cliente es un «no lo sé», un «no aplica» o una respuesta vaga.
- **El agente de plataforma:** qué dice el cliente de cada herramienta o nube que nombra, y qué modelo de despliegue le conviene.

Se entrena en Colab y se sirve con `llama-server`. Todo está en `modelo_decision/README.md`. Sin `AIDM_DECISION_URL` en el `.env`, AIDM hace lo mismo con reglas más simples.

> El código usa la **v2** del modelo, que contesta las tres preguntas. La v1 solo conoce la primera: mientras `llama-server` tenga la v1, deja `AIDM_DECISION_URL` fuera del `.env`.

---

## Infracost (opcional)

Con `INFRACOST_API_KEY` en el `.env` (gratis en [dashboard.infracost.io](https://dashboard.infracost.io)) e `infracost.exe` (CLI 0.10) en la raíz o en el PATH, AIDM escribe un Terraform de la propuesta y le pide los precios a Infracost. Sin él, los precios salen de `bd/precios_cloud.csv` y, si un servicio no está, los estima el modelo; las tarjetas del informe dicen de dónde sale cada uno.

---

## Estructura del proyecto

```
AIDM/
├── interface.py                  # La página de Streamlit (punto de entrada)
├── graph.py                      # El grafo: discovery → plataforma → estructura → propuesta → roadmap
├── gateway.py                    # Qué modelo usa cada tarea, con sus reservas, y el registro de llamadas
├── decisiones.py                 # Llamadas al modelo de decisión (llama-server)
├── utils.py                      # Esquemas, lectura y escritura del caso, contexto del cliente, markdown → HTML
├── requirements.txt
├── .streamlit/config.toml        # Sin estadísticas de uso de Streamlit
├── agents/
│   ├── discovery_agent_logic.py
│   ├── plataforma.py             # Agente de plataforma (elige el hiperescalar)
│   ├── structure_proposal_agent_logic.py
│   ├── solution_builder_crew.py  # Propuesta y costes
│   ├── proposal_crew.py
│   ├── estimation_crew.py        # Costes, perfiles y equipo
│   └── roadmap_builder.py        # Las 7 fases del roadmap y el resumen
├── data_structures/              # YAML de configuración, caso, plantillas del informe
├── bd/                           # Tarifas, precios de la nube y su actualizador
├── assets/fonts/                 # La fuente del informe (va dentro del HTML, sin Google Fonts)
├── modelo_decision/              # Datos, cuaderno de Colab y README del modelo de decisión
├── pruebas_modelos/comparar.py   # Comparación de modelos con llamadas grabadas
└── tests/                        # Tests de cada arreglo (sin llamar a modelos) y ejecutar_todos.py
```

---

## Cómo trabajamos

- **Una rama y un PR por fallo**, con su número en el commit («Fallo 71: …»). `main` está protegida: no se hace push directo.
- **Cada arreglo lleva su test** en `tests/`, sin llamar a ningún modelo (se simulan `Crew.kickoff` o `litellm.completion`). Se añade a la lista `TESTS` de `tests/ejecutar_todos.py`. `test_modelos.py` y `test_fallback*.py` sí llaman a los modelos y no van en la lista.
- **Nunca se suben** el `.env`, los logs, las grabaciones ni los `.gguf`.
- Los fallos y su estado están en la revisión «AIDM: revisión para deploy» (pídele el enlace a Rubba).

---

## Problemas frecuentes

| Qué pasa | Por qué | Qué hacer |
|---|---|---|
| `ModuleNotFoundError` al arrancar | El entorno no está activado o faltan dependencias | Activa `aidmENV` y `pip install -r requirements.txt` |
| `AttributeError: module 'streamlit' has no attribute 'iframe'` | Streamlit anterior a 1.56 | `pip install -U streamlit` |
| En la consola, `429` o `RateLimitError` | Cupo del plan gratuito (Mistral y Groq lo agotan pronto) | Nada: responde una reserva (`pedido → reserva X`). Si fallan todas, espera un rato o añade más claves de reserva |
| Una tarea falla con `AuthenticationError` | Falta su clave en el `.env` o está mal | Revisa el `.env`; `comparar.py --comprobar` dice qué proveedor falla |
| `CERTIFICATE_VERIFY_FAILED` | La red o el proxy de la empresa | Prueba desde otra red |
| AIDM empieza preguntando por otro cliente, o genera un informe nada más escribir | `problem_case.json` es el del discovery anterior | Vacíalo con la plantilla (ver [Usar AIDM](#usar-aidm)) |
| El informe ha terminado en la consola (`ROADMAP COMPLETADO`) pero la página no lo enseña | El navegador perdió la conexión durante la generación (portátil suspendido, VPN…) | Antes de nada, copia `data_structures/solution_builder_report.html` a otra carpeta: es el informe. No recargues la página. Si mandas un mensaje y la consola vuelve a lanzar la plataforma, la sesión se perdió: para con Ctrl+C |
| `[decisiones] El modelo de decisión no responde` | `llama-server` no está en marcha | Arráncalo (ver `modelo_decision/README.md`) o quita `AIDM_DECISION_URL` del `.env`; AIDM sigue sin él |