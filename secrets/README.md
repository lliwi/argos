# Secretos

Solo se versionan ficheros cifrados `<perfil>.sops.yaml` (SOPS + age, ADR-0005).
Inicializar: `scripts/init-secrets.sh`. Editar: `sops secrets/infra.sops.yaml`.
Formato (descifrado): claves planas `NOMBRE: valor` que coincidan con `secrets:` del perfil.
