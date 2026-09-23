FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /workspace/NachoBot-Bilibili-Adapter

# Xvfb supplies a display for mss. PortAudio is needed by the optional
# microphone capture path; the remote Live2D adapter remains the preferred
# audio playback path in containers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    libportaudio2 \
    libx11-6 \
    libxtst6 \
    portaudio19-dev \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .

# The adapter imports only the canonical message package from the Core source
# context. Local model code remains in the independently deployed runtime.
COPY --from=nachobot /ncnk_message /workspace/NachoBot/ncnk_message

ENV PATH="/workspace/NachoBot-Bilibili-Adapter/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# Run the screen-monitoring adapter under a virtual X server even when the
# host has no desktop session.
ENTRYPOINT ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24", "python", "main.py"]
