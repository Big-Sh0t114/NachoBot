FROM python:3.13.5-slim
LABEL authors="infinitycat233"

# Build from the integrated workspace root:
# docker build -f NachoBot-Napcat-Adapter/Dockerfile .

# Copy uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY NachoBot-Napcat-Adapter/pyproject.toml NachoBot-Napcat-Adapter/uv.lock /adapters/

# Install requirements
WORKDIR /adapters
RUN uv sync --locked --no-dev --no-install-project
ENV PATH="/adapters/.venv/bin:${PATH}"

COPY NachoBot/ncnk_message /NachoBot/ncnk_message
ENV PYTHONPATH=/NachoBot

WORKDIR /adapters

COPY NachoBot-Napcat-Adapter/ .

EXPOSE 8095

ENTRYPOINT ["python", "main.py"]
