# syntax=docker/dockerfile:1
#
# Production build of the web application served by nginx, which also forwards /api and
# /titiler to the back ends (see nginx.conf).
#
#   docker build -t narsat-web web

FROM node:24-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_API_BASE_URL=/api/v1
ARG VITE_BASEMAP_STYLE_URL=https://tiles.openfreemap.org/styles/liberty
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_BASEMAP_STYLE_URL=${VITE_BASEMAP_STYLE_URL}
RUN npm run build

FROM nginx:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
