FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /bilibili-adapter

# 系统依赖：build-essential（stringzilla 编译）、xvfb（屏幕监控）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖列表并安装
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
ENV PATH="/bilibili-adapter/.venv/bin:${PATH}"

# 复制项目代码
COPY . .

ENTRYPOINT ["python", "main.py"]
