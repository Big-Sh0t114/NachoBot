FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /discord-adapter

# 系统依赖：ffmpeg（音频处理）、libsodium-dev（pynacl/voice）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libffi-dev \
    libsodium-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖列表并安装
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
ENV PATH="/discord-adapter/.venv/bin:${PATH}"

# 复制项目代码
COPY . .

ENTRYPOINT ["python", "main.py"]
