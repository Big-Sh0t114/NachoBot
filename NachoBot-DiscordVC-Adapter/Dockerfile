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

# Shared ASR stays owned by Multimodal-Adapter. docker-compose supplies this
# sibling directory as the named "multimodal" build context.
COPY --from=multimodal /src /NachoBot-Multimodal-Adapter/src
COPY --from=multimodal /configs /NachoBot-Multimodal-Adapter/configs

ENTRYPOINT ["python", "main.py"]
