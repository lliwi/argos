FROM kalilinux/kali-rolling:latest

# mcp-kali-server pulls in Python, Flask, uvicorn and the MCP bridge.
# The recommended tools are installed separately as they're optional in the package.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        mcp-kali-server \
        nmap \
        gobuster \
        dirb \
        nikto \
        sqlmap \
        hydra \
        john \
        wpscan \
        whatweb \
        enum4linux \
        ca-certificates

# Patch server.py: CommandExecutor.execute() raises ValueError for list commands
# and also tries to shlex.split() a list (dead code). Both lines must go so that
# nmap/gobuster/etc. endpoints (which build list args) work correctly.
RUN sed -i \
    '/if not isinstance(self.command, str):/d; \
     /raise ValueError.*CommandExecutor expects a string/d; \
     /cmd_args = shlex.split(self.command)/d' \
    /usr/share/mcp-kali-server/server.py

# El contenedor corre con cap_drop:ALL + no-new-privileges (compose), lo que impide a apt bajar
# privilegios al usuario _apt para su sandbox de descarga (setgroups falla). Al ejecutarse ya como
# root, se desactiva ese sandbox para que la autoinstalación de herramientas funcione. Es el
# patrón habitual en contenedores y no reduce el aislamiento del contenedor frente al host.
RUN printf 'APT::Sandbox::User "root";\n' > /etc/apt/apt.conf.d/99-argos-no-sandbox && \
    chown -R root:root /var/cache/apt /var/lib/apt

# Port for the Kali REST API — internal Docker bridge only, not exposed to host
EXPOSE 8000

# kali-server-mcp is the Flask REST API backend (server.py).
# --ip 0.0.0.0 makes it reachable by other containers via service name kali:8000.
CMD ["kali-server-mcp", "--ip", "0.0.0.0", "--port", "8000"]
