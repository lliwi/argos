# ADR-0016 — Orquestador y delegación entre perfiles

- **Estado:** aceptado
- **Fecha:** 2026-09-28
- **Requisitos:** RF-02, RF-05, RF-SEC-02/03, RF-GOV-04, P2, UC-1..4

## Contexto
El usuario quiere un único interlocutor conversacional que entienda la petición y la enrute al
especialista con las herramientas y credenciales adecuadas, en lugar de elegir el perfil a mano.

## Decisión
- **Perfil `orchestrator`** (segmento main), sin herramientas potentes propias: solo conversa,
  recuerda, planifica, consulta el inventario (lectura) y **delega**. Es el perfil por defecto de
  `argos chat`.
- `agent.delegate` admite un argumento **`profile`**: el subagente corre con ESE perfil (sus
  tools, credenciales, dry-run y aprobaciones), con contexto limpio y el workspace del padre.
- **Allowlist por perfil** (`delegate_profiles`): un perfil solo puede delegar a los que declara.
  La validación es en la tool, no se confía al modelo. El orquestador declara `[personal, infra]`.
- **No cruza segmentos** (RF-SEC-02): el destino debe pertenecer al mismo segmento
  (`allows_profile`). osint/pentest viven en otros segmentos y no son alcanzables por delegación;
  se ejecutan cambiando de segmento explícitamente.
- **P2 se mantiene**: el orquestador no lee contenido no confiable y no posee credenciales; la
  capacidad potente vive en el subagente destino, con su HITL. Las aprobaciones del subagente
  llegan al usuario (el bus de eventos enруta al hilo raíz).

## Consecuencias
- Un solo chat enruta tareas de casa (infra) y cotidianas (personal). Las acciones destructivas
  del subagente infra siguen pidiendo aprobación y, por defecto, se simulan (`dry_run: true`).
- Delegación entre segmentos (a osint/pentest) queda como trabajo futuro: requeriría llamadas
  entre núcleos y rompería parte del aislamiento; se evalúa aparte.
