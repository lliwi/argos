# ADR-0025 — Producción con IP macvlan y API remota del núcleo

- **Estado:** aceptado
- **Fecha:** 2026-10-01
- **Requisitos:** RF-04, RNF-05, RF-SEC-02, RF-SEC-04, ADR-0008, ADR-0010

## Contexto
En el servidor de producción el usuario da a cada contenedor que lo necesita una IP propia en la
LAN mediante una red macvlan (`int-lan`). Argos debe usar `192.168.0.34`, y el plugin de Herdr
(en otro equipo) debe conectarse a esa IP. Hasta ahora la API del núcleo solo existía en un socket
Unix de modo 600: **su autenticación era el permiso del fichero**. Esa API controla todo (lanzar
tareas al orquestador, que gestiona Portainer, Home Assistant, DNS…; aprobar acciones
destructivas; kill switch), así que exponerla en la LAN sin más equivaldría a dar ese control a
cualquier equipo de la red.

## Decisión
- **`compose.prod.yaml`** (override), aplicado con **`scripts/up.sh --prod`**: solo el `daemon`
  se une a la red externa `int-lan` con `ipv4_address: ${ARGOS_LAN_IP:-192.168.0.34}` (además
  de `egress_main`). Ni osint, ni pentest, ni matrix ni el broker entran en la LAN
  (`tests/test_remote_api.py`). El script comprueba que la red y los secretos existen.
- **API por TCP opcional** (`argos serve --api-host 0.0.0.0 --api-port 8788`), la misma API que
  el socket, con **TLS y token Bearer obligatorios**. Fail-closed: sin `ARGOS_API_TOKEN` (≥ 32
  caracteres) o sin certificado y clave, el núcleo no arranca. Token comparado en tiempo
  constante; el listener TCP no ejecuta el lifespan (el apagado lo hace el del socket).
- **`argos api-setup <ip>`**: genera el token (`secrets/api.env`, que compose carga en el daemon),
  un certificado autofirmado EC P-256 con la IP en su SAN (`secrets/api-cert.pem` +
  `api-key.pem`) y el fichero del cliente (`secrets/api-client.env`: URL, token y CA). Todo 600;
  el token no se imprime.
- **Cliente**: con `ARGOS_API_URL` (solo `https://`), `ARGOS_API_TOKEN` y `ARGOS_API_CA`
  (certificado fijado; relativo a la raíz del repo), `argos chat|console|core …` hablan con el
  núcleo remoto. Sin ellas, el socket local, como siempre.
- **Plugin de Herdr**: `argos-launch` carga `secrets/api-client.env` si existe y entonces no
  arranca un núcleo local.

## Consecuencias
- En el equipo con Herdr hay que copiar `secrets/api-client.env` y `secrets/api-cert.pem` (nunca
  `api-key.pem`). Quien tenga ese token controla Argos: trátalo como una contraseña de admin.
- Con macvlan, el host del servidor no alcanza la IP de su propio contenedor; en el servidor se
  sigue usando el socket Unix.
- Los webhooks quedan en `192.168.0.34:8787` (protegidos por su token, como antes).
- Rotación: `argos api-setup <ip> --force`, recrear el daemon y volver a copiar los dos ficheros.
