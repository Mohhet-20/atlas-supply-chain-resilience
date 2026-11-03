# ATLAS frontend image (static build served by nginx). Not yet built or run.
FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Same-origin API: nginx proxies /api/ to the backend, so no backend URL or secret is baked into the bundle.
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
