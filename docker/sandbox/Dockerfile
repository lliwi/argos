# Sandbox de ejecución de Argos (ADR-0002). Sin root, sin capabilities; red solo vía proxy.
FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000
ARG WITH_RUST=false

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git jq less procps nodejs npm \
 && if [ "$WITH_RUST" = "true" ]; then apt-get install -y --no-install-recommends cargo; fi \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g ${GID} agent && useradd -m -u ${UID} -g ${GID} -s /bin/bash agent \
 && mkdir -p /workspace/in /workspace/out && chown -R agent:agent /workspace

USER agent
# venv y prefijo npm en $HOME (volumen por sesión): `pip install` y `npm i -g` sin root.
RUN python -m venv /home/agent/venv \
 && /home/agent/venv/bin/pip install --no-cache-dir --upgrade pip \
 && npm config set prefix /home/agent/.npm-global
ENV PATH=/home/agent/venv/bin:/home/agent/.npm-global/bin:/home/agent/.cargo/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent

WORKDIR /workspace/out
CMD ["sleep", "infinity"]
