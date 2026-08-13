FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

WORKDIR /workspace/NachoBot-Napcat-Adapter

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
COPY --from=nachobot /ncnk_message /workspace/NachoBot/ncnk_message

ENV PATH="/workspace/NachoBot-Napcat-Adapter/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

EXPOSE 8095

ENTRYPOINT ["python", "main.py"]
