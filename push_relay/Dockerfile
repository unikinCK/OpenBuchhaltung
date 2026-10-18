# Push-Relay für die OpenBuchhaltung-iOS-App (ADR-003). Build-Kontext ist das
# Repository-Wurzelverzeichnis: docker compose -f docker-compose.push-relay.yml build
FROM python:3.12-slim@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /relay

RUN groupadd --gid 1000 relay \
    && useradd --uid 1000 --gid relay --home-dir /relay --no-create-home --shell /usr/sbin/nologin relay

COPY push_relay/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY push_relay ./push_relay

USER relay
EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/health', timeout=4)"]

# Genau ein Worker: das Rate-Limit je Instanz liegt im Prozess; Threads für parallele
# Anfragen mehrerer Instanzen.
CMD ["gunicorn", "--bind", "0.0.0.0:8095", "--workers", "1", "--threads", "4", \
     "--access-logfile", "-", "push_relay.wsgi:app"]
