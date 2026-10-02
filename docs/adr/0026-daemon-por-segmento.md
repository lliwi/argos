# ADR-0026 — Un daemon por segmento: osint y pentest desde Matrix y el chat

- **Estado:** aceptado
- **Fecha:** 2026-10-02
- **Requisitos:** RF-SEC-02, RF-SEC-03, P2, RF-07, RF-04, ADR-0008, ADR-0016, ADR-0025

## Contexto
Solo existía el daemon del segmento `main` (orquestador, personal, infra). Desde Matrix o el chat
de Herdr no había forma de usar los perfiles `osint` y `pentest`: el orquestador solo delega en
su propio segmento, y así debe seguir. Si la salida de osint/pentest (contenido no confiable de
Internet) entrase en el contexto del orquestador —que maneja infra con credenciales potentes—,
una inyección de prompt podría acabar dando órdenes a Portainer o al DNS (P2).

## Decisión
- **Un daemon por segmento**: `daemon-osint` (perfil compose `daemon`, siempre en marcha) y
  `daemon-pentest` (perfil compose `pentest`, bajo demanda junto a Kali). Misma contención que
  `core-osint` / `core-pentest`: sin `secrets/` (osint solo recibe `secrets/osint.env`), su red
  de egress, sus datos. Solo sirven su socket Unix: sin puertos ni webhooks.
- **El scheduler de cada daemon solo carga lo de su segmento**: las tareas y hooks cuyo perfil no
  pertenece al segmento se descartan al cargar `config/schedules.yaml`.
- **Matrix**: `!osint <tarea>` / `!pentest <tarea>` abren un hilo atendido por el daemon de ese
  segmento; el resto del hilo (y sus aprobaciones, que llevan la marca `[osint]`) sigue allí. Un
  hilo no cambia de segmento: `!osint` dentro de un hilo del orquestador se rechaza. `!estado`,
  `!kill` y `!rearm` actúan en todos los daemons; `!herramientas osint` / `!memoria osint`.
- **Chat**: `/perfil <nombre>` cambia de perfil y, si es de otro segmento, habla con su daemon
  (conversación nueva). `/threads` y `/memoria` pasan a ir por la API (antes leían la BD local:
  en modo remoto mostraban los datos del equipo, no los del servidor).
- **Relé `/seg/<segmento>/…`** en la API del daemon de main, para clientes que solo alcanzan a
  ese daemon (el chat remoto por `https://192.168.0.34:8788`). Reenvía bytes por el socket del
  otro segmento sin interpretarlos; nada entra en sesiones ni en el contexto de main. En remoto
  hereda TLS + token (ADR-0025).
- **Montajes**: `daemon` y `matrix` (main) ven solo los directorios de socket
  `var/segments/{osint,pentest}/run`; ni datos, ni broker, ni red de esos segmentos
  (`tests/test_segmentation.py`). `setup-env.sh` / `up.sh` crean esos directorios (700) para que
  Docker no los cree como root.

## Consecuencias
- El orquestador sigue sin conocer osint/pentest: si se lo pides, dirá que no puede, y debe usarse
  `!osint` o `/perfil osint`.
- Superficie nueva acotada: el puente Matrix y el relé hablan con tres sockets en lugar de uno,
  pero solo transportan texto entre el usuario y cada daemon.
- Aprobaciones de sesiones osint lanzadas desde otros canales no se reenvían a `notify_room`
  (el flujo global que vigila el puente es el de main).
