# Caddy mit Rate-Limit-Modul (github.com/mholt/caddy-ratelimit).
#
# Das Standard-Image caddy:2 enthält kein Rate-Limiting. Um den auskommentierten
# rate_limit-Block im Caddyfile zu nutzen, im caddy-Service der docker-compose.yml
# `image: caddy:2` durch `build: deploy/caddy` ersetzen und neu bauen:
#   docker compose --profile proxy build caddy && docker compose --profile proxy up -d caddy
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/mholt/caddy-ratelimit

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
