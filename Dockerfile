FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/setapi
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 setapi && useradd --uid 10001 --gid setapi --no-create-home setapi
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY --chown=setapi:setapi app ./app
USER 10001:10001
EXPOSE 8055
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8055/health/ready', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8055", "--ws-max-size", "65536"]
