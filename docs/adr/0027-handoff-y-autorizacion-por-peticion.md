# ADR-0027 — El orquestador reenvía a osint/pentest; la petición autoriza el alcance

- **Estado:** aceptado
- **Fecha:** 2026-10-02
- **Requisitos:** UC-1, UC-2, RF-SEC-02, RF-LEG-01, RF-LEG-06, RF-GOV-04, P2, ADR-0016, ADR-0026

## Contexto
Con ADR-0026 osint/pentest solo se usaban escribiendo `!osint`/`/perfil`. El usuario prefiere que,
igual que con infra, baste con pedírselo al orquestador en lenguaje natural. Y que su propia
petición cuente como autorización: hoy `scope`/`authorization_ref` solo salían del fichero del
perfil (vacíos), así que todo pentest fallaba cerrado (RF-LEG-01) y había que editar YAML.

## Decisión

### Reenvío (handoff)
- Nueva tool `agent.handoff(profile, task)` en el orquestador. **No ejecuta ni devuelve** el
  resultado: emite un evento `handoff` y termina el turno. Validada contra los perfiles aislados
  (otros segmentos); para los del mismo segmento se sigue usando `agent.delegate`.
- El canal reenvía: el chat (`_attach`) y Matrix (`_follow`) detectan el evento `handoff`, abren
  la conversación en el daemon del segmento destino y ejecutan allí la tarea. El resultado va al
  usuario; **nunca entra en el contexto del orquestador** (P2). En el chat el perfil pasa a ser el
  destino (vuelta con `/perfil orchestrator`); en Matrix el hilo se reasigna a ese segmento.
- El orquestador recibe en su prompt que osint/pentest existen pero son aislados y que debe usar
  `agent.handoff` con la petición del usuario literal, sin pedirle cambiar de perfil.

### Autorización por petición (RF-LEG-01)
- El alcance de una sesión de pentest ya no sale solo del perfil: `session_scope()` lo toma de los
  **objetivos que el usuario nombra en su petición** (`extract_targets` sobre el texto de la tarea,
  más `opts.scope` si el canal lo aporta y el `scope` del perfil). Es el texto del humano, no lo
  que decida el modelo: el modelo no puede ampliar el alcance.
- `authorization_ref` se genera y se audita (RF-LEG-06): si hay objetivo, "autorizada por el
  usuario en la petición (<canal>, <fecha>)"; o lo que fije el perfil/el canal.
- **Continuidad por hilo**: los hosts se acumulan en `state.thread_scope`, así "sigue con el mismo
  objetivo" mantiene lo ya autorizado. Sin objetivo nombrado ni historial, sigue fallando cerrado.
- Se mantiene intacto el resto: cada acción ofensiva pide aprobación humana (RF-GOV-04), el MCP de
  Kali y `shell.exec` aplican el mismo `Scope`, y el `dry_run` del perfil (simular por defecto,
  RF-19) no cambia.

## Consecuencias
- Para una auditoría/investigación basta pedírsela al orquestador; él la reenvía. El aislamiento de
  segmentos y P2 se mantienen porque el resultado no vuelve a su contexto.
- La autorización es del usuario y queda auditada, sin editar ficheros, pero sigue acotada al host
  que nombró: un objetivo no mencionado se rechaza por alcance (RF-SEC-06).
- Límite: el `task` del handoff lo compone el modelo; si cambiara el objetivo, el alcance derivado
  cambiaría con él, pero cada acción ofensiva sigue pasando por aprobación humana que muestra el
  objetivo real.
