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
COPY requirements.txt pyproject.toml ./
RUN uv pip install --system --upgrade pip
RUN uv pip install --system -r requirements.txt

# 复制项目代码
COPY . .

ENTRYPOINT ["python", "main.py"]
