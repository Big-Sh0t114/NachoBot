FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

WORKDIR /NachoBot

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

# The core project supports Python 3.11/3.12.  Keep the native libraries
# needed by FAISS and media-related plugins in the image instead of relying
# on the host installation.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    ffmpeg \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install exactly the dependencies already resolved by the repository lock.
COPY pyproject.toml uv.lock ./
COPY --from=qqcall /sdk/pyproject.toml /NachoBot-QQCall/sdk/pyproject.toml
COPY --from=qqcall /sdk/qqcall_client /NachoBot-QQCall/sdk/qqcall_client
COPY --from=universalvc_sdk /pyproject.toml /NachoBot-UniversalVC-Adapter/sdk/pyproject.toml
COPY --from=universalvc_sdk /universalvc_client.py /NachoBot-UniversalVC-Adapter/sdk/universalvc_client.py
RUN uv sync --locked --no-dev --no-install-project

COPY . .

# Playwright is an optional web-search backend, but the browser must be
# present when that backend is enabled in the mounted configuration.
RUN /NachoBot/.venv/bin/playwright install --with-deps chromium

ENV PATH="/NachoBot/.venv/bin:${PATH}" \
    PYTHONPATH=/NachoBot

EXPOSE 8000

ENTRYPOINT ["python", "bot.py"]
