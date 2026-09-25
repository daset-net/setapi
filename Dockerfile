FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/setapi
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 setapi && useradd --uid 10001 --gid setapi --no-create-home setapi
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY --chown=setapi:setapi app ./app
COPY --chown=setapi:setapi scripts ./scripts
COPY --chown=setapi:setapi compose.yaml install.sh .env.example ./
USER 10001:10001
EXPOSE 8055
HEALTHCHECK --interval=15s --timeout=8s --start-period=90s --retries=3 \
    CMD python -m app.healthcheck
CMD ["python", "-m", "app.launcher"]
