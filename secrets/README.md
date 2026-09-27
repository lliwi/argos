# Secretos

Solo se versionan ficheros cifrados `<perfil>.sops.yaml` (SOPS + age, ADR-0005).
Inicializar: `scripts/init-secrets.sh`. Editar: `sops secrets/infra.sops.yaml`.
Formato (descifrado): claves planas `NOMBRE: valor` que coincidan con `secrets:` del perfil.

## Inventario
`inventory.yaml` (git-ignored, 600) guarda infraestructura: URLs, usuarios, notas y credenciales. Los campos secretos (api_key, token, password…) se redactan y se inyectan a las herramientas; nunca se envían al modelo. Copia de `inventory.example.yaml`.
