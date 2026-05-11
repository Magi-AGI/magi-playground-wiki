FROM python:3.11-slim

LABEL org.opencontainers.image.title="hyperon-runtime"
LABEL org.opencontainers.image.version="0.2.10"
LABEL org.opencontainers.image.description="Sandbox image for evaluating MeTTa source via the hyperon Python bindings."

RUN useradd --create-home --shell /usr/sbin/nologin --uid 1001 metta

RUN pip install --no-cache-dir hyperon==0.2.10

COPY entrypoint.py /opt/entrypoint.py
RUN chmod +x /opt/entrypoint.py && chown metta:metta /opt/entrypoint.py

USER metta
WORKDIR /home/metta

ENTRYPOINT ["python", "/opt/entrypoint.py"]
