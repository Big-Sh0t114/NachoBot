FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace/NachoBot-Discord-Adapter

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

# Core returns absolute /NachoBot paths for local media. Keep those paths
# resolvable inside the adapter while sharing only the explicitly mounted data.
RUN ln -s /workspace/NachoBot /NachoBot \
    && mkdir -p \
        /workspace/NachoBot/data/sandbox \
        /workspace/NachoBot/data/video \
        /workspace/NachoBot/data/media-tmp

ENV PATH="/workspace/NachoBot-Discord-Adapter/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "main.py"]
