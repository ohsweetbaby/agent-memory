# 多阶段构建：Node 只负责产出静态资源，运行时仍是 Nginx。
FROM node:20-alpine AS build

ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /app

RUN npm config set registry "$NPM_REGISTRY" \
    && npm install -g pnpm@12.15.0

COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY apps/web ./
RUN pnpm build

FROM nginx:1.27-alpine
COPY infra/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
