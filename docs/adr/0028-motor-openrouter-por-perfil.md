# ADR-0028 — Motor de modelo por perfil: OpenRouter para pentest

- **Estado:** reemplazado por ADR-0029
- **Fecha:** 2026-10-02
- **Requisitos:** RF-05, ADR-0001, RF-SEC-04

## Contexto
El motor por defecto es Codex CLI (suscripción). El usuario quiere que el perfil `pentest` use un
modelo distinto vía OpenRouter (API compatible con OpenAI), controlado desde el inventario.

## Decisión
- Nuevo proveedor `argos.model.openrouter.OpenRouterProvider`: POST `/chat/completions` con el
  contrato de siempre (recibe el contexto, devuelve UNA decisión; el bucle, las tools y la
  auditoría son de Argos). Envía `render()` como un único prompt (estable entre turnos, favorece
  caché) y parsea la decisión con el parser tolerante común.
- Selección por perfil: campo `engine` en el perfil. `engine: openrouter` (en `pentest`) usa
  OpenRouter **si** el inventario tiene el servicio `open-router` con `enabled: true`, `api_key` y
  `model`; si no, cae al motor por defecto. `make_provider(cfg, name, profile)` aplica el override
  solo cuando el motor real no es `fake` (CI y evals siguen con el simulado).
- La api_key y el modelo salen del inventario (`secrets/inventory.yaml`), nunca del contexto del
  modelo; la clave va solo en la cabecera `Authorization`. `provider_factory` del daemon ahora
  recibe el perfil de la sesión para elegir motor.

## Consecuencias
- Cambiar de modelo o desactivar OpenRouter es editar el inventario (`enabled`, `model`), sin
  tocar código. Otros perfiles siguen en Codex.
- El id del modelo debe ser uno válido para `/chat/completions` (no un sufijo de lotes como
  `:batch`, que ese endpoint rechaza). Verificado con `z-ai/glm-5.3-flash`.
- Límite conocido: el coste de OpenRouter no entra en el presupuesto/kill-switch con tarifas
  reales (se contabilizan tokens); si se quiere tope en dinero, es un paso aparte.
