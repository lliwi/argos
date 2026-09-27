# Proxy de egress con allowlist (RF-EX-04). Solo stdlib.
FROM python:3.12-alpine
ARG UID=1000
RUN adduser -D -u ${UID} egress
COPY src/argos/sandbox/egress_proxy.py /app/egress_proxy.py
USER egress
EXPOSE 3128
ENTRYPOINT ["python", "/app/egress_proxy.py", "--dir", "/var/egress", "--port", "3128"]
