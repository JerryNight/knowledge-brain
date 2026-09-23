# 应用镜像 —— api 与 worker 用同一份代码，只有启动命令不同（spec §4）。
#
# 构建（在仓库根目录）：
#   docker build -f docker/Dockerfile.app -t kb-app:local .
#
# 依赖用 uv 从 lockfile 精确安装；不带 dev 依赖，测试只在 CI/本地跑。

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git 是硬依赖：sync worker 靠它做 partial clone 与 diff（spec §6）。
# 不装的话 worker 起得来，但每个 repo_sync 都会失败。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app

# 先只拷依赖清单，让依赖层独立于源码变更被缓存。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker ./docker
COPY eval ./eval

# `--no-editable` matters: an editable install would point at /app/src and break
# if the source tree is ever mounted over. `--frozen` keeps the lockfile authoritative.
RUN uv sync --frozen --no-dev --no-editable

# 上传原件与 bare clone 的落地目录。挂 volume 上去，否则容器重建即丢。
ENV GIT_WORKDIR_ROOT=/data/git \
    BLOB_STORE_PATH=/data/blobs
RUN mkdir -p /data/git /data/blobs

# 非 root 运行。容器里没有需要提权的操作。
RUN useradd --create-home --uid 10001 kb && chown -R kb:kb /app /data
USER kb

ENV PATH="/app/.venv/bin:$PATH"

# 默认起 API；compose 里 worker 服务覆盖 command（见 docker-compose.yml）。
EXPOSE 8000
CMD ["kb", "serve"]
