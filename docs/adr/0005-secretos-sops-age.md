# ADR-0005 — Secretos con SOPS + age (D-6)

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-SEC-04, RNF-06, RF-OB-11

## Decisión
- Ficheros `secrets/<perfil>.sops.yaml` cifrados con age; clave privada fuera del repo
  (`~/.config/argos/age.key`, generada por `scripts/init-secrets.sh`).
- `argos.secrets` descifra con `sops -d` en memoria, entrega a cada perfil solo sus claves y
  registra los valores en el redactor. Los secretos se inyectan como entorno a sandbox/tools y
  **nunca** se serializan al contexto del modelo.
- Si `sops` no está instalado o no hay fichero, el perfil arranca sin secretos (y se avisa).
- Rotación: re-cifrar el fichero; los procesos leen en cada arranque de sesión.
