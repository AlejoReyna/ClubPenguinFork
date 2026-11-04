# syntax=docker/dockerfile:1
# Sources are pinned named build contexts, not network clones during the build.
FROM python:3.11-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libffi-dev libjpeg62-turbo-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY --from=houdini requirements.txt /tmp/houdini-requirements.txt
COPY --from=dash requirements.txt /tmp/dash-requirements.txt
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -c /tmp/constraints.txt \
    -r /tmp/houdini-requirements.txt -r /tmp/dash-requirements.txt prometheus-client

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libjpeg62-turbo zlib1g libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home penguin
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=houdini --chown=10001:10001 houdini /app/houdini/houdini
COPY --from=houdini --chown=10001:10001 bootstrap.py /app/houdini/bootstrap.py
COPY --from=puffle --chown=10001:10001 . /app/experiments/puffle_sim/
COPY --from=production --chown=10001:10001 . /app/production/puffle/
RUN PYTHONPATH=/app python -m experiments.puffle_sim.houdini_patch \
    --houdini /app/houdini/houdini --output /tmp/puffle-overlay \
    && cp -R /tmp/puffle-overlay/. /app/houdini/houdini/ && rm -rf /tmp/puffle-overlay
COPY --from=dash --chown=10001:10001 dash /app/dash/dash
COPY --from=dash --chown=10001:10001 locale /app/dash/locale
COPY --from=dash --chown=10001:10001 autocomplete /app/dash/autocomplete
COPY --from=dash --chown=10001:10001 bootstrap.py /app/dash/bootstrap.py
COPY --chown=10001:10001 run.py /app/run.py
COPY --chown=10001:10001 lab_config.py /app/dash/lab_config.py
COPY --chown=10001:10001 exporter.py /app/exporter.py
COPY --chown=10001:10001 socket_proxy.py /app/socket_proxy.py
RUN mkdir -p /app/houdini/logs /var/lib/puffle && chown -R 10001:10001 /app /var/lib/puffle
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["python", "/app/run.py"]

