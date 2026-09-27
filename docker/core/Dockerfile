# Núcleo de Argos en contenedor. Incluye Codex CLI, cliente docker, git, sops y age.
#
# El código NO se copia: el proyecto se monta en la MISMA ruta absoluta que en el host
# (docker-out-of-docker: las rutas que el núcleo pasa a `docker run -v` las resuelve el daemon
# del host). Así los cambios de código no requieren rebuild; solo cambios de dependencias.
FROM python:3.12-slim-bookworm

ARG SOPS_VERSION=3.9.4
ARG NODE_MAJOR=22

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git age gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @openai/codex \
 && curl -fsSL -o /usr/local/bin/sops \
      "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
 && chmod +x /usr/local/bin/sops \
 && npm cache clean --force && rm -rf /var/lib/apt/lists/*

COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependencias en /opt/venv (fuera del montaje del proyecto).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project && chmod -R a+rX /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/tmp/home \
    CODEX_HOME=/codex \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "-m", "argos.cli"]
CMD ["--help"]
