# 多阶段构建，减小镜像体积
FROM python:3.11-slim as builder

ARG BUILDTIME
ARG VERSION
ARG REVISION
ARG WITH_X1080X_BROWSER=false

WORKDIR /app

# 构建期依赖（curl_cffi 需要 libcurl 头文件 & 编译器才能 build wheel，预装 wheel 通常足够，
# 但保留 build-essential 以兜底 musl/aarch64 等环境）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-auth.txt ./
RUN if [ "$WITH_X1080X_BROWSER" = "true" ]; then \
      pip install --no-cache-dir -r requirements-auth.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi

# 运行阶段
FROM python:3.11-slim
ARG WITH_X1080X_BROWSER=false

# 创建非root用户
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 运行时依赖：curl_cffi 需要 libcurl 的 ssl 后端
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && if [ "$WITH_X1080X_BROWSER" = "true" ]; then apt-get install -y --no-install-recommends \
      libgtk-3-0 libdbus-glib-1-2 libasound2 libx11-xcb1 libxt6 libxrender1 \
      libxrandr2 libxcomposite1 libxcursor1 libxdamage1 libxi6 libnss3 libgbm1; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
RUN if [ "$WITH_X1080X_BROWSER" = "true" ]; then python -m invisible_playwright fetch; fi

COPY --chown=appuser:appuser . .

RUN mkdir -p /app/logs /app/config \
    && chown -R appuser:appuser /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ARG BUILDTIME
ARG VERSION
ARG REVISION
LABEL org.opencontainers.image.created=${BUILDTIME}
LABEL org.opencontainers.image.version=${VERSION}
LABEL org.opencontainers.image.revision=${REVISION}
LABEL org.opencontainers.image.licenses="MIT"

EXPOSE 8181

ENV DOCKER_CONTAINER=true
CMD ["python", "run.py", "--mode", "web"]
