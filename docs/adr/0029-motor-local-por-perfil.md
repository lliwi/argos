# ADR-0029 — Motor de modelo por perfil: modelo local para pentest

- **Estado:** aceptado
- **Fecha:** 2026-10-03
- **Requisitos:** RF-05, ADR-0001, RF-SEC-04
- **Reemplaza:** ADR-0028

## Contexto
ADR-0028 daba al perfil `pentest` un motor alternativo vía OpenRouter. Los modelos cloud han dado
problemas en este uso, así que se prueba con modelos servidos en la red propia.

## Decisión
- Se elimina `OpenRouterProvider`. Nuevo proveedor `argos.model.local.LocalModelProvider` para
  **Ollama**, **llama.cpp** (`llama-server`) o **vLLM**: los tres exponen la API compatible con
  OpenAI, así que un único cliente hace POST a `<url>/v1/chat/completions` con el contrato de
  siempre (recibe el contexto, devuelve UNA decisión; el bucle, las tools y la auditoría son de
  Argos). Envía `render()` como un único prompt y parsea la decisión con el parser tolerante común,
  quitando antes los bloques `<think>…</think>` de los modelos con razonamiento.
- Selección por perfil: `engine: local` (en `pentest`) usa el servicio `local-model` del
  inventario **si** tiene `enabled: true`, `provider` ∈ {ollama, llama.cpp, vllm}, `url` y `model`;
  si no, cae al motor por defecto. Solo sustituye al motor real, nunca a `fake` (CI y evals).
- `api_key` es **opcional**: solo si está se envía `Authorization: Bearer …` (p. ej. vLLM con
  `--api-key`). Sale del inventario, nunca del contexto del modelo.

## Consecuencias
- Cambiar de modelo, servidor o desactivarlo es editar el inventario, sin tocar código. Otros
  perfiles siguen en Codex.
- La ventana de contexto la fija el servidor: en Ollama, la API compatible con OpenAI usa el
  `num_ctx` del modelo, que por defecto puede ser corto para el prompt de Argos; conviene subirlo
  (Modelfile o `OLLAMA_CONTEXT_LENGTH`) o el prompt se truncará en silencio.
- Un modelo pequeño (≈8B) puede fallar más al producir la decisión JSON; el parser tolerante y el
  bucle de reintentos existentes lo absorben, pero la calidad del pentest depende del modelo.
