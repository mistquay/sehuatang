# 多阶段构建，减小镜像体积
FROM python:3.11-slim-trixie AS builder

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

COPY requirements.txt requirements-browser.txt requirements-auth.txt ./
RUN if [ "$WITH_X1080X_BROWSER" = "true" ]; then \
      pip install --no-cache-dir -r requirements-auth.txt; \
    else pip install --no-cache-dir -r requirements-browser.txt; fi

# 运行阶段
FROM python:3.11-slim-trixie
ARG WITH_X1080X_BROWSER=false

# 创建非root用户
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 浏览器和后续登录进程使用同一缓存路径，避免构建在 root HOME、运行却找不到二进制。
ENV INVISIBLE_PLAYWRIGHT_CACHE_DIR=/opt/invisible-playwright

# invisible-playwright 的 headless 在 Linux 通过 Xvfb 运行完整 Firefox。
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && if [ "$WITH_X1080X_BROWSER" = "true" ]; then apt-get install -y --no-install-recommends \
      xvfb xauth fonts-liberation fonts-noto-cjk \
      libgtk-3-0t64 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxt6t64 libxtst6 libxrender1 \
      libxrandr2 libxcomposite1 libxcursor1 libxdamage1 libxfixes3 libxi6 \
      libnss3 libgbm1 libgl1 libglx-mesa0 libegl1 libpci3; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
RUN mkdir -p "$INVISIBLE_PLAYWRIGHT_CACHE_DIR" \
    && if [ "$WITH_X1080X_BROWSER" = "true" ]; then python -m invisible_playwright fetch; fi \
    && chown -R appuser:appuser "$INVISIBLE_PLAYWRIGHT_CACHE_DIR"

COPY --chown=appuser:appuser . .

RUN mkdir -p /app/logs /app/config \
    && chown -R appuser:appuser /app

# 只使用镜像内的二进制；禁止靠联网补下载或访问业务网站来通过检查。
RUN --network=none if [ "$WITH_X1080X_BROWSER" = "true" ]; then \
      python scripts/check_x1080x_browser.py; fi

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
