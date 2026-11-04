# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm
RUN useradd --uid 10001 --create-home penguin \
    && mkdir -p /app/bots /var/lib/bots /etc/bots \
    && chown -R 10001:10001 /app /var/lib/bots
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BOTS_PROVIDER_MODE=simulated \
    BOTS_CONFIG_PATH=/etc/bots/config.json BOTS_BUDGET_PATH=/var/lib/bots/budget.json
WORKDIR /app
# Code only: credentials, configs, chat and operator files never enter the image.
COPY --chown=10001:10001 bots/*.py /app/bots/
USER 10001:10001
EXPOSE 9102
ENTRYPOINT ["python", "-m", "bots"]
