FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace/NachoBot-DiscordVC-Adapter

# git is required for the pinned Pycord Git dependency.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    ffmpeg \
    git \
    libffi-dev \
    libopus0 \
    libsodium-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .

COPY --from=nachobot /ncnk_message /workspace/NachoBot/ncnk_message
COPY --from=multimodal /src /workspace/NachoBot-Multimodal-Adapter/src
COPY --from=multimodal /nachobot_multimodal /workspace/NachoBot-Multimodal-Adapter/nachobot_multimodal
COPY --from=multimodal /configs /workspace/NachoBot-Multimodal-Adapter/configs

ENV PATH="/workspace/NachoBot-DiscordVC-Adapter/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
