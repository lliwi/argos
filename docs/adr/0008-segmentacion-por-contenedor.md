# ADR-0008 — Segmentación de seguridad por contenedor local

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-SEC-01..04, RF-SEC-08, RF-EX-08, RNF-06, RF-LEG-03/04, RNF-10

## Decisión
- Tres segmentos en `config/argos.yaml`: `main` (personal, infra), `osint`, `pentest`. Un perfil
  pertenece a un único segmento y el núcleo rechaza perfiles ajenos (`ARGOS_SEGMENT`).
- Por segmento: servicio de núcleo propio (`core`, `core-osint`, `core-pentest`), red de sandbox
  interna (`argos_sandbox_<seg>`), proxy de egress propio (`egress-<seg>`) y datos propios
  (`var/segments/<seg>`: auditoría, workspaces, blobs; `var/egress/<seg>`: política y eventos).
- Cada núcleo monta el proyecto en solo lectura y solo puede escribir en su segmento. Solo `main`
  recibe la clave age (secretos); osint/pentest no tienen credenciales de servicios (P2).
- Los secretos conocidos se redactan de toda salida de tool **antes** de entrar al contexto del
  modelo (RNF-06), además de en la auditoría. Los datos personales solo se redactan en auditoría.
- Retención: `argos purge` aplica `retention_days` del perfil (osint: 7) o el global; recoge
  blobs huérfanos y anota lo purgado. `argos backup` genera un snapshot cifrado con age.

## Broker de sandbox (completado 2026-09-27)
- Solo el servicio `broker` monta el socket Docker, y no tiene red (`network_mode: none`). Los
  núcleos no tienen el socket ni el grupo docker: piden el sandbox de **su** sesión por
  `var/broker/<seg>/broker.sock` (directorio 700, socket 600), y cada núcleo solo monta el suyo.
- El segmento lo determina el socket por el que llega la petición: un núcleo comprometido no
  puede crear ni usar sandboxes de otro segmento.
- El broker valida cada petición (`sandbox/broker_policy.py`): id de sesión, dominios extra,
  variables de entorno (las de proxy son reservadas), comando y timeout. Imagen, red, proxy,
  límites y workspace montado salen de su configuración, nunca de la petición.
- La política de egress la escribe solo el broker; los núcleos ya no montan `var/egress`.
- Registro propio en `var/broker/broker.jsonl` (nombres de variables, nunca sus valores).
- `sandbox.backend: docker` queda para desarrollo en el host.

## Actualización 2026-09-30 — cierre de RF-SEC-02 con contenedores
Revisión tras añadir el inventario (ADR-0014) y Matrix: tres fugas entre segmentos.
1. Todos los núcleos montaban el repo entero: `core-osint`/`core-pentest` (y `matrix`) podían leer
   `secrets/inventory.yaml`, los tokens y `var/segments/main` (auditoría, memoria, workspaces).
2. Todos compartían la red `argos_egress`: OSINT alcanzaba el daemon (webhooks) y el proxy de
   main, que abre la LAN.
3. Los tokens de Matrix/webhooks vivían en la raíz (`.env.*`), fuera de `secrets/`.

Decisión:
- Un `tmpfs` vacío tapa `${ARGOS_DIR}/var` en todos los núcleos y se remonta encima solo el
  directorio de su segmento; otro tapa `${ARGOS_DIR}/secrets` salvo en `core`/`daemon` de main
  (necesitan inventario y SOPS). `matrix` recibe su token por entorno y no ve `secrets/`.
- Tokens en `secrets/matrix.env` y `secrets/hooks.env`.
- Una red de salida por segmento (`argos_egress_{main,osint,pentest}`): cada núcleo y su proxy
  solo se ven entre sí. `core-pentest` añade `argos_kali`.
- Verificado en vivo desde cada contenedor: OSINT/pentest no leen `secrets/` ni ven datos de
  otros segmentos, y no alcanzan daemon ni proxy de main ni por nombre ni por IP; main sigue
  llegando a la LAN y a Internet. Regresión estática en `tests/test_segmentation.py`.

**RF-SEC-02 se da por cumplido con aislamiento por contenedor** (el requisito admite
"máquinas/contenedores"). Límites aceptados: un fallo del kernel o del daemon Docker del host
rompería el aislamiento (lo evitaría solo otra máquina), el `broker` es el componente de más
privilegio compartido por los tres segmentos, y todos comparten el login de Codex
(`ARGOS_CODEX_HOME`, la suscripción).

## Pendiente
- Cifrado en reposo de la auditoría viva (hoy solo los backups van cifrados).
- Opcional: segmentos sensibles en otra máquina vía Herdr/SSH si el riesgo lo pide.
