# syntax=docker/dockerfile:1
#
# 默认镜像（新前端 SPA 模式）
# =====================================================================
# 本文件构建「新前端」版本：先构建 ant-design-pro（Ant Design Pro + Umi Max）
# 静态产物，再与 Flask 后端一起打包，SPA_ENABLED=true 由 Flask 直接托管前端。
# 如需「旧前端」镜像（无 Node 构建、templates/static 旧界面），使用：
#   docker build -f Dockerfile.legacy -t outlook-email-plus-legacy .
# =====================================================================

FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS frontend
WORKDIR /frontend

COPY ant-design-pro/package.json ant-design-pro/pnpm-lock.yaml ant-design-pro/.npmrc ./
ENV HUSKY=0 CI=true
RUN corepack enable
# Defer setup until Umi's configuration and source files are available.
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile --ignore-scripts --store-dir=/pnpm/store
COPY ant-design-pro/ ./
RUN pnpm rebuild && pnpm exec max setup
ENV NODE_ENV=production
RUN pnpm run build

FROM python:3.11-slim AS runtime-base
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=8 \
    GUNICORN_TIMEOUT=120 \
    SPA_ENABLED=true

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .

RUN mkdir -p /app/data && chmod +x /app/scripts/start-gunicorn.sh && \
    if [ -f /app/.build-info ]; then cp /app/.build-info /app/.build_info_copy; fi

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD ["python","-c","import urllib.request as u; u.urlopen('http://localhost:5000/healthz', timeout=4).read()"]

CMD ["scripts/start-gunicorn.sh"]

# CI supplies the frontend artifact once for both native runtime builds.
FROM runtime-base AS ci-runtime
COPY --from=frontend-dist / /app/ant-design-pro/dist

# Keep a complete source build as the default target.
FROM runtime-base AS app
COPY --from=frontend /frontend/dist /app/ant-design-pro/dist
