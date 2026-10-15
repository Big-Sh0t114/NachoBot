FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

WORKDIR /workspace/NachoBot-Napcat-Adapter

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
# uv validates metadata for optional dependencies even when the extra is disabled.
COPY --from=qqcall /pyproject.toml /workspace/NachoBot-QQCall/pyproject.toml
COPY --from=qqcall /sdk/pyproject.toml /workspace/NachoBot-QQCall/sdk/pyproject.toml
COPY --from=qqcall /sdk/qqcall_client /workspace/NachoBot-QQCall/sdk/qqcall_client
COPY --from=universalvc_sdk /pyproject.toml /workspace/NachoBot-UniversalVC-Adapter/sdk/pyproject.toml
COPY --from=universalvc_sdk /universalvc_client.py /workspace/NachoBot-UniversalVC-Adapter/sdk/universalvc_client.py
RUN uv sync --locked --no-dev --no-install-project

COPY . .
COPY --from=nachobot /ncnk_message /workspace/NachoBot/ncnk_message

ENV PATH="/workspace/NachoBot-Napcat-Adapter/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

EXPOSE 8095

ENTRYPOINT ["python", "main.py"]
