# Basis-Image per Digest gepinnt (Multi-Arch-Index von python:3.12-slim, Stand
# 2026-09-19). Aktualisieren: `docker buildx imagetools inspect python:3.12-slim`
# liefert den aktuellen Digest; Tag im Kommentar mitpflegen.
FROM python:3.12-slim@sha256:6c4dd321d176d61ea848dc8c73a4f7dbae8f70e0ee48bb411ea2f045b599fa8e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Unprivilegierter Laufzeitbenutzer (UID/GID 1000, damit Bind-Mounts der
# Entwicklungs-Compose auf Linux-Hosts beschreibbar bleiben).
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Code gehört root (nur lesbar); nur instance/ (Uploads, ggf. SQLite) ist beschreibbar.
COPY . .
RUN mkdir -p /app/instance/uploads && chown -R app:app /app/instance

# Commit des gebauten Standes (redeploy.sh übergibt GIT_COMMIT als Build-Arg);
# erscheint in /api/v1/health und im Prüferexport-Manifest.
ARG GIT_COMMIT=""
ENV GIT_COMMIT=${GIT_COMMIT}

USER app
EXPOSE 8000

# Health-Check gegen den DB-gestützten Endpoint (503 → urlopen wirft → Exit 1).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"]

# Produktions-WSGI-Server. Schema-Migrationen laufen nicht hier, sondern als
# eigener Schritt (`alembic upgrade head`, siehe redeploy.sh) bzw. beim App-Start,
# solange DB_AUTO_MIGRATE nicht auf 0 steht.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", \
     "--timeout", "180", "--graceful-timeout", "30", \
     "--access-logfile", "-", "run:app"]
