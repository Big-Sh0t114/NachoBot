FROM python:3.13.5-slim
LABEL authors="infinitycat233"

# Copy uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY requirements.txt /requirements.txt

# Install requirements
RUN uv pip install --system --upgrade pip
RUN uv pip install --system -r /requirements.txt

WORKDIR /adapters

COPY . .

EXPOSE 8095

ENTRYPOINT ["python", "main.py"]
