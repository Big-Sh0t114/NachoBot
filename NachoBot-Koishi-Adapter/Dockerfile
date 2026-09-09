FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace/NachoBot-Koishi-Adapter

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
COPY --from=nachobot /ncnk_message /workspace/NachoBot/ncnk_message

ENV PATH="/workspace/NachoBot-Koishi-Adapter/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
