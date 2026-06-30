# Agent Gateway

Capa de abstracción para agentes Claude con tres componentes de control:

- **Context Optimizer** — Trunca outputs masivos de tools y los persiste en SQLite con TTL, devolviendo un resumen + `ref_id` para recuperación paginada vía `fetch_fragment`.
- **Loop Arbiter** — Cuatro detectores de bucles patológicos (repetición exacta, presupuesto de llamadas, ráfagas, oscilación A↔B) que inyectan un circuit-breaker antes de que el agente se quede atascado.
- **Secure Executor Gateway** — Ejecuta todo el código/shell de Claude exclusivamente en microVMs de E2B, nunca en el host local. Sandbox por sesión, con detección automática de lenguaje y validación de paths.

## Estado

✅ **79/79 tests unitarios pasando** (arbiter, optimizer, gateway — sin mocks falsos, con SQLite real y E2B mockeado correctamente).
✅ **Servidor MCP real verificado** — arranca, inicializa la base, registra las 7 tools.
✅ **Pipeline end-to-end verificado** — truncamiento real, circuit breaker real, passthrough real.

## Quick Start

```bash
# Instalar dependencias
pip install --break-system-packages -e ".[dev]"
# o con uv:
uv venv && uv pip install -e ".[dev]"

# Configurar
cp .env.example .env
# Editar .env con ANTHROPIC_API_KEY y E2B_API_KEY

# Crear directorio de datos
mkdir -p data

# Correr tests unitarios (no requieren E2B ni red)
python3 -m pytest tests/unit/ -v

# Correr tests de integración (requieren E2B_API_KEY real)
python3 -m pytest tests/integration/ -v -m integration

# Levantar el servidor MCP (stdio, para conectar con Claude Desktop / Claude Code)
python3 -m agent_gateway.main
# o:
agent-gateway
```

## Estructura

```
agent-gateway/
├── pyproject.toml
├── .env.example
├── src/agent_gateway/
│   ├── main.py              # Entry point del servidor MCP
│   ├── types.py             # Enums y dataclasses compartidos
│   ├── config.py            # Config desde variables de entorno
│   ├── session.py           # SessionContext + SessionRegistry
│   ├── middleware.py         # tool_middleware() — el pegamento
│   ├── context_optimizer/   # Componente 1
│   ├── loop_arbiter/        # Componente 2
│   └── secure_executor/     # Componente 3
└── tests/
    ├── unit/                # 79 tests, sin red, sin E2B
    └── integration/         # Requiere E2B_API_KEY
```

## Notas de implementación

- `ToolError` de FastMCP vive en `mcp.server.fastmcp.exceptions`, no en el módulo raíz como sugieren algunos ejemplos online. El código usa `ValueError` en su lugar — el servidor MCP de bajo nivel captura *cualquier* `Exception` y la convierte en `isError=True` en el `CallToolResult`, así que el comportamiento es idéntico sin acoplarse a esa clase específica.
- El import de `e2b_code_interpreter` es perezoso (dentro de `SandboxManager._create()`) para que los tests unitarios no requieran la librería instalada ni una API key.
- `SessionRegistry` vive en memoria por proceso. Para despliegues multi-proceso, hay que externalizar `AgentState` (ej. Redis) — el diseño actual asume un solo proceso del servidor MCP.

## Próximos pasos sugeridos

1. Conectar el orquestador (LangGraph o un loop propio de mensajes de la API de Claude) al servidor MCP vía stdio o SSE.
2. Cablear `execute_python` / `execute_shell` / `fetch_fragment` como las primeras tools reales que Claude puede invocar.
3. Agregar un cron o tarea periódica que llame `ResultStore.purge_expired()` en producción.
4. Si se necesita persistencia de `AgentState` entre reinicios del proceso, migrar de dict en memoria a Redis o SQLite.
