# The web UI as a container: production Vite build served by nginx, with /api proxied
# to the api container -- same path contract as the dev server's proxy (prefix stripped).
# The UI deployment carries no host dependency (no node/npm on the host).
#
#   podman build -t pyrrhula-web:dev -f docker/web.Dockerfile .
#   podman run -d --name pyrrhula_web --network pyrrhula_default -p 5173:80 pyrrhula-web:dev

FROM docker.io/node:22-bookworm-slim AS build
WORKDIR /app
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm run build

FROM docker.io/nginx:1.27-alpine
# The nginx image's entrypoint envsubsts templates/ into conf.d/ on start; the api
# upstream is overridable per deployment (ECS service discovery, k8s Service name).
ENV PYRRHULA_API_UPSTREAM=pyrrhula_api_1:8000
COPY docker/web-nginx.conf.template /etc/nginx/templates/default.conf.template
# Runs before the image's envsubst step; writes conf.d/00-resolver.conf so the
# /api upstream can be re-resolved instead of pinned at startup.
COPY docker/web-resolver.sh /docker-entrypoint.d/15-resolver.sh
COPY --from=build /app/dist /usr/share/nginx/html
